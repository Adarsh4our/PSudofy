#!/usr/bin/env python3
"""
PSudofy Lyrics Embedder
========================
Fetches lyrics from lrclib.net (free, no API key) for every song
in the Navidrome library and embeds them into each MP3's ID3 tags.

Usage:
    python3 add_lyrics.py                  # Full run
    python3 add_lyrics.py --dry-run        # Preview only, no files modified
    python3 add_lyrics.py --limit 10       # Process only first N songs (for testing)
"""

import os
import re
import sys
import time
import json
import sqlite3
import argparse
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path
from datetime import datetime
from typing import Optional, Tuple

# ── Optional Rich UI ──────────────────────────────────────────────────────────
try:
    from rich.console import Console
    from rich.progress import (
        Progress, SpinnerColumn, BarColumn,
        TextColumn, MofNCompleteColumn, TimeRemainingColumn
    )
    from rich.panel import Panel
    from rich.table import Table
    from rich import box
    RICH = True
    console = Console()
except ImportError:
    RICH = False
    class _FallbackConsole:
        def print(self, *a, **kw): print(*a)
    console = _FallbackConsole()

# ── Mutagen ───────────────────────────────────────────────────────────────────
try:
    from mutagen.mp3 import MP3
    from mutagen.id3 import ID3, USLT, SYLT, Encoding, ID3NoHeaderError
    MUTAGEN = True
except ImportError:
    MUTAGEN = False
    console.print("[bold red]Error: mutagen is not installed. Run: sudo apt-get install python3-mutagen[/bold red]")
    sys.exit(1)

# ── Config ────────────────────────────────────────────────────────────────────
DB_PATH     = Path("~/PSudofy/data/navidrome.db").expanduser()
MUSIC_ROOT  = Path("~/PSudofy/music").expanduser()
LOG_PATH    = Path("~/PSudofy/lyrics_log.json").expanduser()
API_BASE    = "https://lrclib.net/api/get"
DELAY       = 0.5   # seconds between API calls — respectful to lrclib.net
TIMEOUT     = 8     # HTTP request timeout

# ── Helpers ───────────────────────────────────────────────────────────────────

def clean_title(title: str) -> str:
    """Strip common suffix noise for better API matching."""
    t = re.sub(r"\s*\(from\s+[^)]+\)", "", title, flags=re.IGNORECASE)
    t = re.sub(r"\s*-\s*from\s+.+", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*\(feat\.?\s+[^)]+\)", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*\(ft\.?\s+[^)]+\)", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*\(with\s+[^)]+\)", "", t, flags=re.IGNORECASE)
    return t.strip()

def primary_artist(artist_str: str) -> str:
    """Return just the first/primary artist from a slash-separated list."""
    if not artist_str:
        return ""
    return re.split(r"[/,&]|\bfeat\b|\bft\b", artist_str, maxsplit=1)[0].strip()

def fetch_lyrics(title: str, artist: str, album: str, duration: float) -> Tuple[Optional[str], Optional[str]]:
    """
    Query lrclib.net for lyrics.
    Returns (plain_lyrics, synced_lyrics) — either may be None.
    """
    params = {
        "track_name":  clean_title(title),
        "artist_name": primary_artist(artist),
        "duration":    int(duration) if duration else 0,
    }
    if album:
        params["album_name"] = album

    url = f"{API_BASE}?{urllib.parse.urlencode(params)}"

    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "PSudofy-LyricsEmbedder/1.0 (github.com/PSudofy)"}
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                plain  = data.get("plainLyrics") or None
                synced = data.get("syncedLyrics") or None
                return plain, synced
            return None, None
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, None   # No lyrics found — normal
        raise
    except Exception:
        return None, None


def parse_lrc(lrc: str):
    """
    Parse an LRC string into SYLT-compatible list of (text, timestamp_ms) tuples.
    LRC format: [mm:ss.xx] lyric line
    """
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


def embed_lyrics(mp3_path: Path, plain: Optional[str], synced: Optional[str]) -> bool:
    """
    Embed lyrics into the MP3 file's ID3 tags.
    Returns True on success.
    """
    try:
        try:
            tags = ID3(str(mp3_path))
        except ID3NoHeaderError:
            tags = ID3()

        if plain:
            tags.delall("USLT")
            tags.add(USLT(
                encoding=Encoding.UTF8,
                lang="eng",
                desc="",
                text=plain
            ))

        if synced:
            parsed = parse_lrc(synced)
            if parsed:
                tags.delall("SYLT")
                tags.add(SYLT(
                    encoding=Encoding.UTF8,
                    lang="eng",
                    format=2,       # milliseconds
                    type=1,         # lyrics
                    desc="",
                    text=parsed
                ))

        tags.save(str(mp3_path))
        return True
    except Exception as e:
        return False


def resolve_path(db_path_field: str) -> Optional[Path]:
    """
    Convert the path stored in Navidrome DB to an absolute filesystem path.
    Navidrome stores paths relative to the music root.
    """
    # Try as absolute path first
    p = Path(db_path_field)
    if p.exists():
        return p

    # Strip leading slashes and try relative to music root
    relative = db_path_field.lstrip("/")
    candidate = MUSIC_ROOT / relative
    if candidate.exists():
        return candidate

    # Try just the filename portion relative to MUSIC_ROOT (deep search not feasible)
    return None


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Embed lyrics into PSudofy MP3 library")
    parser.add_argument("--dry-run", action="store_true", help="Fetch but don't write to files")
    parser.add_argument("--limit",   type=int, default=0,  help="Process only first N songs")
    parser.add_argument("--skip-existing", action="store_true", help="Skip songs that already have USLT tags")
    args = parser.parse_args()

    if not DB_PATH.exists():
        console.print(f"[red]Database not found at {DB_PATH}[/red]")
        sys.exit(1)

    # ── Load songs from DB ────────────────────────────────────────────────────
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, title, artist, album, duration, path
        FROM media_file
        WHERE missing = 0
        ORDER BY artist COLLATE NOCASE, album COLLATE NOCASE, title COLLATE NOCASE
    """)
    songs = cursor.fetchall()
    conn.close()

    if args.limit:
        songs = songs[:args.limit]

    total = len(songs)
    if RICH:
        console.print()
        console.print(Panel(
            f"[bold cyan]PSudofy Lyrics Embedder[/bold cyan]\n"
            f"[dim]Songs to process: [bold]{total}[/bold]  |  "
            f"Dry run: [bold]{'Yes' if args.dry_run else 'No'}[/bold]  |  "
            f"Source: lrclib.net[/dim]",
            border_style="cyan"
        ))
        console.print()
    else:
        print(f"\nPSudofy Lyrics Embedder")
        print(f"Songs to process: {total} | Dry run: {'Yes' if args.dry_run else 'No'} | Source: lrclib.net\n")

    # ── Stats ─────────────────────────────────────────────────────────────────
    stats = {"found": 0, "not_found": 0, "skipped": 0, "failed": 0, "no_file": 0}
    log_entries = []
    start = time.time()

    def _run():
        if RICH:
            with Progress(
                SpinnerColumn(spinner_name="dots2", style="bold cyan"),
                TextColumn("[bold cyan]{task.description}[/bold cyan]"),
                BarColumn(bar_width=36, style="dim cyan", complete_style="bold cyan", finished_style="bold green"),
                MofNCompleteColumn(),
                TextColumn("[dim]·[/dim]"),
                TimeRemainingColumn(),
                console=console,
                transient=False,
            ) as progress:
                task = progress.add_task("Fetching lyrics…", total=total)
                for song in songs:
                    _process_song(song, args, stats, log_entries, progress, task)
        else:
            for i, song in enumerate(songs, 1):
                print(f"[{i}/{total}] {song[1]} — {song[2]}")
                _process_song(song, args, stats, log_entries, None, None)

    def _process_song(song, args, stats, log_entries, progress, task_id):
        sid, title, artist, album, duration, db_path = song

        if progress:
            progress.update(task_id, description=f"[dim]{(title or '')[:45]}[/dim]")

        # Resolve the actual file path on disk
        mp3_path = resolve_path(db_path)

        if not mp3_path:
            stats["no_file"] += 1
            log_entries.append({"title": title, "artist": artist, "status": "no_file", "path": db_path})
            if progress:
                progress.advance(task_id)
            return

        # Optionally skip songs that already have lyrics embedded
        if args.skip_existing and not args.dry_run:
            try:
                existing = ID3(str(mp3_path))
                if existing.getall("USLT"):
                    stats["skipped"] += 1
                    if progress:
                        progress.advance(task_id)
                    return
            except Exception:
                pass

        # Fetch lyrics from lrclib.net
        try:
            plain, synced = fetch_lyrics(title or "", artist or "", album or "", duration or 0)
        except Exception as e:
            stats["failed"] += 1
            log_entries.append({"title": title, "artist": artist, "status": "error", "error": str(e)})
            if progress:
                progress.advance(task_id)
            time.sleep(DELAY)
            return

        if plain or synced:
            # Embed into MP3
            if not args.dry_run:
                ok = embed_lyrics(mp3_path, plain, synced)
            else:
                ok = True  # Dry run always "succeeds"

            if ok:
                stats["found"] += 1
                has_synced = "✓ synced" if synced else "plain only"
                console.print(f"  [green]✅[/green] {title} — {primary_artist(artist or '')} [dim]({has_synced})[/dim]")
                log_entries.append({"title": title, "artist": artist, "status": "found",
                                    "synced": bool(synced), "plain": bool(plain)})
            else:
                stats["failed"] += 1
                console.print(f"  [red]❌[/red] Failed to write: {title}")
                log_entries.append({"title": title, "artist": artist, "status": "write_failed"})
        else:
            stats["not_found"] += 1
            console.print(f"  [yellow]⚠️[/yellow]  No lyrics: [dim]{title} — {primary_artist(artist or '')}[/dim]")
            log_entries.append({"title": title, "artist": artist, "status": "not_found"})

        if progress:
            progress.advance(task_id)

        time.sleep(DELAY)

    _run()

    # ── Summary ───────────────────────────────────────────────────────────────
    elapsed = time.time() - start
    mins, secs = divmod(int(elapsed), 60)

    if RICH:
        from rich.align import Align
        console.print()
        table = Table(box=box.ROUNDED, border_style="cyan", show_header=False, padding=(0, 3))
        table.add_column(style="bold cyan", justify="left")
        table.add_column(justify="right")
        table.add_row("✅  Lyrics embedded",    f"[bold green]{stats['found']}[/bold green]")
        table.add_row("⚠️   Not found",          f"[bold yellow]{stats['not_found']}[/bold yellow]")
        table.add_row("⏭️   Already had lyrics", f"[bold blue]{stats['skipped']}[/bold blue]")
        table.add_row("❌  Failed",             f"[bold red]{stats['failed']}[/bold red]")
        table.add_row("🔍  File not found",     f"[bold red]{stats['no_file']}[/bold red]")
        table.add_row("🎵  Total processed",    f"[bold white]{total}[/bold white]")
        table.add_row("⏱️   Time taken",         f"[bold cyan]{mins}m {secs}s[/bold cyan]")
        console.print(Align.center(Panel(table, title="[bold cyan]✨  Lyrics Embedding Complete[/bold cyan]", border_style="cyan", padding=(1, 4))))
    else:
        print(f"\n--- Lyrics Embedding Complete ---")
        print(f"  Lyrics embedded  : {stats['found']}")
        print(f"  Not found        : {stats['not_found']}")
        print(f"  Already had lyrics: {stats['skipped']}")
        print(f"  Failed           : {stats['failed']}")
        print(f"  File not found   : {stats['no_file']}")
        print(f"  Total processed  : {total}")
        print(f"  Time taken       : {mins}m {secs}s")

    # ── Write log ─────────────────────────────────────────────────────────────
    log_data = {
        "run_at": datetime.now().isoformat(),
        "dry_run": args.dry_run,
        "stats": stats,
        "songs": log_entries
    }
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(log_data, f, indent=2, ensure_ascii=False)
    console.print(f"\n[dim cyan]Full log saved to: {LOG_PATH}[/dim cyan]")


if __name__ == "__main__":
    main()
