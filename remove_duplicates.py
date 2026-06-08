import os
import re
import sqlite3
import urllib.request
import urllib.parse
from pathlib import Path
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.prompt import Confirm
from rich import print as rprint

load_dotenv()

# Constants
MUSIC_FOLDER = os.getenv("MUSIC_FOLDER", "./music")
NAVIDROME_URL = os.getenv("NAVIDROME_URL", "http://localhost:4533")
NAVI_USER = os.getenv("NAVI_USER", "")
NAVI_PASS = os.getenv("NAVI_PASS", "")

# Force utf-8 encoding for Rich console to avoid Windows cp1252 errors
console = Console(force_terminal=True, color_system="auto")

def normalize_string(s: str) -> str:
    """Normalize song title or artist name for soft matching."""
    if not s:
        return ""
    # Convert to lowercase
    s = s.lower()
    # Remove common suffixes like (From "...") or (Original Motion Picture Soundtrack)
    s = re.sub(r"\s*\(from\s+[^)]+\)", "", s)
    s = re.sub(r"\s*\(original\s+motion\s+picture\s+soundtrack\)", "", s)
    s = re.sub(r"\s*\(soundtrack\)", "", s)
    s = re.sub(r"\s*-\s*from\s+.*", "", s)
    s = re.sub(r"\s*\(feat\.\s+[^)]+\)", "", s)
    s = re.sub(r"\s*\(ft\.\s+[^)]+\)", "", s)
    s = re.sub(r"\s*\(with\s+[^)]+\)", "", s)
    # Remove leading/trailing dots, spaces, punctuation
    s = re.sub(r"^[.\s]+|[.\s]+$", "", s)
    # Remove all non-alphanumeric characters
    s = re.sub(r"[^a-z0-9]", "", s)
    return s

def get_artist_set(artist_str: str) -> set:
    """Split multi-artists string and get a normalized set of individual artists."""
    if not artist_str:
        return set()
    # Split by common artist separators: /, ,, &, ft., feat.
    parts = re.split(r"\/|,|&|\bft\b|\bfeat\b", artist_str.lower())
    artists = set()
    for p in parts:
        clean = re.sub(r"[^a-z0-9]", "", p.strip())
        if clean:
            artists.add(clean)
    return artists

def find_duplicates(db_path: str = "data/navidrome.db"):
    """Query Navidrome DB and find duplicate tracks using smart metadata matching."""
    if not os.path.exists(db_path):
        console.print(f"[-] Navidrome database not found at {db_path}")
        return []

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Get media_file table info to ensure columns exist
    cursor.execute("PRAGMA table_info(media_file)")
    cols = [col[1] for col in cursor.fetchall()]

    cursor.execute("SELECT id, path, title, album, artist, duration, size, track_number, year FROM media_file")
    rows = cursor.fetchall()
    conn.close()

    tracks = []
    for r in rows:
        track = {
            "id": r[0],
            "path": r[1],
            "title": r[2],
            "album": r[3],
            "artist": r[4],
            "duration": r[5],
            "size": r[6],
            "track_number": r[7],
            "year": r[8]
        }
        track["norm_title"] = normalize_string(track["title"])
        track["artists_set"] = get_artist_set(track["artist"])
        tracks.append(track)

    duplicates_groups = []
    visited = set()

    for i in range(len(tracks)):
        if tracks[i]["id"] in visited:
            continue

        group = [tracks[i]]
        norm_title_i = tracks[i]["norm_title"]
        artists_i = tracks[i]["artists_set"]
        duration_i = tracks[i]["duration"]

        for j in range(i + 1, len(tracks)):
            if tracks[j]["id"] in visited:
                continue

            # Matching criteria:
            # 1. Normalized titles match
            # 2. Artist sets have significant overlap (e.g. at least one common artist)
            # 3. Duration difference is <= 12 seconds
            title_match = (norm_title_i == tracks[j]["norm_title"]) or (norm_title_i in tracks[j]["norm_title"]) or (tracks[j]["norm_title"] in norm_title_i)
            
            # Simple length validation (avoid empty titles matching)
            if not norm_title_i or len(norm_title_i) < 2:
                continue

            artist_match = bool(artists_i & tracks[j]["artists_set"])
            duration_match = abs(duration_i - tracks[j]["duration"]) <= 12.0

            if title_match and artist_match and duration_match:
                group.append(tracks[j])
                visited.add(tracks[j]["id"])

        if len(group) > 1:
            visited.add(tracks[i]["id"])
            duplicates_groups.append(group)

    return duplicates_groups

def choose_best_track(group):
    """
    Select the 'best' track to keep based on heuristics:
    1. Prefer tracks with cleaner titles (no dots, no 'from' in title if possible, or standard titles).
    2. Prefer larger file sizes (better quality/bitrate).
    """
    # Sort group by clean title first, then by size descending
    def track_score(t):
        score = 0
        # Deduct score if title has suspicious dots at the beginning or is unusually formatted
        if t["title"].startswith("..."):
            score -= 10
        if "from" in t["title"].lower():
            # Sometimes 'from' title is good, but standard title is cleaner
            score -= 2
        # Add file size to score (normalized to a reasonable range)
        score += (t["size"] / 1024 / 1024)  # Size in MB
        return score

    sorted_group = sorted(group, key=track_score, reverse=True)
    best = sorted_group[0]
    duplicates_to_remove = sorted_group[1:]
    return best, duplicates_to_remove

def trigger_navidrome_scan():
    """Tells Navidrome to scan the music folder right now."""
    if not NAVI_USER or not NAVI_PASS:
        return False
    try:
        import secrets
        import hashlib
        salt = secrets.token_hex(8)
        token = hashlib.md5((NAVI_PASS + salt).encode()).hexdigest()
        params = urllib.parse.urlencode({
            "u": NAVI_USER,
            "t": token,
            "s": salt,
            "v": "1.16.1",
            "c": "psudofy",
            "f": "json",
        })
        urllib.request.urlopen(
            f"{NAVIDROME_URL}/rest/startScan.view?{params}",
            timeout=5,
        )
        return True
    except Exception:
        return False

def clean_duplicates(dry_run: bool = True):
    """Scan and clean duplicates."""
    action_word = "Dry Run" if dry_run else "Execution"
    console.print(Panel(f"[bold cyan]Scanning Navidrome Music Library for Duplicates ({action_word})[/bold cyan]", border_style="cyan"))

    groups = find_duplicates()
    if not groups:
        console.print("[bold green]No duplicates found in your music library![/bold green]")
        return

    console.print(f"[yellow]Found {len(groups)} duplicate song groups![/yellow]\n")

    total_removed_size = 0
    removed_count = 0

    for idx, group in enumerate(groups, 1):
        best, to_remove = choose_best_track(group)
        
        table = Table(title=f"Group {idx}: {best['title']} by {best['artist']}", show_header=True, header_style="bold magenta", box=None)
        table.add_column("Action", style="bold")
        table.add_column("Title")
        table.add_column("Album")
        table.add_column("Artist")
        table.add_column("Duration")
        table.add_column("Size (MB)")
        table.add_column("Path")

        # Add the best one (KEEP)
        mins, secs = divmod(int(best['duration']), 60)
        table.add_row(
            "[green]KEEP[/green]",
            best['title'],
            best['album'],
            best['artist'],
            f"{mins:02d}:{secs:02d}",
            f"{best['size'] / 1024 / 1024:.2f}",
            best['path']
        )

        # Add duplicates to remove
        for t in to_remove:
            mins, secs = divmod(int(t['duration']), 60)
            table.add_row(
                "[red]DELETE[/red]",
                t['title'],
                t['album'],
                t['artist'],
                f"{mins:02d}:{secs:02d}",
                f"{t['size'] / 1024 / 1024:.2f}",
                t['path']
            )
            total_removed_size += t['size']
            removed_count += 1

        console.print(table)
        console.print()

    mb_saved = total_removed_size / 1024 / 1024
    console.print(f"[bold cyan]Total duplicates to delete: {removed_count} files ({mb_saved:.2f} MB saved)[/bold cyan]")

    if dry_run:
        console.print("\n[dim cyan]To actually delete these duplicates, run the script with: python remove_duplicates.py --commit[/dim cyan]")
        return

    # Commit action
    if not Confirm.ask("[bold yellow]Are you sure you want to delete these duplicate files from disk?[/bold yellow]"):
        console.print("[yellow]Operation cancelled. No files were deleted.[/yellow]")
        return

    console.print("\n[bold cyan]Deleting duplicate files...[/bold cyan]")
    deleted_count = 0
    for group in groups:
        best, to_remove = choose_best_track(group)
        for t in to_remove:
            # Construct absolute path to the file
            # In database, path is relative to the music folder
            file_path = os.path.join(MUSIC_FOLDER, t["path"])
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                    console.print(f"  [green]*[/green] Deleted: {t['path']}")
                    deleted_count += 1
                except Exception as e:
                    console.print(f"  [red]Failed to delete {t['path']}: {e}[/red]")
            else:
                # Let's try direct path
                if os.path.exists(t["path"]):
                    try:
                        os.remove(t["path"])
                        console.print(f"  [green]*[/green] Deleted: {t['path']}")
                        deleted_count += 1
                    except Exception as e:
                        console.print(f"  [red]Failed to delete {t['path']}: {e}[/red]")
                else:
                    console.print(f"  [yellow]File not found on disk: {t['path']}[/yellow]")

    console.print(f"\n[bold green]Successfully deleted {deleted_count} duplicate files![/bold green]")
    
    console.print("[cyan]Triggering Navidrome library scan to update database...[/cyan]")
    if trigger_navidrome_scan():
        console.print("[bold green]Navidrome scan triggered successfully. Database will update in seconds![/bold green]")
    else:
        console.print("[yellow]Could not trigger Navidrome scan automatically. Please click refresh in Navidrome Web UI.[/yellow]")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Clean duplicate tracks in PSudofy music library")
    parser.add_argument("--commit", action="store_true", help="Actually delete duplicate files from disk")
    args = parser.parse_args()

    clean_duplicates(dry_run=not args.commit)
