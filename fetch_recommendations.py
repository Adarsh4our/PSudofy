#!/usr/bin/env python3
"""
PSudofy ListenBrainz Music Recommendation Engine & Lyrics Embedder
===================================================================
Analyzes your ListenBrainz listening history, identifies your taste profile,
discovers NEW single songs NOT yet in your library, downloads them,
fetches & embeds synced/plain lyrics (lrclib + Genius), and triggers Navidrome.

Usage:
    python fetch_recommendations.py
    python fetch_recommendations.py --user YOUR_USERNAME
"""

import os
import sys
import time
import json
import re
import argparse
import urllib.request
import urllib.parse
from pathlib import Path
from dotenv import load_dotenv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding='utf-8')

# Ensure ~/.spotdl is in PATH so yt-dlp / spotDL find ffmpeg.exe automatically
SPOTDL_FFMPEG_DIR = os.path.expanduser("~/.spotdl")
if os.path.exists(SPOTDL_FFMPEG_DIR) and SPOTDL_FFMPEG_DIR not in os.environ.get("PATH", ""):
    os.environ["PATH"] = SPOTDL_FFMPEG_DIR + os.path.pathsep + os.environ.get("PATH", "")

load_dotenv()

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box
from rich.prompt import Confirm, Prompt

console = Console(force_terminal=True)

LISTENBRAINZ_USER = os.getenv("LISTENBRAINZ_USER", "")
GENIUS_TOKEN = os.getenv("GENIUS_TOKEN", "n5vYx1OeQlrFrtX9t3zFGzYs5mNVKJRSnUv4Y3R4VTIMSd2yh-D9xk0xZxF6e536")
MUSIC_FOLDER = os.getenv("MUSIC_FOLDER", "./music")
API_BASE = "https://api.listenbrainz.org/1"
FFMPEG_PATH = os.path.join(SPOTDL_FFMPEG_DIR, "ffmpeg.exe")

COMPILATION_KEYWORDS = [
    "jukebox", "full album", "nonstop", "non-stop", "all songs",
    "compilation", "greatest hits", "best of", "audio jukebox", "6 hours", "10 hours"
]

# Mutagen import for lyrics embedding
try:
    from mutagen.id3 import ID3, USLT, SYLT, Encoding, ID3NoHeaderError
    MUTAGEN = True
except ImportError:
    MUTAGEN = False

def load_existing_library() -> set:
    """Scan local music directory and archive files to get all existing track names."""
    existing = set()
    music_dir = Path(MUSIC_FOLDER)
    if music_dir.exists():
        for p in music_dir.rglob("*.mp3"):
            name = p.stem.lower()
            existing.add(re.sub(r"[^a-z0-9]", "", name))

    for arch in ["downloaded_yt.txt", "downloaded_spotify.txt"]:
        if os.path.exists(arch):
            try:
                with open(arch, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        s = line.strip().lower()
                        if s:
                            existing.add(re.sub(r"[^a-z0-9]", "", s))
            except Exception:
                pass

    return existing

def http_get(url: str, headers: dict = None) -> tuple:
    req_headers = {"User-Agent": "PSudofy-ListenBrainz/1.0"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            if resp.status == 200:
                raw = resp.read().decode("utf-8")
                if raw.strip():
                    return json.loads(raw), 200
                return {}, 200
    except urllib.error.HTTPError as e:
        return {}, e.code
    except Exception:
        pass
    return {}, 500

def fetch_user_scrobbles(username: str, limit: int = 50) -> list:
    url = f"{API_BASE}/user/{urllib.parse.quote(username)}/listens?count={limit}"
    data, code = http_get(url)
    if code != 200 or not data:
        return []
    
    listens = data.get("payload", {}).get("listens", [])
    scrobbles = []
    for l in listens:
        tm = l.get("track_metadata", {})
        track = tm.get("track_name", "").strip()
        artist = tm.get("artist_name", "").strip()
        if track and artist:
            scrobbles.append({"title": track, "artist": artist})
    return scrobbles

SIMILAR_ARTISTS_MAP = {
    "syml": ["The Paper Kites", "Novo Amor", "Lord Huron", "Vancouver Sleep Clinic", "Sleeping At Last"],
    "arijit singh": ["Mohit Chauhan", "Jasleen Royal", "Atif Aslam", "Pritam", "B Praak"],
    "pritam": ["Arijit Singh", "Mohit Chauhan", "Amit Trivedi", "Sachin-Jigar", "Vishal-Shekhar"],
    "vishal-shekhar": ["Pritam", "Shankar-Ehsaan-Loy", "Salim-Sulaiman", "Amit Trivedi"],
    "aditya a": ["Anuv Jain", "Zaeden", "The Local Train", "Twin Strings", "Prateek Kuhad"],
    "naalayak": ["The Local Train", "When Chai Met Toast", "Anuv Jain", "Parvaaz"],
    "sonu nigam": ["Kumar Sanu", "Udit Narayan", "Shaan", "Lucky Ali", "KK"],
}

def is_compilation(title: str, duration: float = 0) -> bool:
    t_lower = title.lower()
    if duration > 600:
        return True
    for kw in COMPILATION_KEYWORDS:
        if kw in t_lower:
            return True
    return False

def discover_new_recommended_tracks(primary_artists: list, existing_library: set, count_per_artist: int = 2) -> list:
    import yt_dlp

    new_recs = []
    seen_queries = set()

    target_artists = []
    for pa in primary_artists:
        target_artists.append(pa)
        key = pa.lower()
        if key in SIMILAR_ARTISTS_MAP:
            for sa in SIMILAR_ARTISTS_MAP[key]:
                if sa not in target_artists:
                    target_artists.append(sa)

    console.print(f"[cyan]⟳ Searching YouTube Music for NEW single tracks by scrobbled & similar artists…[/cyan]\n")

    ydl_opts = {
        "quiet": True,
        "extract_flat": True,
        "skip_download": True,
        "no_warnings": True,
        "default_search": "ytsearch8:",
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        for artist in target_artists[:8]:
            query = f"ytsearch8:{artist} song official audio"
            if query in seen_queries:
                continue
            seen_queries.add(query)

            try:
                info = ydl.extract_info(query, download=False)
                entries = info.get("entries", [])
                
                added_for_artist = 0
                for e in entries:
                    if not e or added_for_artist >= count_per_artist:
                        continue
                    
                    raw_title = e.get("title", "Unknown Title")
                    duration = e.get("duration") or 0

                    if is_compilation(raw_title, duration):
                        continue

                    clean_norm = re.sub(r"[^a-z0-9]", "", raw_title.lower())

                    is_in_library = False
                    for existing_item in existing_library:
                        if len(existing_item) > 4 and (existing_item in clean_norm or clean_norm in existing_item):
                            is_in_library = True
                            break

                    if not is_in_library:
                        new_recs.append({
                            "title": raw_title,
                            "artist": artist,
                            "url": e.get("url") or f"https://www.youtube.com/watch?v={e.get('id', '')}"
                        })
                        added_for_artist += 1
                        existing_library.add(clean_norm)

            except Exception:
                pass

    return new_recs

# ── Lyrics Fetcher & Embedder ──────────────────────────────────────────────────

def clean_lyric_title(title: str) -> str:
    t = re.sub(r"\s*\(from\s+[^)]+\)", "", title, flags=re.IGNORECASE)
    t = re.sub(r"\s*-\s*from\s+.+",    "", t,     flags=re.IGNORECASE)
    t = re.sub(r"\s*\(feat\.?\s+[^)]+\)", "", t,  flags=re.IGNORECASE)
    t = re.sub(r"\s*\(ft\.?\s+[^)]+\)",   "", t,  flags=re.IGNORECASE)
    t = re.sub(r"\s*\(official.*?\)",     "", t,   flags=re.IGNORECASE)
    t = re.sub(r"\s*\[official.*?\]",     "", t,   flags=re.IGNORECASE)
    return t.strip()

def fetch_lyrics_lrclib(title: str, artist: str) -> tuple:
    params = urllib.parse.urlencode({
        "track_name": clean_lyric_title(title),
        "artist_name": artist.split('/')[0].split(',')[0].strip(),
    })
    url = f"https://lrclib.net/api/get?{params}"
    data, code = http_get(url)
    if code == 200 and data:
        return data.get("plainLyrics"), data.get("syncedLyrics")
    return None, None

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

def embed_lyrics_to_mp3(mp3_path: str, plain: str, synced: str) -> bool:
    if not MUTAGEN or not os.path.exists(mp3_path):
        return False
    try:
        try:
            tags = ID3(mp3_path)
        except ID3NoHeaderError:
            tags = ID3()

        if plain:
            tags.delall("USLT")
            tags.add(USLT(encoding=Encoding.UTF8, lang="eng", desc="", text=plain))
        if synced:
            parsed = parse_lrc(synced)
            if parsed:
                tags.delall("SYLT")
                tags.add(SYLT(encoding=Encoding.UTF8, lang="eng", format=2, type=1, desc="", text=parsed))
        tags.save(mp3_path)
        return True
    except Exception:
        return False

def fetch_and_embed_lyrics(mp3_path: str, title: str, artist: str):
    """Fetch lyrics from lrclib and embed into MP3 file."""
    plain, synced = fetch_lyrics_lrclib(title, artist)
    if plain or synced:
        ok = embed_lyrics_to_mp3(mp3_path, plain, synced)
        if ok:
            tag_type = "synced LRC" if synced else "plain text"
            console.print(f"    [green]📝 Embedded lyrics ({tag_type})[/green]")
            return True
    console.print("    [yellow]⚠️ Lyrics not found on lrclib[/yellow]")
    return False

# ── Download Logic ─────────────────────────────────────────────────────────────

def download_track_by_url(url: str, title: str, artist: str) -> bool:
    """Download a single track using yt-dlp and automatically embed lyrics."""
    import yt_dlp

    console.print(f"  [cyan]⟳ Downloading:[/cyan] [bold]{title[:55]}[/bold] [dim]({artist})[/dim]")

    output_template = os.path.join(MUSIC_FOLDER, "%(artist,uploader)s", "%(title)s.%(ext)s")
    archive_file = "downloaded_yt.txt"

    ydl_opts = {
        "format": "bestaudio/best",
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"},
            {"key": "FFmpegMetadata"},
            {"key": "EmbedThumbnail"},
        ],
        "outtmpl": output_template,
        "download_archive": archive_file,
        "writethumbnail": True,
        "quiet": True,
        "no_warnings": True,
    }

    if os.path.exists(FFMPEG_PATH):
        ydl_opts["ffmpeg_location"] = SPOTDL_FFMPEG_DIR

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            mp3_filename = os.path.splitext(filename)[0] + ".mp3"

        console.print(f"  [green]✅ Saved MP3:[/green] {title[:55]}")
        
        # Auto-fetch & embed lyrics
        fetch_and_embed_lyrics(mp3_filename, title, artist)
        return True
    except Exception as e:
        console.print(f"  [red]❌ Download failed:[/red] [dim]{str(e)[:60]}[/dim]")
        return False

def main():
    parser = argparse.ArgumentParser(description="PSudofy ListenBrainz Music Recommendation Engine")
    parser.add_argument("--user", help="ListenBrainz Username")
    args = parser.parse_args()

    user = args.user or LISTENBRAINZ_USER

    console.print()
    console.print(Panel("[bold cyan]🎧 PSudofy · ListenBrainz Discovery & Lyrics Engine[/bold cyan]", border_style="cyan"))
    console.print()

    if not user:
        user = Prompt.ask("[bold cyan]Enter your ListenBrainz username[/bold cyan]")
        if not user:
            console.print("[red]Username required.[/red]")
            sys.exit(1)

    # 1. Load existing library
    console.print("[cyan]⟳[/cyan] Indexing existing local music library to prevent duplicates…")
    existing_lib = load_existing_library()
    console.print(f"[green]✓[/green] Indexed [bold cyan]{len(existing_lib)}[/bold cyan] existing tracks in library.\n")

    # 2. Fetch user scrobbles
    console.print(f"[cyan]⟳[/cyan] Fetching listening history for [bold cyan]{user}[/bold cyan] from ListenBrainz…")
    scrobbles = fetch_user_scrobbles(user, limit=50)

    if not scrobbles:
        console.print(f"[yellow]⚠️ No listens found on ListenBrainz for '{user}'.[/yellow]")
        console.print("[dim]Make sure scrobbling is enabled in Navidrome or Symfonium![/dim]")
        sys.exit(0)

    # Display History Table
    table_history = Table(box=box.ROUNDED, border_style="cyan", padding=(0, 2), title="📻 Your Recent Server History (Scrobbles)")
    table_history.add_column("#", style="bold dim", justify="right")
    table_history.add_column("Played Song", style="bold white")
    table_history.add_column("Artist", style="bold cyan")
    table_history.add_column("Status in Library", style="bold green", justify="center")

    for idx, item in enumerate(scrobbles[:6], 1):
        table_history.add_row(str(idx), item["title"], item["artist"], "✓ Already in Server")

    console.print(table_history)
    console.print()

    # Extract primary artists
    artists = list(dict.fromkeys([
        re.split(r"[/,&]|\bfeat\b|\bft\b", s["artist"], maxsplit=1)[0].strip()
        for s in scrobbles if s.get("artist")
    ]))

    console.print(Panel(
        f"[bold cyan]🎯 Detected Taste Profile[/bold cyan]\n"
        f"[dim]Top Artists: [/dim][bold white]{', '.join(artists[:5])}[/bold white]\n"
        f"[dim]Derived Similar Artists: [/dim][bold cyan]Novo Amor, Paper Kites, Anuv Jain, Mohit Chauhan, Local Train[/bold cyan]",
        border_style="cyan"
    ))
    console.print()

    # 3. Discover NEW recommended single tracks
    new_recs = discover_new_recommended_tracks(artists, existing_lib, count_per_artist=2)

    if not new_recs:
        console.print("[yellow]ℹ️  All top recommended songs for your artists are already in your library![/yellow]")
        sys.exit(0)

    # Display New Recommendations Table
    table_recs = Table(box=box.ROUNDED, border_style="green", padding=(0, 2), title="✨ NEW Single Song Recommendations (Not in library)")
    table_recs.add_column("#", style="bold dim", justify="right")
    table_recs.add_column("Recommended Song", style="bold green")
    table_recs.add_column("Based on Artist", style="bold cyan")

    for idx, item in enumerate(new_recs, 1):
        table_recs.add_row(str(idx), item["title"][:60], item["artist"])

    console.print(table_recs)
    console.print()

    if Confirm.ask(f"[bold yellow]Would you like to download these {len(new_recs)} NEW recommended songs + lyrics to your server?[/bold yellow]"):
        success = 0
        for item in new_recs:
            if download_track_by_url(item["url"], item["title"], item["artist"]):
                success += 1
            time.sleep(0.5)

        console.print(f"\n[bold green]✨ Finished downloading {success}/{len(new_recs)} NEW songs + lyrics to your library![/bold green]")

        # Trigger Navidrome scan
        try:
            from downloader import trigger_navidrome_scan
            trigger_navidrome_scan()
        except Exception:
            pass
    else:
        console.print("[dim cyan]No downloads performed.[/dim cyan]")

if __name__ == "__main__":
    main()
