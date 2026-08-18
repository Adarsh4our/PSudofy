#!/usr/bin/env python3
"""
PSudofy Smart Playlist Generator
==================================
Uses Last.fm API (650M+ crowd-tagged tracks) + ListenBrainz personal history
to automatically generate themed playlists from your library.

Usage:
    python smart_playlists.py --preview    # Show what would be created (no writes)
    python smart_playlists.py --run        # Write playlists to server
    python smart_playlists.py --refresh    # Clear cache + re-fetch all tags, then run
"""

import os
import sys
import json
import time
import uuid
import subprocess
import argparse
import urllib.request
import urllib.parse
import sqlite3
import random
from datetime import datetime, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from dotenv import load_dotenv
load_dotenv()

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box
from rich.progress import Progress, BarColumn, TextColumn, TimeElapsedColumn

console = Console(force_terminal=True)

# ── Config ────────────────────────────────────────────────────────────────────
SSH_KEY    = os.getenv("SSH_KEY_PATH", "")
SSH_HOST   = os.getenv("SSH_HOST", "ubuntu@168.138.114.54")
REMOTE_DIR = os.getenv("REMOTE_DIR", "~/PSudofy")
REMOTE_DB  = REMOTE_DIR + "/data/navidrome.db"

LASTFM_KEY  = os.getenv("LASTFM_API_KEY", "")
LASTFM_BASE = "http://ws.audioscrobbler.com/2.0/"

LB_USER    = os.getenv("LISTENBRAINZ_USER", "adarsh4our")
LB_BASE    = "https://api.listenbrainz.org/1"

CACHE_FILE  = "lastfm_cache.json"
OWNER_ID    = "IyLjH8H2Wk8fr0iOXqL0pR"
OWNER_NAME  = "adarsh4our"
HIGH_BPM    = 120
LOW_BPM     = 85

# ── Tag Clusters ──────────────────────────────────────────────────────────────
TAG_CLUSTERS = {
    "Sad and Emotional": [
        "sad", "melancholy", "emotional", "heartbreak", "depressing",
        "melancholic", "sorrowful", "heartbreaking", "crying",
        "longing", "loneliness", "grief", "loss",
    ],
    "Romantic Vibes": [
        "love", "romantic", "romance", "love songs", "lovesong",
        "beautiful", "passionate", "seductive", "sweet",
    ],
    "Party and Upbeat": [
        "party", "dance", "upbeat", "feel-good", "fun", "feelgood",
        "happy", "cheerful", "euphoric", "joyful", "celebration",
        "dancefloor", "banger",
    ],
    "Chill and Relax": [
        "chill", "relax", "relaxing", "calm", "study", "chillout",
        "soft", "lo-fi", "lofi", "peaceful", "mellow",
        "easy listening", "coffee",
    ],
    "Late Night Feels": [
        "night", "late night", "sleep", "dark", "midnight",
        "atmospheric", "dreamy", "introspective", "rainy day", "rain",
        "solitude",
    ],
    "Workout Energy": [
        "workout", "gym", "motivation", "motivational", "running",
        "pump up", "intense", "aggressive", "adrenaline", "power",
    ],
    "Bollywood and Hindi": [
        "bollywood", "hindi", "indian", "desi", "punjabi",
        "hindi pop", "filmi",
    ],
    "Indie and Alt": [
        "indie", "indie pop", "alternative", "indie rock", "alt-pop",
        "indie folk", "dream pop", "shoegaze", "emo", "folk",
    ],
    "Nostalgia": [
        "90s", "2000s", "2010s", "classic", "retro", "oldies",
        "throwback", "classics", "80s", "70s", "vintage",
    ],
}


# ── Utilities ─────────────────────────────────────────────────────────────────

def http_get_json(url):
    req = urllib.request.Request(
        url, headers={"User-Agent": "PSudofy-SmartPlaylists/1.0"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            if r.status == 200:
                raw = r.read().decode("utf-8")
                if raw.strip():
                    return json.loads(raw)
    except Exception:
        pass
    return {}


def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_cache(cache):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def ssh_cmd(cmd):
    r = subprocess.run(
        ["ssh", "-i", SSH_KEY, "-o", "StrictHostKeyChecking=no",
         "-o", "ConnectTimeout=15", SSH_HOST, cmd],
        capture_output=True, text=True,
    )
    return r.returncode, r.stdout + r.stderr


def scp_download(remote, local):
    r = subprocess.run(
        ["scp", "-i", SSH_KEY, "-o", "StrictHostKeyChecking=no",
         f"{SSH_HOST}:{remote}", local],
        capture_output=True, text=True,
    )
    return r.returncode == 0


def scp_upload(local, remote):
    r = subprocess.run(
        ["scp", "-i", SSH_KEY, "-o", "StrictHostKeyChecking=no",
         local, f"{SSH_HOST}:{remote}"],
        capture_output=True, text=True,
    )
    return r.returncode == 0


# ── Last.fm ───────────────────────────────────────────────────────────────────

def get_lastfm_tags(title, artist, cache):
    key = (artist or "").lower().strip() + "|" + (title or "").lower().strip()
    if key in cache:
        return cache[key]
    params = urllib.parse.urlencode({
        "method": "track.getTopTags",
        "track": title,
        "artist": artist,
        "api_key": LASTFM_KEY,
        "format": "json",
        "autocorrect": 1,
    })
    data = http_get_json(LASTFM_BASE + "?" + params)
    tags = []
    for t in data.get("toptags", {}).get("tag", [])[:20]:
        name = (t.get("name") or "").lower().strip()
        count = int(t.get("count") or 0)
        if name and count >= 5:
            tags.append(name)
    time.sleep(0.25)
    cache[key] = tags
    return tags


# ── ListenBrainz ─────────────────────────────────────────────────────────────

def get_lb_trending_artists(limit=50):
    url = LB_BASE + "/user/" + urllib.parse.quote(LB_USER) + "/listens?count=" + str(limit)
    data = http_get_json(url)
    artists, seen = [], set()
    for listen in data.get("payload", {}).get("listens", []):
        a = (listen.get("track_metadata", {}).get("artist_name") or "").strip()
        k = a.lower()
        if a and k not in seen:
            seen.add(k)
            artists.append(a)
    return artists


# ── Cluster mapping ───────────────────────────────────────────────────────────

def map_tags_to_clusters(tags):
    tags_set = set(tags)
    matched = []
    for pname, kws in TAG_CLUSTERS.items():
        for kw in kws:
            if kw in tags_set:
                matched.append(pname)
                break
    return matched


# ── DB helpers ────────────────────────────────────────────────────────────────

def download_and_read_db():
    console.print("[cyan]>[/cyan] Downloading database from server...")
    local_db = "temp_navidrome_smart_read.db"
    if not scp_download(REMOTE_DB, local_db):
        console.print("[red]Failed to download database.[/red]")
        sys.exit(1)
    conn = sqlite3.connect(local_db)
    conn.row_factory = sqlite3.Row
    songs = conn.execute(
        "SELECT id, title, artist, album, genre, year, bpm, duration, average_rating "
        "FROM media_file WHERE missing = 0"
    ).fetchall()
    annotations = conn.execute(
        "SELECT item_id, rating, starred FROM annotation "
        "WHERE user_id = ? AND item_type = 'media_file'",
        (OWNER_ID,),
    ).fetchall()
    conn.close()
    try:
        os.remove(local_db)
    except Exception:
        pass
    console.print(
        f"[green]OK[/green] Loaded [bold cyan]{len(songs)}[/bold cyan] songs "
        f"+ [bold cyan]{len(annotations)}[/bold cyan] rated/starred tracks\n"
    )
    return list(songs), list(annotations)


# ── Playlist builder ──────────────────────────────────────────────────────────

def build_playlists(songs, annotations, cache):
    # --- ratings
    rated_ids   = {r["item_id"] for r in annotations if (r["rating"] or 0) >= 4}
    starred_ids = {r["item_id"] for r in annotations if r["starred"]}
    top_rated   = list(rated_ids | starred_ids)

    # --- ListenBrainz trending
    console.print("[cyan]>[/cyan] Fetching ListenBrainz trending artists...")
    trending_artists = get_lb_trending_artists(50)
    trending_lower   = {a.lower() for a in trending_artists}
    console.print(
        f"[green]OK[/green] {len(trending_artists)} recently scrobbled artists found\n"
    )

    trending_picks = []
    for song in songs:
        al = (song["artist"] or "").lower()
        for ta in trending_lower:
            if ta and (ta in al or al in ta):
                trending_picks.append(song["id"])
                break

    # --- initialise buckets
    playlists = {name: [] for name in TAG_CLUSTERS.keys()}
    playlists["Trending Picks"] = trending_picks
    playlists["Top Rated"]      = top_rated
    playlists["Daily Mix"]      = []

    new_songs    = [s for s in songs
                    if (s["artist"] or "").lower().strip() + "|" + (s["title"] or "").lower().strip()
                    not in cache]
    cached_count = len(songs) - len(new_songs)

    console.print(
        f"[cyan]>[/cyan] Enriching with Last.fm world tags...\n"
        f"  {cached_count} cached  |  {len(new_songs)} need API\n"
    )

    all_tagged = []

    with Progress(
        TextColumn("[bold cyan]{task.description}"),
        BarColumn(bar_width=40),
        TextColumn("[cyan]{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
    ) as prog:
        task = prog.add_task("Fetching Last.fm tags...", total=len(songs))
        for song in songs:
            title  = song["title"]  or ""
            artist = song["artist"] or ""
            tags     = get_lastfm_tags(title, artist, cache)
            clusters = map_tags_to_clusters(tags)
            # BPM fallback when Last.fm returns nothing useful
            if not clusters:
                bpm = song["bpm"] or 0
                if bpm >= HIGH_BPM:
                    clusters = ["Party and Upbeat"]
                elif 0 < bpm <= LOW_BPM:
                    clusters = ["Chill and Relax"]
            for c in clusters:
                playlists[c].append(song["id"])
            if clusters:
                all_tagged.append(song["id"])
            prog.advance(task)

    save_cache(cache)

    # --- daily mix
    pool = list(set(all_tagged))
    random.shuffle(pool)
    playlists["Daily Mix"] = pool[:30]

    # --- deduplicate
    for name in list(playlists.keys()):
        seen, deduped = set(), []
        for sid in playlists[name]:
            if sid not in seen:
                seen.add(sid)
                deduped.append(sid)
        playlists[name] = deduped

    return playlists


# ── Preview ───────────────────────────────────────────────────────────────────

def preview_playlists(playlists, songs):
    song_map = {s["id"]: s for s in songs}
    console.print()
    console.print(Panel("[bold cyan]Smart Playlist Preview[/bold cyan]", border_style="cyan"))
    console.print()
    tbl = Table(
        box=box.ROUNDED, border_style="cyan",
        title="Playlists to be Written to Your Server",
    )
    tbl.add_column("Playlist",      style="bold white",  min_width=24)
    tbl.add_column("Songs",         style="bold cyan",   justify="right", min_width=6)
    tbl.add_column("Sample Tracks", style="dim white")
    total_songs = 0
    for name, ids in playlists.items():
        if not ids:
            continue
        total_songs += len(ids)
        samples = []
        for sid in ids[:3]:
            s = song_map.get(sid)
            if s:
                samples.append(
                    (s["title"] or "?")[:38] + "  --  " + (s["artist"] or "?")[:22]
                )
        tbl.add_row(name, str(len(ids)), "\n".join(samples))
    console.print(tbl)
    non_empty = sum(1 for ids in playlists.values() if ids)
    console.print(f"\n  {non_empty} playlists  |  {total_songs} total track entries\n")


# ── Write ─────────────────────────────────────────────────────────────────────

def restart_navidrome():
    console.print("[cyan]>[/cyan] Restarting Navidrome...")
    ssh_cmd(
        "cd " + REMOTE_DIR +
        " && sudo docker compose up -d 2>/dev/null || sudo docker-compose up -d 2>/dev/null"
    )
    time.sleep(3)
    console.print("[green]OK[/green] Navidrome is back online")


def write_playlists_to_server(playlists):
    now      = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    local_db = "temp_navidrome_smart_write.db"

    console.print("\n[cyan]>[/cyan] Pausing Navidrome for safe DB write...")
    ssh_cmd(
        "cd " + REMOTE_DIR +
        " && sudo docker compose stop 2>/dev/null || sudo docker-compose stop 2>/dev/null"
    )
    time.sleep(2)

    console.print("[cyan]>[/cyan] Downloading database...")
    if not scp_download(REMOTE_DB, local_db):
        console.print("[red]Failed to download database.[/red]")
        restart_navidrome()
        sys.exit(1)

    conn = sqlite3.connect(local_db)
    conn.execute("PRAGMA journal_mode=WAL")
    created = updated = total_tracks = 0

    for name, ids in playlists.items():
        if not ids:
            continue
        existing = conn.execute(
            "SELECT id FROM playlist WHERE name = ? AND owner_id = ?",
            (name, OWNER_ID),
        ).fetchone()
        if existing:
            pl_id = existing[0]
            conn.execute("DELETE FROM playlist_tracks WHERE playlist_id = ?", (pl_id,))
            conn.execute(
                "UPDATE playlist SET updated_at = ?, comment = ? WHERE id = ?",
                (now, "Auto-generated by PSudofy Smart Playlists", pl_id),
            )
            updated += 1
        else:
            pl_id = uuid.uuid4().hex[:20]
            conn.execute(
                "INSERT INTO playlist "
                "(id, name, comment, public, path, sync, owner_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    pl_id, name,
                    "Auto-generated by PSudofy Smart Playlists",
                    0, "", 0, OWNER_ID, now, now,
                ),
            )
            created += 1

        for media_id in ids:
            if not conn.execute(
                "SELECT 1 FROM media_file WHERE id = ?", (media_id,)
            ).fetchone():
                continue
            pt_id = uuid.uuid4().hex[:20]
            conn.execute(
                "INSERT OR IGNORE INTO playlist_tracks "
                "(id, playlist_id, media_file_id) VALUES (?, ?, ?)",
                (pt_id, pl_id, media_id),
            )
            total_tracks += 1

    conn.commit()
    conn.close()
    console.print(
        f"[green]OK[/green] {created} new playlists, {updated} updated, "
        f"{total_tracks} track entries written"
    )

    console.print("[cyan]>[/cyan] Uploading database to server...")
    ok = scp_upload(local_db, REMOTE_DB)
    try:
        os.remove(local_db)
    except Exception:
        pass
    if not ok:
        console.print("[red]Upload failed.[/red]")
        restart_navidrome()
        sys.exit(1)
    console.print("[green]OK[/green] Database uploaded")

    restart_navidrome()

    console.print()
    console.print(Panel(
        f"[bold green]{created + updated} Smart Playlists written to your server![/bold green]\n"
        "Open Navidrome -> Playlists   |   Then refresh Symfonium to sync",
        border_style="green",
    ))


# ── Entry ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="PSudofy Smart Playlist Generator"
    )
    parser.add_argument("--preview", action="store_true",
                        help="Preview playlists without writing")
    parser.add_argument("--run",     action="store_true",
                        help="Generate and write playlists to server")
    parser.add_argument("--refresh", action="store_true",
                        help="Clear Last.fm cache and re-fetch all tags")
    args = parser.parse_args()

    if not args.preview and not args.run:
        parser.print_help()
        sys.exit(0)

    if not LASTFM_KEY:
        console.print("[red]LASTFM_API_KEY not set in .env[/red]")
        sys.exit(1)

    console.print()
    console.print(Panel(
        "[bold cyan]PSudofy - Smart Playlist Generator[/bold cyan]\n"
        "[dim]Powered by Last.fm world music knowledge base + ListenBrainz[/dim]",
        border_style="cyan",
    ))
    console.print()

    cache = {} if args.refresh else load_cache()
    songs, annotations = download_and_read_db()
    playlists = build_playlists(songs, annotations, cache)
    preview_playlists(playlists, songs)

    if args.run:
        write_playlists_to_server(playlists)
    else:
        console.print(
            "[dim]Run with --run to write these playlists to your server.[/dim]\n"
        )


if __name__ == "__main__":
    main()
