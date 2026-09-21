#!/usr/bin/env python3
"""
PSudofy {ext} Filename Sanitation & Migration Utility
======================================================
Cleans up legacy files that have literal `.{ext}.mp3` in their filename
caused by previous spotDL template formatting.

Features:
- Scans music directory for `*.{ext}.mp3` files.
- Checks for name collisions before renaming.
- Safe `--dry-run` by default (no disk or DB changes).
- Updates Navidrome SQLite DB (`media_file.path`) on `--apply` if DB exists.
- Generates a remote Linux bash script (`--server-script`) for running on Oracle Cloud.

Usage:
    python scripts/fix_ext_filenames.py --dry-run
    python scripts/fix_ext_filenames.py --apply
    python scripts/fix_ext_filenames.py --server-script
"""

import os
import sys
import argparse
import sqlite3
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

MUSIC_FOLDER_DEFAULT = os.getenv("MUSIC_FOLDER", "./music")
DB_PATH_DEFAULT = os.getenv("DB_PATH", "./data/navidrome.db")


def find_ext_files(music_dir: Path):
    """Find all files ending with .{ext}.mp3 in music_dir."""
    candidates = []
    if not music_dir.exists():
        return candidates

    for p in music_dir.rglob("*.{ext}.mp3"):
        if p.is_file():
            candidates.append(p)
    return candidates


def generate_target_path(p: Path) -> Path:
    """Replace .{ext}.mp3 with .mp3 in filename."""
    new_name = p.name.replace(".{ext}.mp3", ".mp3")
    return p.with_name(new_name)


def generate_server_bash_script(output_file: str = "fix_ext_server.sh"):
    """Generate a standalone bash script to run on Ubuntu Oracle Cloud server."""
    bash_script = r'''#!/usr/bin/env bash
# PSudofy Remote Server .{ext}.mp3 Migration Script
# Run this on your Ubuntu server in ~/PSudofy

set -e

MUSIC_DIR="${HOME}/PSudofy/music"
DB_PATH="${HOME}/PSudofy/data/navidrome.db"

echo "=== PSudofy .{ext}.mp3 Migration ==="
echo "Target Music Dir: ${MUSIC_DIR}"

count=$(find "${MUSIC_DIR}" -type f -name "*.{ext}.mp3" | wc -l)
echo "Found ${count} tracks with .{ext}.mp3"

if [ "$count" -eq 0 ]; then
    echo "No tracks to rename. Exiting."
    exit 0
fi

read -p "Do you want to rename these files and update SQLite DB? (y/N) " confirm
if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
    echo "Aborted."
    exit 1
fi

echo "Renaming files..."
find "${MUSIC_DIR}" -type f -name "*.{ext}.mp3" | while read -r file; do
    newfile="${file//.{ext}.mp3/.mp3}"
    if [ -f "$newfile" ]; then
        echo "Skip (collision): $newfile"
    else
        mv "$file" "$newfile"
    fi
done

echo "Files renamed successfully."

if [ -f "${DB_PATH}" ]; then
    echo "Updating SQLite Navidrome database paths..."
    sqlite3 "${DB_PATH}" "UPDATE media_file SET path = replace(path, '.{ext}.mp3', '.mp3') WHERE path LIKE '%.{ext}.mp3';"
    echo "Database paths updated."
fi

echo "Triggering Navidrome rescan..."
if command -v docker &>/dev/null; then
    docker exec psudofy_navidrome_1 /app/navidrome scan || true
fi

echo "=== Done! Pull to refresh in Symfonium ==="
'''
    with open(output_file, "w", encoding="utf-8", newline="\n") as f:
        f.write(bash_script)
    print(f"[OK] Generated server bash script: {output_file}")


def main():
    parser = argparse.ArgumentParser(description="Sanitize .{ext}.mp3 filenames in PSudofy library")
    parser.add_argument("--music-dir", default=MUSIC_FOLDER_DEFAULT, help="Path to music folder")
    parser.add_argument("--db-path", default=DB_PATH_DEFAULT, help="Path to navidrome.db")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Preview changes without renaming (default)")
    parser.add_argument("--apply", action="store_true", help="Apply file renames and database updates")
    parser.add_argument("--server-script", action="store_true", help="Generate bash script for Oracle Cloud Ubuntu server")
    args = parser.parse_args()

    if args.server_script:
        generate_server_bash_script()
        return

    apply_mode = args.apply
    music_dir = Path(args.music_dir).resolve()
    db_path = Path(args.db_path).resolve()

    print(f"[*] Scanning: {music_dir}")
    candidates = find_ext_files(music_dir)
    print(f"[*] Total candidates found: {len(candidates)}")

    if not candidates:
        print("[OK] No .{ext}.mp3 files found.")
        return

    # Check for collisions
    collisions = []
    renames = []
    for p in candidates:
        target = generate_target_path(p)
        if target.exists():
            collisions.append((p, target))
        else:
            renames.append((p, target))

    print(f"[*] Safe to rename: {len(renames)}")
    if collisions:
        print(f"[!] Warning: {len(collisions)} collisions found (target already exists)")
        for src, dst in collisions[:5]:
            print(f"    - Source: {src.name}\n      Target: {dst.name}")

    if not apply_mode:
        print("\n--- DRY-RUN PREVIEW (First 10 items) ---")
        for src, dst in renames[:10]:
            print(f"  {src.name}  ->  {dst.name}")
        if len(renames) > 10:
            print(f"  ...and {len(renames) - 10} more files.")
        print("\n[INFO] To execute changes, run:")
        print("    python scripts/fix_ext_filenames.py --apply")
        print("To generate a script for your Oracle Cloud server, run:")
        print("    python scripts/fix_ext_filenames.py --server-script")
        return

    # Execute renames
    print(f"\n[*] Applying rename to {len(renames)} files...")
    renamed_count = 0
    for src, dst in renames:
        try:
            src.rename(dst)
            renamed_count += 1
        except Exception as e:
            print(f"[ERROR] Failed renaming {src.name}: {e}")

    print(f"[OK] Successfully renamed {renamed_count} files on disk.")

    # Update SQLite database if available
    if db_path.exists():
        print(f"[*] Checking Navidrome database at: {db_path}")
        try:
            conn = sqlite3.connect(str(db_path))
            cur = conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            # Replace paths in media_file table
            cur.execute(
                "UPDATE media_file SET path = replace(path, '.{ext}.mp3', '.mp3') WHERE path LIKE '%.{ext}.mp3%'"
            )
            affected = cur.rowcount
            conn.commit()
            conn.close()
            print(f"[OK] Database updated: {affected} paths updated in Navidrome SQLite database.")
        except Exception as e:
            print(f"[WARN] Database update skipped or failed ({e}). Run a library scan via `!scan` in Discord.")
    else:
        print("[INFO] Database file not found locally. If using on remote server, trigger `!scan` in Discord.")


if __name__ == "__main__":
    main()
