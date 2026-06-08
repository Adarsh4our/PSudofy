#!/usr/bin/env python3
"""
PSudofy Lyrics Fallback Embedder
==================================
Fetches lyrics for songs that lrclib.net couldn't find.
Uses a 3-source fallback chain:
  1. lrclib.net  — retry without duration filter (catches near-misses)
  2. Genius API  — best Bollywood/Hindi coverage (needs token)
  3. lyrics.ovh  — free no-key fallback

Usage:
    python3 add_lyrics_fallback.py
    python3 add_lyrics_fallback.py --limit 10   # test first N songs
"""

import os
import re
import sys
import time
import json
import sqlite3
import urllib.request
import urllib.parse
import urllib.error
import argparse
from pathlib import Path
from datetime import datetime
from typing import Optional, Tuple
from html.parser import HTMLParser

# ── Mutagen ───────────────────────────────────────────────────────────────────
try:
    from mutagen.id3 import ID3, USLT, Encoding, ID3NoHeaderError
    MUTAGEN = True
except ImportError:
    print("Error: mutagen not installed. Run: sudo apt-get install python3-mutagen")
    sys.exit(1)

# ── BeautifulSoup (for Genius scraping) ───────────────────────────────────────
try:
    from bs4 import BeautifulSoup
    BS4 = True
except ImportError:
    BS4 = False
    print("Warning: python3-bs4 not installed. Genius scraping disabled.")

# ── Config ────────────────────────────────────────────────────────────────────
DB_PATH      = Path("~/PSudofy/data/navidrome.db").expanduser()
MUSIC_ROOT   = Path("~/PSudofy/music").expanduser()
LOG_PATH     = Path("~/PSudofy/lyrics_log.json").expanduser()
GENIUS_TOKEN = "n5vYx1OeQlrFrtX9t3zFGzYs5mNVKJRSnUv4Y3R4VTIMSd2yh-D9xk0xZxF6e536"
DELAY        = 0.6   # seconds between API calls
TIMEOUT      = 10

# ── String helpers ────────────────────────────────────────────────────────────

def clean_title(title: str) -> str:
    t = re.sub(r"\s*\(from\s+[^)]+\)", "", title, flags=re.IGNORECASE)
    t = re.sub(r"\s*-\s*from\s+.+",    "", t,     flags=re.IGNORECASE)
    t = re.sub(r"\s*\(feat\.?\s+[^)]+\)", "", t,  flags=re.IGNORECASE)
    t = re.sub(r"\s*\(ft\.?\s+[^)]+\)",   "", t,  flags=re.IGNORECASE)
    t = re.sub(r"\s*\(with\s+[^)]+\)",    "", t,  flags=re.IGNORECASE)
    t = re.sub(r"\s*\(dance version\)",   "", t,   flags=re.IGNORECASE)
    t = re.sub(r"\s*\(acoustic.*?\)",     "", t,   flags=re.IGNORECASE)
    t = re.sub(r"\s*\(remastered.*?\)",   "", t,   flags=re.IGNORECASE)
    return t.strip()

def primary_artist(artist_str: str) -> str:
    if not artist_str:
        return ""
    return re.split(r"[/,&]|\bfeat\b|\bft\b", artist_str, maxsplit=1)[0].strip()

def make_request(url: str, headers: dict = None) -> Optional[str]:
    """Make an HTTP GET request and return the response body as text."""
    req = urllib.request.Request(url, headers=headers or {
        "User-Agent": "Mozilla/5.0 PSudofy-LyricsFallback/1.0"
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            if resp.status == 200:
                return resp.read().decode("utf-8", errors="replace")
    except Exception:
        pass
    return None

# ── Source 1: lrclib.net (relaxed — no duration) ─────────────────────────────

def fetch_lrclib_relaxed(title: str, artist: str, album: str) -> Tuple[Optional[str], Optional[str]]:
    """Retry lrclib without duration constraint — catches near-misses."""
    params = {
        "track_name":  clean_title(title),
        "artist_name": primary_artist(artist),
    }
    if album:
        params["album_name"] = album

    url  = f"https://lrclib.net/api/get?{urllib.parse.urlencode(params)}"
    body = make_request(url, {"User-Agent": "PSudofy-LyricsFallback/1.0"})
    if body:
        try:
            data   = json.loads(body)
            plain  = data.get("plainLyrics")  or None
            synced = data.get("syncedLyrics") or None
            return plain, synced
        except Exception:
            pass
    return None, None

# ── Source 2: Genius API + page scrape ───────────────────────────────────────

def search_genius(title: str, artist: str) -> Optional[str]:
    """Search Genius API and return the URL of the best matching song page."""
    if not GENIUS_TOKEN or not BS4:
        return None

    query  = f"{clean_title(title)} {primary_artist(artist)}"
    params = urllib.parse.urlencode({"q": query})
    url    = f"https://api.genius.com/search?{params}"
    body   = make_request(url, {
        "Authorization": f"Bearer {GENIUS_TOKEN}",
        "User-Agent": "PSudofy-LyricsFallback/1.0"
    })
    if not body:
        return None

    try:
        data = json.loads(body)
        hits = data.get("response", {}).get("hits", [])
        if hits:
            return hits[0]["result"]["url"]
    except Exception:
        pass
    return None


def scrape_genius_lyrics(page_url: str) -> Optional[str]:
    """Scrape full lyrics from a Genius song page."""
    if not BS4:
        return None

    body = make_request(page_url)
    if not body:
        return None

    try:
        soup = BeautifulSoup(body, "html.parser")

        # Genius wraps lyrics in data-lyrics-container divs
        containers = soup.find_all("div", attrs={"data-lyrics-container": "true"})
        if containers:
            lines = []
            for container in containers:
                # Replace <br> with newlines
                for br in container.find_all("br"):
                    br.replace_with("\n")
                lines.append(container.get_text())
            return "\n".join(lines).strip()

        # Fallback: older Genius page structure
        old = soup.find("div", class_="lyrics")
        if old:
            return old.get_text().strip()

    except Exception:
        pass
    return None


def fetch_genius(title: str, artist: str) -> Optional[str]:
    """Search Genius and scrape lyrics from the result page."""
    page_url = search_genius(title, artist)
    if not page_url:
        return None
    time.sleep(0.3)   # Small delay between search and page fetch
    return scrape_genius_lyrics(page_url)

# ── Source 3: lyrics.ovh ─────────────────────────────────────────────────────

def fetch_lyricsovh(title: str, artist: str) -> Optional[str]:
    """Fetch plain lyrics from lyrics.ovh (free, no key needed)."""
    a = urllib.parse.quote(primary_artist(artist))
    t = urllib.parse.quote(clean_title(title))
    url  = f"https://api.lyrics.ovh/v1/{a}/{t}"
    body = make_request(url)
    if body:
        try:
            data = json.loads(body)
            lyr  = data.get("lyrics", "").strip()
            return lyr if lyr else None
        except Exception:
            pass
    return None

# ── Embed into MP3 ────────────────────────────────────────────────────────────

def embed_plain_lyrics(mp3_path: Path, plain: str) -> bool:
    """Write USLT (plain lyrics) tag into the MP3."""
    try:
        try:
            tags = ID3(str(mp3_path))
        except ID3NoHeaderError:
            tags = ID3()
        tags.delall("USLT")
        tags.add(USLT(encoding=Encoding.UTF8, lang="eng", desc="", text=plain))
        tags.save(str(mp3_path))
        return True
    except Exception:
        return False

def resolve_path(db_path_field: str) -> Optional[Path]:
    """Resolve DB-stored path to actual filesystem path."""
    p = Path(db_path_field)
    if p.exists():
        return p
    candidate = MUSIC_ROOT / db_path_field.lstrip("/")
    if candidate.exists():
        return candidate
    return None

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="PSudofy Lyrics Fallback Embedder")
    parser.add_argument("--limit",   type=int, default=0, help="Process only first N songs")
    parser.add_argument("--offset",  type=int, default=0, help="Skip first N songs (for testing)")
    parser.add_argument("--dry-run", action="store_true", help="Fetch but don't write to files")
    args = parser.parse_args()

    # Load the not-found list from previous run log
    if not LOG_PATH.exists():
        print(f"Error: {LOG_PATH} not found. Run add_lyrics.py first.")
        sys.exit(1)

    with open(LOG_PATH, encoding="utf-8") as f:
        log = json.load(f)

    not_found = [s for s in log["songs"] if s["status"] == "not_found"]
    if args.offset:
        not_found = not_found[args.offset:]
    if args.limit:
        not_found = not_found[:args.limit]

    total = len(not_found)
    print(f"\nPSudofy Lyrics Fallback Embedder")
    print(f"Songs to retry: {total} | Sources: lrclib(relaxed) → Genius → lyrics.ovh\n")

    # Load full song paths from DB (we need the file path)
    conn   = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()
    cursor.execute("SELECT title, artist, album, duration, path FROM media_file WHERE missing=0")
    db_songs = {(r[0], r[1]): r for r in cursor.fetchall()}
    conn.close()

    stats = {"lrclib": 0, "genius": 0, "ovh": 0, "not_found": 0, "failed": 0}
    updated_entries = []
    start = time.time()

    for i, song in enumerate(not_found, 1):
        title  = song.get("title", "") or ""
        artist = song.get("artist", "") or ""

        print(f"[{i}/{total}] {title} — {primary_artist(artist)}", end=" ", flush=True)

        # Find the file path from DB
        db_row  = db_songs.get((title, artist))
        mp3_path = resolve_path(db_row[4]) if db_row else None

        plain  = None
        synced = None
        source = None

        # ── Source 1: lrclib relaxed ─────────────────────────────────────
        album    = db_row[2] if db_row else ""
        p, s     = fetch_lrclib_relaxed(title, artist, album)
        if p or s:
            plain, synced, source = p, s, "lrclib"

        # ── Source 2: Genius ─────────────────────────────────────────────
        if not plain and not synced:
            time.sleep(DELAY)
            g = fetch_genius(title, artist)
            if g:
                plain, source = g, "genius"

        # ── Source 3: lyrics.ovh ─────────────────────────────────────────
        if not plain and not synced:
            time.sleep(DELAY)
            o = fetch_lyricsovh(title, artist)
            if o:
                plain, source = o, "ovh"

        # ── Embed ─────────────────────────────────────────────────────────
        if plain or synced:
            if not args.dry_run and mp3_path:
                ok = embed_plain_lyrics(mp3_path, plain or synced)
                if not ok:
                    print(f"  ❌ write failed")
                    stats["failed"] += 1
                    updated_entries.append({**song, "status": "write_failed"})
                    time.sleep(DELAY)
                    continue

            stats[source] += 1
            tag = "✓ synced" if synced else "plain"
            print(f"  ✅ [{source}] ({tag})")
            updated_entries.append({**song, "status": "found", "source": source,
                                     "synced": bool(synced), "plain": bool(plain)})
        else:
            stats["not_found"] += 1
            print(f"  ⚠️  still not found")
            updated_entries.append({**song, "status": "not_found"})

        time.sleep(DELAY)

    # ── Update log ────────────────────────────────────────────────────────────
    if not args.dry_run:
        # Merge updated entries back into the master log
        updated_map = {(e["title"], e["artist"]): e for e in updated_entries}
        for entry in log["songs"]:
            key = (entry["title"], entry["artist"])
            if key in updated_map:
                entry.update(updated_map[key])

        with open(LOG_PATH, "w", encoding="utf-8") as f:
            json.dump(log, f, indent=2, ensure_ascii=False)

    # ── Summary ───────────────────────────────────────────────────────────────
    elapsed = time.time() - start
    mins, secs = divmod(int(elapsed), 60)
    newly_found = stats["lrclib"] + stats["genius"] + stats["ovh"]

    print(f"\n--- Fallback Lyrics Complete ---")
    print(f"  ✅ lrclib  (relaxed) : {stats['lrclib']}")
    print(f"  ✅ Genius            : {stats['genius']}")
    print(f"  ✅ lyrics.ovh        : {stats['ovh']}")
    print(f"  ⚠️  Still not found  : {stats['not_found']}")
    print(f"  ❌ Write failed      : {stats['failed']}")
    print(f"  🎵 Total recovered   : {newly_found} / {total}")
    print(f"  ⏱️  Time taken        : {mins}m {secs}s")
    print(f"\nLog updated: {LOG_PATH}")

if __name__ == "__main__":
    main()
