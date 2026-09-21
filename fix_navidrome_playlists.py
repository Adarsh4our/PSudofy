# -*- coding: utf-8 -*-
"""
Fix Navidrome Playlist Tracks Schema & Renumbering
==================================================
Fixes the UNIQUE constraint error during Navidrome GC scan:
"running GC: renumbering playlist ...: UNIQUE constraint failed: playlist_tracks.playlist_id, playlist_tracks.id"

Ensures playlist_tracks.id is a clean 1-based sequential integer (1, 2, 3, ...) for every playlist.
"""

import os
import sys
import sqlite3

def fix_playlists(db_path: str):
    print(f"🔧 Opening Navidrome database: {db_path}")
    if not os.path.exists(db_path):
        print(f"❌ Database not found at {db_path}")
        return False

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    try:
        # Check schema of playlist_tracks
        schema = cur.execute("SELECT sql FROM sqlite_master WHERE name='playlist_tracks'").fetchone()
        print(f"📋 Current schema:\n{schema[0] if schema else 'None'}\n")

        # Fetch all playlists
        playlists = cur.execute("SELECT id, name FROM playlist").fetchall()
        print(f"🎵 Found {len(playlists)} playlist(s):")

        total_cleaned = 0
        for pl_id, name in playlists:
            # Fetch all tracks for this playlist in current order, filtering out missing/orphaned media files
            rows = cur.execute("""
                SELECT pt.media_file_id 
                FROM playlist_tracks pt
                JOIN media_file mf ON pt.media_file_id = mf.id
                WHERE pt.playlist_id = ? AND mf.missing = 0
                ORDER BY pt.rowid
            """, (pl_id,)).fetchall()

            # Deduplicate media_file_ids while preserving order
            seen = set()
            unique_media_ids = []
            for r in rows:
                mid = r[0]
                if mid and mid not in seen:
                    seen.add(mid)
                    unique_media_ids.append(mid)

            # Delete all existing playlist_tracks for this playlist
            cur.execute("DELETE FROM playlist_tracks WHERE playlist_id = ?", (pl_id,))

            # Re-insert with clean 1-based integer sequence for 'id' (position)
            for pos, mid in enumerate(unique_media_ids, 1):
                cur.execute("""
                    INSERT INTO playlist_tracks (id, playlist_id, media_file_id)
                    VALUES (?, ?, ?)
                """, (pos, pl_id, mid))

            print(f"  ✓ Playlist '{name}' ({pl_id}): re-indexed {len(unique_media_ids)} tracks (1..{len(unique_media_ids)})")
            total_cleaned += len(unique_media_ids)

        # Delete any orphaned tracks pointing to non-existent playlists
        cur.execute("DELETE FROM playlist_tracks WHERE playlist_id NOT IN (SELECT id FROM playlist)")
        
        conn.commit()
        print(f"\n✅ Successfully cleaned and renumbered all playlist tracks ({total_cleaned} entries)!")
        return True

    except Exception as e:
        print(f"\n❌ Error fixing playlists: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()

if __name__ == "__main__":
    db_file = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/PSudofy/data/navidrome.db")
    fix_playlists(db_file)
