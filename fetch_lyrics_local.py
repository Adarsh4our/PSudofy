#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PSudofy — Local Lyrics Fetcher
================================
Runs on YOUR PC (not the server) to bypass Genius Cloudflare blocking.
Reads the missing songs from lyrics_log.json, fetches lyrics from Genius
and lrclib.net, saves results to lyrics_data.json for upload to server.

Usage:
    python fetch_lyrics_local.py
    python fetch_lyrics_local.py --limit 10   # test first N songs
"""

import re
import json
import time
import urllib.request
import urllib.parse
import urllib.error
import argparse
from pathlib import Path
from datetime import datetime
from typing import Optional, Tuple

try:
    from bs4 import BeautifulSoup
    BS4 = True
except ImportError:
    BS4 = False
    print("Warning: beautifulsoup4 not installed. Run: pip install beautifulsoup4")
    print("Genius scraping will be disabled — only lrclib will be used.\n")

# ── Config ────────────────────────────────────────────────────────────────────
GENIUS_TOKEN = "n5vYx1OeQlrFrtX9t3zFGzYs5mNVKJRSnUv4Y3R4VTIMSd2yh-D9xk0xZxF6e536"
LOG_PATH     = Path("data/lyrics_log.json")        # downloaded from server
OUTPUT_PATH  = Path("data/lyrics_data.json")        # will be uploaded to server
DELAY        = 0.5
TIMEOUT      = 10

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,hi;q=0.8",
    "Accept-Encoding": "identity",
    "Connection": "keep-alive",
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def clean_title(title: str) -> str:
    t = re.sub(r"\s*\(from\s+[^)]+\)", "", title, flags=re.IGNORECASE)
    t = re.sub(r"\s*-\s*from\s+.+",    "", t,     flags=re.IGNORECASE)
    t = re.sub(r"\s*\(feat\.?\s+[^)]+\)", "", t,  flags=re.IGNORECASE)
    t = re.sub(r"\s*\(ft\.?\s+[^)]+\)",   "", t,  flags=re.IGNORECASE)
    t = re.sub(r"\s*\(with\s+[^)]+\)",    "", t,  flags=re.IGNORECASE)
    t = re.sub(r"\s*\(dance version\)",   "", t,   flags=re.IGNORECASE)
    t = re.sub(r"\s*\(acoustic.*?\)",     "", t,   flags=re.IGNORECASE)
    return t.strip()

def primary_artist(artist_str: str) -> str:
    if not artist_str:
        return ""
    return re.split(r"[/,&]|\bfeat\b|\bft\b", artist_str, maxsplit=1)[0].strip()

def http_get(url: str, headers: dict = None) -> Optional[str]:
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "PSudofy/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception:
        return None

# ── Source 1: lrclib.net (relaxed, no duration) ───────────────────────────────

def fetch_lrclib(title: str, artist: str) -> Tuple[Optional[str], Optional[str]]:
    params = urllib.parse.urlencode({
        "track_name":  clean_title(title),
        "artist_name": primary_artist(artist),
    })
    body = http_get(f"https://lrclib.net/api/get?{params}")
    if body:
        try:
            data = json.loads(body)
            return data.get("plainLyrics") or None, data.get("syncedLyrics") or None
        except Exception:
            pass
    return None, None

# ── Source 2: Genius (search API + page scrape — works from local PC) ─────────

def genius_search_url(title: str, artist: str) -> Optional[str]:
    params = urllib.parse.urlencode({"q": f"{clean_title(title)} {primary_artist(artist)}"})
    body = http_get(
        f"https://api.genius.com/search?{params}",
        {"Authorization": f"Bearer {GENIUS_TOKEN}", "User-Agent": "PSudofy/1.0"}
    )
    if body:
        try:
            hits = json.loads(body)["response"]["hits"]
            if hits:
                return hits[0]["result"]["url"]
        except Exception:
            pass
    return None

def genius_scrape(url: str) -> Optional[str]:
    if not BS4:
        return None
    body = http_get(url, BROWSER_HEADERS)
    if not body:
        return None
    soup = BeautifulSoup(body, "html.parser")

    # Method 1: data-lyrics-container (current Genius layout)
    containers = soup.find_all("div", attrs={"data-lyrics-container": "true"})
    if containers:
        parts = []
        for c in containers:
            for br in c.find_all("br"):
                br.replace_with("\n")
            parts.append(c.get_text())
        text = "\n".join(parts).strip()
        if len(text) > 30:
            return text

    # Method 2: JSON embedded in <script> tag
    for script in soup.find_all("script"):
        src = script.string or ""
        m = re.search(r'"plainLyrics"\s*:\s*"((?:[^"\\]|\\.)*)"', src)
        if m:
            return m.group(1).replace("\\n", "\n").replace('\\"', '"').strip()

    # Method 3: older Genius class
    for div in soup.find_all("div"):
        cls = " ".join(div.get("class", []))
        if "lyrics" in cls.lower():
            text = div.get_text().strip()
            if len(text) > 30:
                return text
    return None

def fetch_genius(title: str, artist: str) -> Optional[str]:
    url = genius_search_url(title, artist)
    if not url:
        return None
    time.sleep(0.3)
    return genius_scrape(url)

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit",  type=int, default=0, help="Process only first N songs")
    parser.add_argument("--offset", type=int, default=0, help="Skip first N songs")
    args = parser.parse_args()

    if not LOG_PATH.exists():
        print(f"Error: {LOG_PATH} not found.")
        print("Download it from the server first:")
        print('  scp -i "...key" ubuntu@161.118.165.241:~/PSudofy/lyrics_log.json data/lyrics_log.json')
        return

    with open(LOG_PATH, encoding="utf-8") as f:
        log = json.load(f)

    not_found = [s for s in log["songs"] if s["status"] == "not_found"]
    if args.offset:
        not_found = not_found[args.offset:]
    if args.limit:
        not_found = not_found[:args.limit]

    total = len(not_found)
    print(f"\nPSudofy — Local Lyrics Fetcher")
    print(f"Songs to fetch: {total} | Sources: lrclib -> Genius")
    print(f"Output: {OUTPUT_PATH}\n")

    # Load existing output if resuming
    results = {}
    if OUTPUT_PATH.exists():
        with open(OUTPUT_PATH, encoding="utf-8") as f:
            results = json.load(f)
        print(f"Resuming — {len(results)} songs already fetched.\n")

    stats = {"lrclib": 0, "genius": 0, "not_found": 0}
    start = time.time()

    for i, song in enumerate(not_found, 1):
        title  = song.get("title", "") or ""
        artist = song.get("artist", "") or ""
        key    = f"{title}|||{artist}"

        if key in results:
            print(f"[{i}/{total}] Skipping (already fetched): {title}")
            continue

        print(f"[{i}/{total}] {title} — {primary_artist(artist)}", end=" ", flush=True)

        plain, synced, source = None, None, None

        # lrclib first (has synced lyrics)
        p, s = fetch_lrclib(title, artist)
        if p or s:
            plain, synced, source = p, s, "lrclib"

        # Genius fallback (better Bollywood coverage)
        if not plain and not synced:
            time.sleep(DELAY)
            g = fetch_genius(title, artist)
            if g:
                plain, source = g, "genius"

        if plain or synced:
            results[key] = {
                "title":  title,
                "artist": artist,
                "source": source,
                "plain":  plain,
                "synced": synced,
            }
            stats[source] += 1
            tag = "synced" if synced else "plain"
            print(f"  ✅ [{source}] ({tag})")
        else:
            stats["not_found"] += 1
            print(f"  ⚠️  not found")

        # Save after every song so we can resume if interrupted
        with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)

        time.sleep(DELAY)

    elapsed = time.time() - start
    mins, secs = divmod(int(elapsed), 60)
    recovered  = stats["lrclib"] + stats["genius"]

    print(f"\n--- Done ---")
    print(f"  ✅ lrclib  : {stats['lrclib']}")
    print(f"  ✅ Genius  : {stats['genius']}")
    print(f"  ⚠️  Not found: {stats['not_found']}")
    print(f"  🎵 Recovered: {recovered} / {total}")
    print(f"  ⏱️  Time    : {mins}m {secs}s")
    print(f"\nNow upload to server:")
    print(f'  scp -i "...key" data/lyrics_data.json ubuntu@161.118.165.241:~/PSudofy/lyrics_data.json')
    print(f"  Then run: python3 ~/PSudofy/embed_lyrics_remote.py")

if __name__ == "__main__":
    main()
