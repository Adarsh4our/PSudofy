import os
import sys
import json
import csv
import sqlite3
import shutil
import tarfile
import argparse
import subprocess
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

PROJECT_DIR = Path(r"C:\Users\Adarsh Singh\PSudofy")
load_dotenv(PROJECT_DIR / ".env")

SSH_KEY_PATH = os.getenv("SSH_KEY_PATH", r"C:\Users\Adarsh Singh\Desktop\ssh-key-2026-07-28.key")
SSH_HOST = os.getenv("SSH_HOST", "ubuntu@168.138.114.54")
REMOTE_DIR = os.getenv("REMOTE_DIR", "~/PSudofy")
BACKUP_BASE_DIR = PROJECT_DIR / "backups"

def get_timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")

def pull_remote_db(backup_dir: Path) -> Path:
    print(f"[*] Fetching latest navidrome.db from remote server ({SSH_HOST})...")
    remote_db_path = f"{REMOTE_DIR.replace('~', '/home/ubuntu')}/data/navidrome.db"
    dest_db = backup_dir / "navidrome.db"
    
    cmd = [
        "scp",
        "-i", SSH_KEY_PATH,
        f"{SSH_HOST}:{remote_db_path}",
        str(dest_db)
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"[!] Warning: Remote SCP failed: {res.stderr.strip()}")
        print("[*] Falling back to local data/navidrome.db if present...")
        local_db = PROJECT_DIR / "data" / "navidrome.db"
        if local_db.exists():
            shutil.copy2(local_db, dest_db)
            return dest_db
        else:
            raise FileNotFoundError("Could not retrieve remote or local navidrome.db")
    else:
        print(f"[+] Successfully downloaded remote navidrome.db ({dest_db.stat().st_size / (1024*1024):.2f} MB)")
    return dest_db

def extract_metadata_and_playlists(db_path: Path, backup_dir: Path):
    print("[*] Exporting metadata catalog and playlists from database...")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # 1. Fetch all media files & user annotations (ratings, play counts, stars)
    query = """
        SELECT 
            mf.id,
            mf.path,
            mf.title,
            mf.album,
            mf.artist,
            mf.album_artist,
            mf.genre,
            mf.year,
            mf.track_number,
            mf.disc_number,
            mf.duration,
            mf.bit_rate,
            mf.size,
            mf.suffix,
            mf.has_cover_art,
            CASE WHEN mf.lyrics IS NOT NULL AND LENGTH(mf.lyrics) > 0 THEN 1 ELSE 0 END AS has_lyrics,
            COALESCE(an.play_count, 0) AS play_count,
            COALESCE(an.starred, 0) AS starred,
            COALESCE(an.rating, 0) AS rating,
            an.play_date
        FROM media_file mf
        LEFT JOIN annotation an ON an.item_id = mf.id AND an.item_type = 'media_file'
        ORDER BY mf.artist COLLATE NOCASE, mf.album COLLATE NOCASE, mf.track_number, mf.title COLLATE NOCASE
    """
    
    rows = cur.execute(query).fetchall()
    tracks = [dict(row) for row in rows]
    
    # Save JSON catalog
    json_path = backup_dir / "all_tracks_metadata.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(tracks, f, indent=2, ensure_ascii=False)
    print(f"[+] Exported {len(tracks)} tracks to {json_path.name}")

    # Save CSV catalog (for Excel / Spreadsheet)
    csv_path = backup_dir / "all_tracks_metadata.csv"
    if tracks:
        keys = list(tracks[0].keys())
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=keys)
            writer.writeheader()
            writer.writerows(tracks)
        print(f"[+] Exported track metadata spreadsheet to {csv_path.name}")

    # 2. Fetch playlists and tracks
    playlists_dir = backup_dir / "playlists"
    playlists_dir.mkdir(parents=True, exist_ok=True)

    playlists_query = """
        SELECT id, name, comment, duration, song_count, public, created_at, updated_at
        FROM playlist
        ORDER BY name COLLATE NOCASE
    """
    playlists = cur.execute(playlists_query).fetchall()
    
    all_playlists_data = []
    for pl in playlists:
        pl_dict = dict(pl)
        pl_tracks_query = """
            SELECT 
                pt.id AS position,
                mf.title,
                mf.artist,
                mf.album,
                mf.path,
                mf.duration
            FROM playlist_tracks pt
            JOIN media_file mf ON pt.media_file_id = mf.id
            WHERE pt.playlist_id = ?
            ORDER BY pt.id ASC
        """
        pl_tracks = [dict(r) for r in cur.execute(pl_tracks_query, (pl["id"],)).fetchall()]
        pl_dict["tracks"] = pl_tracks
        all_playlists_data.append(pl_dict)

        # Write standard .m3u8 playlist file
        safe_name = "".join(c for c in pl["name"] if c.isalnum() or c in (" ", "_", "-", "(", ")")).strip()
        if not safe_name:
            safe_name = f"playlist_{pl['id']}"
        m3u_file = playlists_dir / f"{safe_name}.m3u8"
        with open(m3u_file, "w", encoding="utf-8") as m3u:
            m3u.write("#EXTM3U\n")
            for t in pl_tracks:
                dur = int(t.get("duration") or 0)
                m3u.write(f"#EXTINF:{dur},{t.get('artist', 'Unknown')} - {t.get('title', 'Unknown')}\n")
                m3u.write(f"{t.get('path')}\n")

    pl_json_path = backup_dir / "all_playlists.json"
    with open(pl_json_path, "w", encoding="utf-8") as f:
        json.dump(all_playlists_data, f, indent=2, ensure_ascii=False)
    print(f"[+] Exported {len(all_playlists_data)} playlists to JSON and M3U8 files in playlists/")

    conn.close()

def backup_music_files(backup_dir: Path, is_remote: bool = False):
    music_backup_tar = backup_dir / "music_files_backup.tar.gz"
    print("[*] Archiving audio files into compressed package...")

    if is_remote:
        print(f"[*] Creating tar archive directly on remote server ({SSH_HOST})...")
        remote_music_path = f"{REMOTE_DIR.replace('~', '/home/ubuntu')}/music"
        remote_tar = "/tmp/music_backup.tar.gz"
        ssh_cmd = [
            "ssh",
            "-i", SSH_KEY_PATH,
            SSH_HOST,
            f"tar -czf {remote_tar} -C {remote_music_path} ."
        ]
        subprocess.run(ssh_cmd, check=True)
        print("[*] Downloading remote music archive...")
        scp_cmd = [
            "scp",
            "-i", SSH_KEY_PATH,
            f"{SSH_HOST}:{remote_tar}",
            str(music_backup_tar)
        ]
        subprocess.run(scp_cmd, check=True)
        subprocess.run(["ssh", "-i", SSH_KEY_PATH, SSH_HOST, f"rm -f {remote_tar}"])
    else:
        local_music = PROJECT_DIR / "music"
        if not local_music.exists():
            print("[!] Local music folder not found, skipping audio archive.")
            return
        with tarfile.open(music_backup_tar, "w:gz") as tar:
            tar.add(local_music, arcname="music")

    print(f"[+] Music archive created: {music_backup_tar.name} ({music_backup_tar.stat().st_size / (1024*1024):.2f} MB)")

def main():
    parser = argparse.ArgumentParser(description="Backup PSudofy / Navidrome music library and metadata.")
    parser.add_argument("--remote", action="store_true", default=True, help="Fetch latest DB and files from remote server (default: True)")
    parser.add_argument("--local-only", action="store_true", help="Use local files and database instead of remote")
    parser.add_argument("--include-music", action="store_true", help="Include full audio files archive (.tar.gz)")
    args = parser.parse_args()

    use_remote = not args.local_only

    timestamp = get_timestamp()
    backup_dir = BACKUP_BASE_DIR / f"backup_{timestamp}"
    backup_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print(f" PSudofy Library & Metadata Backup [{timestamp}]")
    print("=" * 60)
    print(f"Destination: {backup_dir.resolve()}\n")

    if use_remote:
        try:
            db_path = pull_remote_db(backup_dir)
        except Exception as e:
            print(f"[!] Remote pull error: {e}")
            local_db = PROJECT_DIR / "data" / "navidrome.db"
            if local_db.exists():
                print("[*] Using local database copy...")
                db_path = backup_dir / "navidrome.db"
                shutil.copy2(local_db, db_path)
            else:
                sys.exit(1)
    else:
        local_db = PROJECT_DIR / "data" / "navidrome.db"
        if not local_db.exists():
            print(f"[!] Local database not found at {local_db}")
            sys.exit(1)
        db_path = backup_dir / "navidrome.db"
        shutil.copy2(local_db, db_path)

    extract_metadata_and_playlists(db_path, backup_dir)

    if args.include_music:
        backup_music_files(backup_dir, is_remote=use_remote)

    print("\n" + "=" * 60)
    print(" Backup Complete!")
    print("=" * 60)
    print(f"Saved to: {backup_dir.resolve()}")
    print("Contents:")
    for item in backup_dir.iterdir():
        if item.is_dir():
            print(f" - {item.name}/ ({len(list(item.iterdir()))} files)")
        else:
            print(f" - {item.name} ({item.stat().st_size / 1024:.1f} KB)")

if __name__ == "__main__":
    main()
