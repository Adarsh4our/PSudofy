#!/usr/bin/env python3
"""
PSudofy — Remote Lyrics Embedder
===================================
Reads lyrics_data.json (uploaded from local PC) and embeds lyrics
into MP3 ID3 tags on the server.

Usage:
    python3 embed_lyrics_remote.py
"""

import os
import re
import json
import sqlite3
import time
from pathlib import Path
from typing import Optional

from mutagen.id3 import ID3, USLT, SYLT, Encoding, ID3NoHeaderError
from mutagen.mp3 import MP3

DB_PATH    = Path("~/PSudofy/data/navidrome.db").expanduser()
MUSIC_ROOT = Path("~/PSudofy/music").expanduser()
DATA_PATH  = Path("~/PSudofy/lyrics_data.json").expanduser()
LOG_PATH   = Path("~/PSudofy/lyrics_log.json").expanduser()

def resolve_path(db_path_field: str) -> Optional[Path]:
    p = Path(db_path_field)
    if p.exists():
        return p
    candidate = MUSIC_ROOT / db_path_field.lstrip("/")
    if candidate.exists():
        return candidate
    return None

def parse_lrc(lrc: str):
    result = []
    pattern = re.compile(r"\[(\d+):(\d+)(?:\.(\d+))?\](.*)")
    for line in lrc.splitlines():
        m = pattern.match(line.strip())
        if m:
            mins = int(m.group(1))
            secs = int(m.group(2))
            centis = int(m.group(3) or 0)
            text = m.group(4).strip()
            ms = (mins * 60 + secs) * 1000 + centis * 10
            if text:
                result.append((text, ms))
    return result

def embed(mp3_path: Path, plain: str, synced: str) -> bool:
    try:
        try:
            tags = ID3(str(mp3_path))
        except ID3NoHeaderError:
            tags = ID3()
        if plain:
            tags.delall("USLT")
            tags.add(USLT(encoding=Encoding.UTF8, lang="eng", desc="", text=plain))
        if synced:
            parsed = parse_lrc(synced)
            if parsed:
                tags.delall("SYLT")
                tags.add(SYLT(encoding=Encoding.UTF8, lang="eng",
                              format=2, type=1, desc="", text=parsed))
        tags.save(str(mp3_path))
        return True
    except Exception as e:
        print(f"    Error embedding: {e}")
        return False

def main():
    if not DATA_PATH.exists():
        print(f"Error: {DATA_PATH} not found. Upload lyrics_data.json first.")
        return

    with open(DATA_PATH, encoding="utf-8") as f:
        lyrics_data = json.load(f)

    print(f"\nPSudofy — Remote Lyrics Embedder")
    print(f"Lyrics entries to embed: {len(lyrics_data)}\n")

    # Load DB paths
    conn   = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()
    cursor.execute("SELECT title, artist, path FROM media_file WHERE missing=0")
    db_map = {(r[0], r[1]): r[2] for r in cursor.fetchall()}
    conn.close()

    stats = {"embedded": 0, "no_file": 0, "failed": 0}

    for i, (key, entry) in enumerate(lyrics_data.items(), 1):
        title  = entry["title"]
        artist = entry["artist"]
        plain  = entry.get("plain")
        synced = entry.get("synced")

        print(f"[{i}/{len(lyrics_data)}] {title} — {artist.split('/')[0]}", end=" ", flush=True)

        db_path  = db_map.get((title, artist))
        mp3_path = resolve_path(db_path) if db_path else None

        if not mp3_path:
            print("  ⚠️  file not found")
            stats["no_file"] += 1
            continue

        ok = embed(mp3_path, plain or "", synced or "")
        if ok:
            print(f"  ✅ embedded [{entry.get('source','?')}]")
            stats["embedded"] += 1
        else:
            print(f"  ❌ failed")
            stats["failed"] += 1

    # Update lyrics_log.json
    if LOG_PATH.exists():
        with open(LOG_PATH, encoding="utf-8") as f:
            log = json.load(f)
        found_keys = set(lyrics_data.keys())
        for entry in log["songs"]:
            key = f"{entry['title']}|||{entry['artist']}"
            if key in found_keys and entry["status"] == "not_found":
                entry["status"] = "found"
                entry["source"] = lyrics_data[key].get("source")
        with open(LOG_PATH, "w", encoding="utf-8") as f:
            json.dump(log, f, indent=2, ensure_ascii=False)
        print(f"\nUpdated {LOG_PATH}")

    print(f"\n--- Embedding Complete ---")
    print(f"  ✅ Embedded   : {stats['embedded']}")
    print(f"  ⚠️  No file   : {stats['no_file']}")
    print(f"  ❌ Failed     : {stats['failed']}")

if __name__ == "__main__":
    main()
