#!/usr/bin/env python3
"""
PSudofy Downloader
==================
Self-hosted music downloader with beautiful terminal UI.
Supports Spotify and YouTube Music playlists.
"""

import os
import sys
import time
import threading
import subprocess
import urllib.request
import urllib.parse
import re

import yt_dlp
from rich.console import Console
from rich.progress import (
    Progress,
    SpinnerColumn,
    BarColumn,
    TextColumn,
    TimeRemainingColumn,
    MofNCompleteColumn,
)
from rich.panel import Panel
from rich.table import Table
from rich import box
from rich.align import Align
from rich.rule import Rule
from rich.text import Text

# ─── Constants ───────────────────────────────────────────────────────────────

MUSIC_FOLDER = "./music"
ARCHIVE_FILE = "downloaded_yt.txt"   # Tracks already-downloaded YouTube songs

# ─── Navidrome ───────────────────────────────────────────────────────────────

NAVIDROME_URL = "http://localhost:4533"
NAVI_USER     = "adarsh4our"
NAVI_PASS     = "adarsh_@1234"

# ─── Console ─────────────────────────────────────────────────────────────────

console = Console()

# ─── Banner ──────────────────────────────────────────────────────────────────

BANNER = (
    "[bold cyan]\n"
    "██████╗ ███████╗██╗   ██╗██████╗  ██████╗ ███████╗██╗   ██╗\n"
    "██╔══██╗██╔════╝██║   ██║██╔══██╗██╔═══██╗██╔════╝╚██╗ ██╔╝\n"
    "██████╔╝███████╗██║   ██║██║  ██║██║   ██║█████╗   ╚████╔╝ \n"
    "██╔═══╝ ╚════██║██║   ██║██║  ██║██║   ██║██╔══╝    ╚██╔╝  \n"
    "██║     ███████║╚██████╔╝██████╔╝╚██████╔╝██║        ██║   \n"
    "╚═╝     ╚══════╝ ╚═════╝ ╚═════╝  ╚═════╝ ╚═╝        ╚═╝   \n"
    "[/bold cyan]"
    "[dim cyan]          ✦  Your Self-Hosted Music Network  ✦[/dim cyan]"
)


def print_banner():
    console.clear()
    console.print(Align.center(Text.from_markup(BANNER)))
    console.print(Rule(style="dim cyan"))
    console.print()


# ─── Shared UI Helpers ────────────────────────────────────────────────────────

def make_progress() -> Progress:
    """Create a consistently styled Rich Progress bar."""
    return Progress(
        SpinnerColumn(spinner_name="dots2", style="bold cyan"),
        TextColumn("[bold cyan]{task.description}[/bold cyan]", justify="left"),
        BarColumn(
            bar_width=38,
            style="dim cyan",
            complete_style="bold cyan",
            finished_style="bold green",
        ),
        MofNCompleteColumn(),
        TextColumn("[dim]·[/dim]"),
        TimeRemainingColumn(),
        console=console,
        transient=False,
    )


def show_summary(stats: dict, elapsed: float):
    """Render a final summary box after all downloads finish."""
    mins, secs = divmod(int(elapsed), 60)
    time_str = f"{mins}m {secs}s" if mins else f"{secs}s"

    table = Table(
        box=box.ROUNDED,
        border_style="cyan",
        padding=(0, 3),
        show_header=False,
    )
    table.add_column(style="bold cyan", justify="left")
    table.add_column(justify="right")

    table.add_row("✅  Downloaded", f"[bold green]{stats['downloaded']}[/bold green]")
    table.add_row("⚠️   Skipped",    f"[bold yellow]{stats['skipped']}[/bold yellow]")
    table.add_row("❌  Failed",      f"[bold red]{stats['failed']}[/bold red]")
    table.add_row("🎵  Total Songs", f"[bold white]{stats['total']}[/bold white]")
    table.add_row("⏱️   Time Taken",  f"[bold cyan]{time_str}[/bold cyan]")

    console.print()
    console.print(Rule(style="dim cyan"))
    console.print(
        Align.center(
            Panel(
                table,
                title="[bold cyan]✨  Download Complete[/bold cyan]",
                border_style="cyan",
                padding=(1, 4),
            )
        )
    )
    console.print()


# ─── Navidrome Auto-Scan ──────────────────────────────────────────────────────

def trigger_navidrome_scan():
    """
    Tells Navidrome to scan the music folder right now via its Subsonic API.
    New songs appear immediately — no waiting for the hourly scan.
    """
    try:
        params = urllib.parse.urlencode({
            "u": NAVI_USER,
            "p": NAVI_PASS,
            "v": "1.16.1",
            "c": "psudofy",
            "f": "json",
        })
        urllib.request.urlopen(
            f"{NAVIDROME_URL}/rest/startScan.view?{params}",
            timeout=5,
        )
        console.print(
            "[cyan]✓[/cyan]  Navidrome is scanning for new songs… "
            "[dim](they'll appear in seconds)[/dim]"
        )
    except Exception:
        console.print(
            "[yellow]⚠️[/yellow]  Could not reach Navidrome — "
            "[dim]open it and click the 🔄 refresh icon manually.[/dim]"
        )


# ─── YouTube Music Downloader ─────────────────────────────────────────────────

def download_youtube(url: str, workers: int = 3):
    """
    Download a YouTube / YouTube Music playlist or track.
    Uses parallel workers for significantly faster downloads.
    """
    from concurrent.futures import ThreadPoolExecutor

    console.print("[cyan]⟳[/cyan]  Fetching playlist info…")
    flat_opts = {"quiet": True, "extract_flat": True, "skip_download": True}
    with yt_dlp.YoutubeDL(flat_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if info.get("_type") == "playlist":
        entries        = [e for e in info.get("entries", []) if e]
        playlist_title = info.get("title", "Playlist")
    else:
        entries        = [info]
        playlist_title = info.get("title", "Single Song")

    total = len(entries)
    stats = {"downloaded": 0, "skipped": 0, "failed": 0, "total": total}
    lock  = threading.Lock()
    start = time.time()

    console.print(
        f"[cyan]✓[/cyan]  Found [bold cyan]{total}[/bold cyan] songs "
        f"in [bold]{playlist_title}[/bold] "
        f"[dim]· downloading {workers} at a time[/dim]\n"
    )
    console.print(Rule(style="dim cyan"))

    with make_progress() as progress:
        overall_task = progress.add_task("Overall Progress", total=total)

        slots      = list(range(workers))
        slots_lock = threading.Lock()
        song_tasks = [
            progress.add_task("[dim]idle[/dim]", total=100, visible=False)
            for _ in range(workers)
        ]

        def claim_slot():
            with slots_lock:
                if slots:
                    s = slots.pop(0)
                    progress.update(song_tasks[s], visible=True, completed=0)
                    return s
            return None

        def release_slot(s):
            with slots_lock:
                progress.update(
                    song_tasks[s], visible=False,
                    description="[dim]idle[/dim]", completed=0
                )
                slots.append(s)

        def download_one(entry):
            title     = entry.get("title", "Unknown Title")
            entry_url = (
                entry.get("url")
                or entry.get("webpage_url")
                or f"https://www.youtube.com/watch?v={entry.get('id', '')}"
            )

            slot            = claim_slot()
            hook_was_called = [False]

            def prog_hook(d):
                if d["status"] == "downloading":
                    hook_was_called[0] = True
                    raw   = d.get("_percent_str", "0%").strip().rstrip("%")
                    speed = d.get("speed") or 0
                    if speed > 1_048_576:
                        spd = f"  {speed / 1_048_576:.1f} MB/s"
                    elif speed > 0:
                        spd = f"  {speed / 1024:.0f} KB/s"
                    else:
                        spd = ""
                    if slot is not None:
                        try:
                            progress.update(
                                song_tasks[slot],
                                completed=float(raw),
                                description=f"[dim]{title[:40]}[/dim][cyan]{spd}[/cyan]",
                            )
                        except ValueError:
                            pass
                elif d["status"] == "finished":
                    if slot is not None:
                        progress.update(song_tasks[slot], completed=100)

            ydl_opts = {
                "format": "bestaudio/best",
                "postprocessors": [
                    {"key": "FFmpegExtractAudio",
                     "preferredcodec": "mp3", "preferredquality": "0"},
                    {"key": "FFmpegMetadata"},
                    {"key": "EmbedThumbnail"},
                ],
                "outtmpl": (
                    f"{MUSIC_FOLDER}"
                    "/%(artist,uploader)s"
                    "/%(album,playlist_title,uploader)s"
                    "/%(title)s.%(ext)s"
                ),
                "download_archive": ARCHIVE_FILE,
                "progress_hooks":   [prog_hook],
                "writethumbnail":   True,
                "quiet":            True,
                "no_warnings":      True,
            }

            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([entry_url])

                with lock:
                    if hook_was_called[0]:
                        stats["downloaded"] += 1
                        console.print(f"  [green]✅[/green] {title}")
                    else:
                        stats["skipped"] += 1
                        console.print(f"  [yellow]⚠️[/yellow]  Skipped: [dim]{title}[/dim]")
                    progress.update(overall_task, advance=1)

            except Exception as e:
                with lock:
                    stats["failed"] += 1
                    console.print(f"  [red]❌[/red] Failed: {title} — [dim]{str(e)[:70]}[/dim]")
                    progress.update(overall_task, advance=1)

            finally:
                if slot is not None:
                    release_slot(slot)

        with ThreadPoolExecutor(max_workers=workers) as executor:
            list(executor.map(download_one, entries))

    show_summary(stats, time.time() - start)
    trigger_navidrome_scan()


# ─── Spotify Downloader ───────────────────────────────────────────────────────

def download_spotify(url: str):
    """
    Download a Spotify playlist or track using spotDL.

    Key design:
    - spotDL is launched immediately (no blocking pre-scan).
    - Simultaneously, a background thread fetches the full playlist via spotapi
      to identify songs that spotDL will silently skip (archive hits).
    - When spotDL prints "Found X songs", we wait up to 15 s for the pre-fetch
      to finish, then print the silently-skipped songs by name.
    - This means the pre-fetch adds ZERO wall-clock overhead.
    """
    output_template = f"{MUSIC_FOLDER}/{{artist}}/{{album}}/{{title}} - {{artist}}.{{ext}}"

    stats = {"downloaded": 0, "skipped": 0, "failed": 0, "total": 0}
    start = time.time()

    console.print("[cyan]⟳[/cyan]  Connecting to Spotify via spotDL…\n")
    console.print(Rule(style="dim cyan"))

    archive_file = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "downloaded_spotify.txt"
    )
    cmd = [
        "spotdl", "download", url,
        "--output", output_template,
        "--archive", archive_file,
        "--threads", "4",
    ]

    # Prevent spotDL / rich from wrapping long lines in its stdout
    env = os.environ.copy()
    env["COLUMNS"] = "10000"
    env["TERM"]    = "dumb"

    # ── Load archive from disk (fast local read) ──────────────────────────────
    archived_urls: set = set()
    if os.path.exists(archive_file):
        with open(archive_file, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                s = line.strip()
                if s:
                    archived_urls.add(s)

    # ── Launch spotDL immediately ─────────────────────────────────────────────
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
    )

    # ── Pre-fetch playlist in parallel with spotDL ────────────────────────────
    # Both this thread and spotDL contact Spotify at the same time, so the
    # pre-fetch adds no extra wait time compared to running spotDL alone.
    playlist_tracks: dict = {}      # { spotify_track_url: "Song — Artist" }
    _prefetch_done = threading.Event()

    def _prefetch():
        try:
            import spotapi  # type: ignore
            playlist_id = url.split("/")[-1].split("?")[0]
            pub = spotapi.PublicPlaylist(playlist_id)
            for chunk in pub.paginate_playlist():
                for item in chunk.get("items", []):
                    try:
                        td  = item.get("itemV3", {}).get("data", {})
                        uri = td.get("uri", "")
                        if not uri.startswith("spotify:track:"):
                            continue
                        track_url = (
                            "https://open.spotify.com/track/"
                            + uri.removeprefix("spotify:track:")
                        )
                        name         = td.get("identityTrait", {}).get("name", "Unknown")
                        contributors = (
                            td.get("identityTrait", {})
                            .get("contributors", {})
                            .get("items", [])
                        )
                        artist = (
                            contributors[0].get("profile", {}).get("name", "")
                            if contributors else ""
                        )
                        playlist_tracks[track_url] = (
                            f"{name} — {artist}" if artist else name
                        )
                    except Exception:
                        pass
        except Exception:
            pass
        finally:
            _prefetch_done.set()

    threading.Thread(target=_prefetch, daemon=True).start()

    # ── Helpers ───────────────────────────────────────────────────────────────
    last_downloading = "Unknown Song"
    history          = [(time.time(), 0)]
    _done_flag       = [False]

    def _rolling_rate(processed: int) -> float:
        """15-second rolling average songs/min."""
        now = time.time()
        history.append((now, processed))
        while len(history) > 2 and history[1][0] < now - 15.0:
            history.pop(0)
        if len(history) >= 2:
            dt = history[-1][0] - history[0][0]
            ds = history[-1][1] - history[0][1]
            if dt > 0.5:
                return (ds / dt) * 60
        return 0.0

    def _advance(progress, task_id):
        if stats["total"]:
            processed = stats["downloaded"] + stats["skipped"] + stats["failed"]
            rate = _rolling_rate(processed)
            progress.update(
                task_id, advance=1,
                description=f"Overall Progress  [dim cyan]{rate:.1f} songs/min[/dim cyan]",
            )

    def _heartbeat(progress, task_id):
        """Tick the spinner every 3 s while spotDL is running so bar looks live."""
        frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        i = 0
        while not _done_flag[0]:
            time.sleep(3)
            if _done_flag[0]:
                break
            if stats["total"]:
                processed = stats["downloaded"] + stats["skipped"] + stats["failed"]
                rate  = _rolling_rate(processed)
                frame = frames[i % len(frames)]
                try:
                    progress.update(
                        task_id,
                        description=(
                            f"Overall Progress  "
                            f"[dim cyan]{rate:.1f} songs/min  {frame}[/dim cyan]"
                        ),
                    )
                except Exception:
                    pass
                i += 1

    # ── Main read loop ────────────────────────────────────────────────────────
    with make_progress() as progress:
        overall_task = progress.add_task("Waiting for spotDL…", total=None)

        threading.Thread(
            target=_heartbeat, args=(progress, overall_task), daemon=True
        ).start()

        for raw_line in process.stdout:
            line       = raw_line.strip()
            if not line:
                continue
            line_lower = line.lower()

            # ── Total song count ─────────────────────────────────────────
            if "found" in line_lower and "song" in line_lower:
                for word in line.split():
                    if word.isdigit():
                        spotdl_total = int(word)

                        # Wait for pre-fetch (already running in parallel)
                        _prefetch_done.wait(timeout=15)

                        stats["total"] = (
                            len(playlist_tracks) if playlist_tracks else spotdl_total
                        )
                        progress.update(
                            overall_task,
                            total=stats["total"],
                            completed=0,
                            description="Overall Progress",
                        )
                        console.print(
                            f"[cyan]✓[/cyan]  Found "
                            f"[bold cyan]{stats['total']}[/bold cyan] songs\n"
                        )

                        # Print silently-skipped archive songs immediately
                        silent_skips = [
                            display
                            for track_url, display in playlist_tracks.items()
                            if track_url in archived_urls
                        ]
                        for name in silent_skips:
                            stats["skipped"] += 1
                            console.print(
                                f"  [yellow]⚠️[/yellow]  "
                                f"Skipped [dim](archive)[/dim]: [dim]{name}[/dim]"
                            )
                            _advance(progress, overall_task)
                        break

            # ── Downloaded ───────────────────────────────────────────────
            elif (
                line_lower.startswith("downloaded")
                or ("downloaded" in line_lower and '"' in line)
            ):
                name    = line.split('"')[1] if '"' in line else line
                parts   = name.split(" - ", 1)
                display = f"{parts[1]} \u2014 {parts[0]}" if len(parts) == 2 else name
                stats["downloaded"] += 1
                console.print(f"  [green]\u2705[/green] {display}")
                _advance(progress, overall_task)

            # ── Skipped (file exists / duplicate) ────────────────────────
            elif "skipping" in line_lower:
                clean = re.sub(r"(?i)^skipping\s+", "", line)
                clean = re.sub(r"(?i)\s*\(file already exists\)\s*", "", clean)
                clean = re.sub(r"(?i)\s*\(duplicate\)\s*", "", clean)
                clean = clean.strip(' "')
                parts   = clean.split(" - ", 1)
                display = f"{parts[1]} \u2014 {parts[0]}" if len(parts) == 2 else clean
                stats["skipped"] += 1
                console.print(f"  [yellow]⚠️[/yellow]  Skipped: [dim]{display}[/dim]")
                _advance(progress, overall_task)

            # ── Failed ───────────────────────────────────────────────────
            elif "failed" in line_lower or "error" in line_lower:
                if "fetch secrets" in line_lower or "thetadev" in line_lower:
                    console.print(
                        "  [dim]⚡ Using fallback Spotify credentials (harmless)[/dim]"
                    )
                    continue
                name   = line.split('"')[1] if '"' in line else last_downloading
                reason = line if '"' not in line else ""
                stats["failed"] += 1
                if reason:
                    console.print(
                        f"  [red]❌[/red] Failed: {name} — [dim]{reason[:80]}[/dim]"
                    )
                else:
                    console.print(f"  [red]❌[/red] Failed: {name}")
                _advance(progress, overall_task)

            # ── Currently downloading (live label) ───────────────────────
            elif "downloading" in line_lower and '"' in line:
                name = line.split('"')[1]
                last_downloading = name
                progress.update(
                    overall_task,
                    description=(
                        f"[bold cyan]Downloading:[/bold cyan] "
                        f"[italic]{name[:45]}[/italic]"
                    ),
                )

        _done_flag[0] = True
        process.wait()

        # Safety net: songs still unaccounted for after the run
        actual = stats["downloaded"] + stats["skipped"] + stats["failed"]
        if stats["total"] > 0 and stats["total"] > actual:
            remaining = stats["total"] - actual
            stats["skipped"] += remaining
            progress.update(
                overall_task, completed=stats["total"],
                description="Overall Progress",
            )
            console.print(
                f"  [dim]ℹ️  {remaining} additional songs silently skipped via archive[/dim]"
            )
        elif stats["total"] == 0:
            stats["total"] = actual

    show_summary(stats, time.time() - start)
    trigger_navidrome_scan()


# ─── Entry Point ─────────────────────────────────────────────────────────────

def main():
    os.makedirs(MUSIC_FOLDER, exist_ok=True)
    print_banner()

    url = console.input(
        "[bold cyan]🎵  Paste a Spotify or YouTube Music URL: [/bold cyan]"
    ).strip()

    if not url:
        console.print("[red]No URL provided. Exiting.[/red]")
        return

    console.print()

    if "spotify.com" in url:
        download_spotify(url)
    elif "youtube.com" in url or "youtu.be" in url:
        download_youtube(url)
    else:
        console.print(
            "[red]❌  Unsupported URL.[/red] "
            "[dim]Please provide a Spotify or YouTube Music link.[/dim]"
        )


if __name__ == "__main__":
    main()
