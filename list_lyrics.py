# -*- coding: utf-8 -*-
#!/usr/bin/env python3
"""
PSudofy Lyrics Auditor & Categorizer
=====================================
Scans all MP3s in your music library / Navidrome database and categorizes them into:
1. Synced Lyrics (time-stamped / SYLT)
2. Unsynced Lyrics (plain text / USLT)
3. No Lyrics Available (missing)

Usage:
    python list_lyrics.py
    python list_lyrics.py --export   # Saves categorized lists to text/markdown files
    python list_lyrics.py --synced   # Show only synced
    python list_lyrics.py --unsynced # Show only unsynced
    python list_lyrics.py --missing  # Show only missing
"""

import os
import re
import sys
import json
import sqlite3
import argparse
from pathlib import Path

# Mutagen for ID3 tag reading
try:
    from mutagen.mp3 import MP3
    from mutagen.id3 import ID3, USLT, SYLT, ID3NoHeaderError
    MUTAGEN = True
except ImportError:
    MUTAGEN = False

from dotenv import load_dotenv
load_dotenv()

MUSIC_FOLDER = os.getenv("MUSIC_FOLDER", "./music")
DB_PATH      = os.getenv("DB_PATH", "./data/navidrome.db")

def check_file_lyrics(mp3_path: str) -> str:
    """
    Check MP3 ID3 tags and adjacent .lrc files.
    Returns: 'synced', 'unsynced', or 'missing'
    """
    # 1. Check for standalone .lrc file
    lrc_path = os.path.splitext(mp3_path)[0] + ".lrc"
    if os.path.exists(lrc_path):
        try:
            with open(lrc_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
                if re.search(r"\[\d+:\d+", content):
                    return "synced"
                elif content.strip():
                    return "unsynced"
        except Exception:
            pass

    # 2. Check embedded ID3 tags
    if not MUTAGEN or not os.path.exists(mp3_path):
        return "missing"

    try:
        tags = ID3(mp3_path)
        # Check for SYLT (Synced Lyrics)
        for key in tags.keys():
            if key.startswith("SYLT"):
                return "synced"

        # Check USLT (Unsynced lyrics)
        for key in tags.keys():
            if key.startswith("USLT"):
                uslt_frame = tags[key]
                text = getattr(uslt_frame, "text", "")
                if text:
                    if re.search(r"\[\d+:\d+", text):
                        return "synced"
                    return "unsynced"
    except Exception:
        pass

    return "missing"


def audit_library(music_dir: str, db_path: str | None = None) -> tuple[list, list, list]:
    synced = []
    unsynced = []
    missing = []

    # If DB exists, use it to get accurate track title + artist
    tracks = []
    if db_path and os.path.exists(db_path):
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            rows = cur.execute("""
                SELECT path, title, artist, album, lyrics FROM media_file
                WHERE missing = 0 ORDER BY artist, title
            """).fetchall()
            for r in rows:
                tracks.append({
                    "path": r[0],
                    "title": r[1] or "Unknown Title",
                    "artist": r[2] or "Unknown Artist",
                    "album": r[3] or "",
                    "db_lyrics": r[4] or "",
                })
            conn.close()
        except Exception as e:
            print(f"[DB read error] {e}")

    # Fallback to direct directory scan if DB not present
    if not tracks:
        p = Path(music_dir)
        if p.exists():
            for f in sorted(p.rglob("*.mp3")):
                title = f.stem
                artist = f.parent.name if f.parent != p else "Unknown Artist"
                tracks.append({
                    "path": str(f),
                    "title": title,
                    "artist": artist,
                    "album": "",
                    "db_lyrics": "",
                })

    for t in tracks:
        path = t["path"]
        # Convert path if relative
        if not os.path.isabs(path) and not os.path.exists(path):
            test_path = os.path.join(music_dir, path)
            if os.path.exists(test_path):
                path = test_path

        status = check_file_lyrics(path)
        if status == "missing" and t["db_lyrics"]:
            if re.search(r"\[\d+:\d+", t["db_lyrics"]):
                status = "synced"
            else:
                status = "unsynced"

        item = {
            "title": t["title"],
            "artist": t["artist"],
            "album": t.get("album", ""),
            "path": path,
        }

        if status == "synced":
            synced.append(item)
        elif status == "unsynced":
            unsynced.append(item)
        else:
            missing.append(item)

    return synced, unsynced, missing


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="PSudofy Lyrics Auditor")
    parser.add_argument("--synced", action="store_true", help="Show only synced lyrics songs")
    parser.add_argument("--unsynced", action="store_true", help="Show only unsynced lyrics songs")
    parser.add_argument("--missing", action="store_true", help="Show only songs missing lyrics")
    parser.add_argument("--export", action="store_true", help="Export lists to text/markdown files")
    args = parser.parse_args()

    music_dir = os.path.expanduser(MUSIC_FOLDER)
    db_file   = os.path.expanduser(DB_PATH)

    print("Auditing music library for lyrics status...")
    synced, unsynced, missing = audit_library(music_dir, db_file)
    total = len(synced) + len(unsynced) + len(missing)

    print("\n" + "=" * 60)
    print(f"LYRICS AUDIT SUMMARY (Total: {total} songs)")
    print("=" * 60)
    print(f"Synced Lyrics (Time-stamped):  {len(synced):4}  ({(len(synced)/total*100 if total else 0):.1f}%)")
    print(f"Unsynced Lyrics (Plain text):   {len(unsynced):4}  ({(len(unsynced)/total*100 if total else 0):.1f}%)")
    print(f"Missing Lyrics (No lyrics):     {len(missing):4}  ({(len(missing)/total*100 if total else 0):.1f}%)")
    print("=" * 60 + "\n")

    if args.synced or (not args.unsynced and not args.missing):
        print(f"\nSYNCED LYRICS SONGS ({len(synced)}):")
        print("-" * 50)
        for i, s in enumerate(synced, 1):
            print(f"{i:3}. {s['title']} - {s['artist']}")

    if args.unsynced or (not args.synced and not args.missing):
        print(f"\nUNSYNCED / PLAIN TEXT SONGS ({len(unsynced)}):")
        print("-" * 50)
        for i, s in enumerate(unsynced, 1):
            print(f"{i:3}. {s['title']} - {s['artist']}")

    if args.missing or (not args.synced and not args.unsynced):
        print(f"\nMISSING LYRICS SONGS ({len(missing)}):")
        print("-" * 50)
        for i, s in enumerate(missing, 1):
            print(f"{i:3}. {s['title']} - {s['artist']}")

    if args.export:
        with open("lyrics_synced.txt", "w", encoding="utf-8") as f:
            f.write(f"SYNCED LYRICS SONGS ({len(synced)} tracks)\n" + "="*50 + "\n\n")
            for i, s in enumerate(synced, 1):
                f.write(f"{i}. {s['title']} - {s['artist']}\n")

        with open("lyrics_unsynced.txt", "w", encoding="utf-8") as f:
            f.write(f"UNSYNCED PLAIN TEXT LYRICS SONGS ({len(unsynced)} tracks)\n" + "="*50 + "\n\n")
            for i, s in enumerate(unsynced, 1):
                f.write(f"{i}. {s['title']} - {s['artist']}\n")

        with open("lyrics_missing.txt", "w", encoding="utf-8") as f:
            f.write(f"MISSING LYRICS SONGS ({len(missing)} tracks)\n" + "="*50 + "\n\n")
            for i, s in enumerate(missing, 1):
                f.write(f"{i}. {s['title']} - {s['artist']}\n")

        print("\nExported to lyrics_synced.txt, lyrics_unsynced.txt, and lyrics_missing.txt")

if __name__ == "__main__":
    main()
