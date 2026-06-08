#!/usr/bin/env python3
"""
PSudofy — Title Cleaner
========================
Removes noisy bracket content from MP3 title tags:
  - (From "Movie Name")
  - - From "Movie Name"
  - (From 'Movie Name')
  - (Official Audio / Video / Lyric Video)
  - (Audio) (HD) (4K) etc.

Usage:
    python3 clean_titles.py            # preview only
    python3 clean_titles.py --apply    # apply changes
"""

import re
import sys
import argparse
from pathlib import Path

from mutagen.id3 import ID3, TIT2, Encoding, ID3NoHeaderError

MUSIC_ROOT = Path("~/PSudofy/music").expanduser()

# ── Patterns to strip (in order) ─────────────────────────────────────────────
STRIP_PATTERNS = [
    # (From "Movie Name") or (From 'Movie Name')
    r'\s*\(From\s+["\'][^"\']+["\'][^)]*\)',
    # - From "Movie Name"  (dash variant, end of string)
    r'\s*-\s*From\s+["\'].+$',
    # (Official Audio), (Official Video), (Lyric Video), (Official Music Video)
    r'\s*\(Official[^)]*\)',
    # (Audio), (Video), (HD), (4K), (HQ)
    r'\s*\((Audio|Video|HD|4K|HQ|Full Song|Full Video|Lyric|Lyrics)\)',
    # (Title Track) when it's the only thing in parens
    r'\s*\(Title Track\)',
]

COMPILED = [re.compile(p, re.IGNORECASE) for p in STRIP_PATTERNS]

def clean(title: str) -> str:
    for pattern in COMPILED:
        title = pattern.sub("", title)
    return title.strip(" -–—")

def process(apply: bool):
    mp3_files = list(MUSIC_ROOT.rglob("*.mp3"))
    print(f"Scanning {len(mp3_files)} MP3 files...\n")

    changes = []

    for mp3 in mp3_files:
        try:
            tags = ID3(str(mp3))
            tit2 = tags.get("TIT2")
            if not tit2:
                continue
            original = str(tit2)
            cleaned  = clean(original)
            if cleaned != original:
                changes.append((mp3, tags, original, cleaned))
        except (ID3NoHeaderError, Exception):
            continue

    print(f"Found {len(changes)} titles to clean:\n")

    for mp3, tags, original, cleaned in changes:
        print(f"  BEFORE: {original}")
        print(f"  AFTER : {cleaned}")
        print()

    if not changes:
        print("Nothing to change!")
        return

    if not apply:
        print(f"--- DRY RUN: {len(changes)} changes would be made ---")
        print("Run with --apply to commit changes.")
        return

    print(f"Applying {len(changes)} changes...")
    success = 0
    for mp3, tags, original, cleaned in changes:
        try:
            tags["TIT2"] = TIT2(encoding=Encoding.UTF8, text=cleaned)
            tags.save(str(mp3))
            success += 1
        except Exception as e:
            print(f"  ERROR on {mp3.name}: {e}")

    print(f"\nDone! {success}/{len(changes)} titles updated.")
    print("Trigger a Navidrome rescan to see changes in the app.")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Apply changes (default is dry-run preview)")
    args = parser.parse_args()
    process(apply=args.apply)

if __name__ == "__main__":
    main()
