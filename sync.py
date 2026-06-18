#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PSudofy — One-Command Sync
============================
Paste a Spotify or YouTube Music playlist URL and this script will:
  1. Download songs directly on your Oracle Cloud server
  2. Fetch lyrics locally (bypasses Cloudflare block on server)
  3. Upload lyrics to server and embed into MP3 tags
  4. Trigger Navidrome rescan so songs appear immediately
  5. Email you a full summary report

Usage:
    python sync.py "https://open.spotify.com/playlist/..."
    python sync.py "https://music.youtube.com/playlist?list=..."
    python sync.py --dry-run "https://open.spotify.com/playlist/..."

Requirements (local PC):
    pip install rich python-dotenv beautifulsoup4 mutagen

The following must be set in your .env file:
    SSH_KEY_PATH   = C:\\Users\\Adarsh Singh\\Downloads\\ssh-key-2026-05-28.key
    SSH_HOST       = ubuntu@161.118.165.241
    GENIUS_TOKEN   = <your token>
    NAVIDROME_URL  = http://161.118.165.241:4533
    NAVI_USER      = <user>
    NAVI_PASS      = <pass>
    NOTIFY_EMAIL_FROM = yourgmail@gmail.com
    NOTIFY_EMAIL_TO   = yourgmail@gmail.com
    NOTIFY_EMAIL_PASS = xxxx xxxx xxxx xxxx   # Gmail App Password
"""

import os
import re
import sys
import json
import time
import smtplib
import hashlib
import secrets
import argparse
import subprocess
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from typing import Optional, Tuple

from dotenv import load_dotenv

load_dotenv()

from rich.console import Console  # noqa: E402
from rich.panel import Panel  # noqa: E402
from rich.table import Table  # noqa: E402
from rich import box  # noqa: E402
from rich.align import Align  # noqa: E402
from rich.rule import Rule  # noqa: E402
from rich.text import Text  # noqa: E402

try:
    from bs4 import BeautifulSoup
    BS4 = True
except ImportError:
    BS4 = False

# ── Configuration ─────────────────────────────────────────────────────────────

SSH_KEY_PATH   = os.getenv("SSH_KEY_PATH",   r"C:\Users\Adarsh Singh\Downloads\ssh-key-2026-05-28.key")
SSH_HOST       = os.getenv("SSH_HOST",       "ubuntu@161.118.165.241")
REMOTE_DIR     = os.getenv("REMOTE_DIR",     "~/PSudofy")
MUSIC_FOLDER   = os.getenv("MUSIC_FOLDER",   "./music")  # relative on server

GENIUS_TOKEN   = os.getenv("GENIUS_TOKEN", "")
NAVIDROME_URL  = os.getenv("NAVIDROME_URL", "http://161.118.165.241:4533")
NAVI_USER      = os.getenv("NAVI_USER", "")
NAVI_PASS      = os.getenv("NAVI_PASS", "")

NOTIFY_EMAIL_FROM = os.getenv("NOTIFY_EMAIL_FROM", "")
NOTIFY_EMAIL_TO   = os.getenv("NOTIFY_EMAIL_TO", "")
NOTIFY_EMAIL_PASS = os.getenv("NOTIFY_EMAIL_PASS", "")

DELAY   = 0.5
TIMEOUT = 12

# Local temp paths
LOCAL_DATA_DIR   = Path("data")
LYRICS_DATA_PATH = LOCAL_DATA_DIR / "lyrics_data.json"

# ── Console ───────────────────────────────────────────────────────────────────

console = Console()

# ── Banner ────────────────────────────────────────────────────────────────────

BANNER = (
    "[bold cyan]\n"
    "██████╗ ███████╗██╗   ██╗██████╗  ██████╗ ███████╗██╗   ██╗\n"
    "██╔══██╗██╔════╝██║   ██║██╔══██╗██╔═══██╗██╔════╝╚██╗ ██╔╝\n"
    "██████╔╝███████╗██║   ██║██║  ██║██║   ██║█████╗   ╚████╔╝ \n"
    "██╔═══╝ ╚════██║██║   ██║██║  ██║██║   ██║██╔══╝    ╚██╔╝  \n"
    "██║     ███████║╚██████╔╝██████╔╝╚██████╔╝██║        ██║   \n"
    "╚═╝     ╚══════╝ ╚═════╝ ╚═════╝  ╚═════╝ ╚═╝        ╚═╝   \n"
    "[/bold cyan]"
    "[dim cyan]      ✦  One-Command Sync: Songs + Lyrics + Notify  ✦[/dim cyan]"
)


def print_banner():
    console.clear()
    console.print(Align.center(Text.from_markup(BANNER)))
    console.print(Rule(style="dim cyan"))
    console.print()


# ── SSH / SCP Helpers ─────────────────────────────────────────────────────────

def _ssh_base() -> list:
    """Return the base ssh command args (with key, host control)."""
    return [
        "ssh",
        "-i", SSH_KEY_PATH,
        "-o", "StrictHostKeyChecking=no",
        "-o", "BatchMode=yes",
        SSH_HOST,
    ]


def ssh_run(command: str, stream: bool = False) -> subprocess.CompletedProcess:
    """Run a command on the remote server. If stream=True, output goes to console."""
    args = _ssh_base() + [command]
    if stream:
        return subprocess.run(args, check=False)
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")


def ssh_stream_lines(command: str):
    """Run a command on the remote server and yield output lines in real-time."""
    args = _ssh_base() + [command]
    process = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    for line in process.stdout:
        yield line.rstrip()
    process.wait()
    return process.returncode


def scp_upload(local_path: str, remote_path: str):
    """Upload a file to the remote server via SCP."""
    subprocess.run(
        [
            "scp",
            "-i", SSH_KEY_PATH,
            "-o", "StrictHostKeyChecking=no",
            local_path,
            f"{SSH_HOST}:{remote_path}",
        ],
        check=True,
        capture_output=True,
    )


# ── Navidrome Scan ────────────────────────────────────────────────────────────

def _subsonic_params() -> dict:
    salt  = secrets.token_hex(8)
    token = hashlib.md5((NAVI_PASS + salt).encode()).hexdigest()
    return {"u": NAVI_USER, "t": token, "s": salt, "v": "1.16.1", "c": "psudofy", "f": "json"}


def trigger_navidrome_scan() -> bool:
    if not NAVI_USER or not NAVI_PASS:
        console.print("[yellow]⚠️[/yellow]  Navidrome credentials not set — skipping rescan.")
        return False
    try:
        params = urllib.parse.urlencode(_subsonic_params())
        urllib.request.urlopen(f"{NAVIDROME_URL}/rest/startScan.view?{params}", timeout=8)
        console.print("[cyan]✓[/cyan]  Navidrome is rescanning… [dim](new songs appear in seconds)[/dim]")
        return True
    except Exception:
        console.print("[yellow]⚠️[/yellow]  Could not reach Navidrome — open it and click 🔄 manually.")
        return False


# ── Step 1: Download Songs on Server ─────────────────────────────────────────

def download_songs_on_server(url: str, dry_run: bool = False) -> dict:
    """
    SSH into the Oracle server and run spotdl / yt-dlp to download the playlist
    directly there. Returns a stats dict.
    """
    console.print()
    console.print(Rule("[bold cyan]Step 1 · Download Songs on Server[/bold cyan]", style="cyan"))
    console.print()

    stats = {"downloaded": 0, "skipped": 0, "failed": 0, "total": 0}
    failed_songs = []

    is_spotify = "spotify.com" in url
    is_youtube = "youtube.com" in url or "youtu.be" in url

    if not is_spotify and not is_youtube:
        console.print("[red]❌  Unsupported URL.[/red] Please provide a Spotify or YouTube Music link.")
        return stats

    if dry_run:
        console.print(f"[cyan]ℹ️  Dry Run — would download:[/cyan] [bold]{url}[/bold]")
        return stats

    if is_spotify:
        output_template = f"{REMOTE_DIR}/music/{{artist}}/{{title}} - {{artist}}.{{ext}}"
        archive_path    = f"{REMOTE_DIR}/downloaded_spotify.txt"
        cmd = (
            f"cd {REMOTE_DIR} && "
            f"spotdl download '{url}' "
            f"--output '{output_template}' "
            f"--archive {archive_path} "
            f"--threads 4"
        )
    else:
        output_template = f"{REMOTE_DIR}/music/%(artist,uploader)s/%(title)s.%(ext)s"
        archive_path    = f"{REMOTE_DIR}/downloaded_yt.txt"
        cmd = (
            f"cd {REMOTE_DIR} && "
            f"yt-dlp --format bestaudio/best "
            f"--postprocessors '[{{\"key\":\"FFmpegExtractAudio\",\"preferredcodec\":\"mp3\",\"preferredquality\":\"0\"}},{{\"key\":\"FFmpegMetadata\"}},{{\"key\":\"EmbedThumbnail\"}}]' "
            f"--write-thumbnail "
            f"--output '{output_template}' "
            f"--download-archive {archive_path} "
            f"--quiet --no-warnings "
            f"'{url}'"
        )

    console.print(f"[cyan]⟳[/cyan]  Connecting to server and starting download…")
    console.print(f"[dim]  Source: {'Spotify (spotdl)' if is_spotify else 'YouTube (yt-dlp)'}[/dim]\n")

    for line in ssh_stream_lines(cmd):
        line_lower = line.lower()

        if not line.strip():
            continue

        # ── Spotify parsing ───────────────────────────────────────────────
        if is_spotify:
            if "found" in line_lower and "song" in line_lower:
                for word in line.split():
                    if word.isdigit():
                        stats["total"] = int(word)
                        console.print(f"[cyan]✓[/cyan]  Found [bold cyan]{stats['total']}[/bold cyan] songs in playlist\n")
                        break
            elif line_lower.startswith("downloaded") or ("downloaded" in line_lower and '"' in line):
                name = line.split('"')[1] if '"' in line else line
                stats["downloaded"] += 1
                console.print(f"  [green]✅[/green] {name}")
            elif "skipping" in line_lower:
                name = re.sub(r"(?i)^skipping\s+", "", line).strip(' "')
                stats["skipped"] += 1
                console.print(f"  [yellow]⚠️[/yellow]  Skipped: [dim]{name}[/dim]")
            elif "failed" in line_lower and "fetch secrets" not in line_lower and "thetadev" not in line_lower:
                name = line.split('"')[1] if '"' in line else line
                stats["failed"] += 1
                failed_songs.append(name)
                console.print(f"  [red]❌[/red] Failed: {name}")
            elif "downloading" in line_lower and '"' in line:
                name = line.split('"')[1]
                console.print(f"  [cyan]⟳[/cyan]  Downloading: [italic]{name}[/italic]")

        # ── YouTube parsing ───────────────────────────────────────────────
        else:
            if "[download]" in line_lower and "%" in line:
                console.print(f"  [cyan]⟳[/cyan]  [dim]{line}[/dim]")
            elif "error" in line_lower or "unable" in line_lower:
                stats["failed"] += 1
                failed_songs.append(line[:80])
                console.print(f"  [red]❌[/red] {line[:80]}")
            elif line.strip():
                console.print(f"  [dim]{line}[/dim]")

    if stats["total"] == 0:
        stats["total"] = stats["downloaded"] + stats["skipped"] + stats["failed"]

    # Retry failed (once)
    if failed_songs:
        console.print(f"\n[yellow]⚠️[/yellow]  {len(failed_songs)} songs failed. Retrying once…\n")
        retry_failed = []
        for song in failed_songs:
            # For Spotify retry just re-run the URL — spotdl skips already downloaded
            if is_spotify:
                retry_cmd = (
                    f"cd {REMOTE_DIR} && spotdl download '{url}' "
                    f"--output '{output_template}' --archive {archive_path} --threads 2"
                )
                result = ssh_run(retry_cmd)
                if "downloaded" in result.stdout.lower():
                    stats["downloaded"] += 1
                    stats["failed"] -= 1
                    console.print(f"  [green]✅[/green] Retry success: {song}")
                else:
                    retry_failed.append(song)
            else:
                retry_failed.append(song)  # yt-dlp retries are complex; log for email
        failed_songs = retry_failed

    stats["_failed_list"] = failed_songs
    return stats


# ── Step 2: Get Playlist Tracks & Filter to Missing Lyrics ───────────────────

# Script uploaded to server: checks if a specific set of songs have lyrics
_CHECK_SCRIPT_TPL = """
import os, json, sys
from pathlib import Path
try:
    from mutagen.id3 import ID3, ID3NoHeaderError
except ImportError:
    print("ERROR:mutagen_not_installed")
    sys.exit(1)

music_root = Path(os.path.expanduser("~/PSudofy/music"))

# Songs to check (injected by sync.py)
songs_to_check = {songs_json}

# Build a map: normalised (title, artist) -> path for all MP3s on server
server_map = {{}}
for mp3 in music_root.rglob("*.mp3"):
    try:
        tags = ID3(str(mp3))
        t = str(tags.get("TIT2", "")).strip().lower()
        a = str(tags.get("TPE1", "")).strip().lower()
        if t:
            server_map[(t, a)] = {{"path": str(mp3), "title": str(tags.get("TIT2", "")), "artist": str(tags.get("TPE1", ""))}}
    except (ID3NoHeaderError, Exception):
        pass

missing = []
found   = 0

for song in songs_to_check:
    t = song.get("title", "").strip().lower()
    a = song.get("artist", "").strip().lower()
    entry = server_map.get((t, a)) or server_map.get((t, ""))
    if not entry:
        continue  # not on server yet (might still be downloading)
    try:
        tags = ID3(entry["path"])
        has_lyrics = bool(tags.getall("USLT") or tags.getall("SYLT"))
        if has_lyrics:
            found += 1
        else:
            missing.append({{"title": entry["title"], "artist": entry["artist"], "path": entry["path"]}})
    except Exception:
        pass

print(json.dumps({{"missing": missing, "found_count": found}}))
"""


def get_playlist_tracks(url: str) -> list:
    """
    Fetch track list (title, artist) from a Spotify or YouTube playlist URL.
    Returns list of dicts: [{"title": ..., "artist": ...}, ...]
    """
    tracks = []
    is_spotify = "spotify.com" in url
    is_youtube = "youtube.com" in url or "youtu.be" in url

    if is_spotify:
        try:
            import spotapi  # type: ignore
            if "playlist/" in url:
                playlist_id = url.split("playlist/")[-1].split("?")[0]
                pub = spotapi.PublicPlaylist(playlist_id)
                for chunk in pub.paginate_playlist():
                    for item in chunk.get("items", []):
                        try:
                            td  = item.get("itemV3", {}).get("data", {})
                            uri = td.get("uri", "")
                            if not uri.startswith("spotify:track:"):
                                continue
                            name = td.get("name", "") or td.get("identityTrait", {}).get("name", "")
                            contributors = td.get("artists", {}).get("items", []) or td.get("identityTrait", {}).get("contributors", {}).get("items", [])
                            artist = ""
                            if contributors:
                                p = contributors[0].get("profile", {})
                                artist = p.get("name", "") if p else contributors[0].get("name", "")
                            if name:
                                tracks.append({"title": name, "artist": artist})
                        except Exception:
                            pass
            elif "album/" in url:
                album_id = url.split("album/")[-1].split("?")[0]
                pub = spotapi.PublicAlbum(album_id)
                for chunk in pub.paginate_album():
                    for item in chunk.get("items", []):
                        try:
                            name   = item.get("name", "")
                            artists = item.get("artists", {}).get("items", [])
                            artist = artists[0].get("profile", {}).get("name", "") if artists else ""
                            if name:
                                tracks.append({"title": name, "artist": artist})
                        except Exception:
                            pass
            elif "track/" in url:
                # Single track — can't easily prefetch name without API, return placeholder
                tracks.append({"title": "Unknown", "artist": ""})
        except Exception as e:
            console.print(f"[yellow]⚠️[/yellow]  Could not prefetch Spotify tracks: [dim]{e}[/dim]")

    elif is_youtube:
        try:
            import yt_dlp  # type: ignore
            flat_opts = {"quiet": True, "extract_flat": True, "skip_download": True}
            with yt_dlp.YoutubeDL(flat_opts) as ydl:
                info = ydl.extract_info(url, download=False)
            entries = info.get("entries", [info]) if info.get("_type") == "playlist" else [info]
            for e in entries:
                if e:
                    tracks.append({"title": e.get("title", ""), "artist": e.get("uploader", "")})
        except Exception as e:
            console.print(f"[yellow]⚠️[/yellow]  Could not prefetch YouTube tracks: [dim]{e}[/dim]")

    return tracks


def get_playlist_songs_missing_lyrics(url: str) -> Tuple[list, int]:
    """
    Fetch the playlist track list, then SSH into server and check only those
    songs for missing lyrics — much faster than scanning all 978 MP3s.
    Returns (missing_songs list, already_have count).
    """
    console.print()
    console.print(Rule("[bold cyan]Step 2 · Check Playlist Songs for Lyrics[/bold cyan]", style="cyan"))
    console.print()

    # 1. Get track list from the playlist URL
    console.print("[cyan]⟳[/cyan]  Fetching track list from playlist…")
    playlist_tracks = get_playlist_tracks(url)

    if not playlist_tracks:
        console.print("[yellow]⚠️[/yellow]  Could not fetch playlist tracks — skipping lyrics check.")
        return [], 0

    console.print(
        f"[cyan]✓[/cyan]  Playlist has [bold cyan]{len(playlist_tracks)}[/bold cyan] tracks  "
        f"[dim]· checking which ones are missing lyrics on server…[/dim]"
    )

    # 2. Upload a targeted check script to the server
    songs_json = json.dumps(playlist_tracks)
    check_script = _CHECK_SCRIPT_TPL.replace("{songs_json}", songs_json)

    LOCAL_DATA_DIR.mkdir(exist_ok=True)
    tmp_path = str(LOCAL_DATA_DIR / "_check_lyrics.py")
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(check_script)

    remote_check_path = f"{REMOTE_DIR}/_check_lyrics.py"
    try:
        scp_upload(tmp_path, remote_check_path)
    finally:
        os.unlink(tmp_path)

    result = ssh_run(f"python3 {remote_check_path} && rm -f {remote_check_path}")

    if result.returncode != 0 or not result.stdout.strip():
        console.print(f"[red]❌  Failed to check server:[/red] {result.stderr[:200]}")
        # Fallback: return all playlist tracks as missing (so we at least try to fetch)
        return playlist_tracks, 0

    try:
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("{"):
                data    = json.loads(line)
                missing = data.get("missing", [])
                found   = data.get("found_count", 0)
                console.print(
                    f"[cyan]✓[/cyan]  [bold cyan]{len(missing)}[/bold cyan] songs in playlist missing lyrics  "
                    f"[dim]·  {found} already have lyrics[/dim]"
                )
                return missing, found
    except Exception as e:
        console.print(f"[red]❌  Could not parse check output:[/red] {e}")

    return playlist_tracks, 0


# ── Step 3: Fetch Lyrics Locally ──────────────────────────────────────────────

def _clean_title(title: str) -> str:
    t = re.sub(r"\s*\(from\s+[^)]+\)", "", title, flags=re.IGNORECASE)
    t = re.sub(r"\s*-\s*from\s+.+",    "", t,     flags=re.IGNORECASE)
    t = re.sub(r"\s*\(feat\.?\s+[^)]+\)", "", t,  flags=re.IGNORECASE)
    t = re.sub(r"\s*\(ft\.?\s+[^)]+\)",   "", t,  flags=re.IGNORECASE)
    t = re.sub(r"\s*\(with\s+[^)]+\)",    "", t,  flags=re.IGNORECASE)
    t = re.sub(r"\s*\(dance version\)",   "", t,   flags=re.IGNORECASE)
    t = re.sub(r"\s*\(acoustic.*?\)",     "", t,   flags=re.IGNORECASE)
    return t.strip()


def _primary_artist(artist_str: str) -> str:
    if not artist_str:
        return ""
    return re.split(r"[/,&]|\bfeat\b|\bft\b", artist_str, maxsplit=1)[0].strip()


def _http_get(url: str, headers: dict = None) -> Optional[str]:
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "PSudofy/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception:
        return None


def _fetch_lrclib(title: str, artist: str) -> Tuple[Optional[str], Optional[str]]:
    params = urllib.parse.urlencode({
        "track_name":  _clean_title(title),
        "artist_name": _primary_artist(artist),
    })
    body = _http_get(f"https://lrclib.net/api/get?{params}")
    if body:
        try:
            data = json.loads(body)
            return data.get("plainLyrics") or None, data.get("syncedLyrics") or None
        except Exception:
            pass
    return None, None


BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,hi;q=0.8",
    "Accept-Encoding": "identity",
    "Connection": "keep-alive",
}


def _genius_search_url(title: str, artist: str) -> Optional[str]:
    if not GENIUS_TOKEN:
        return None
    params = urllib.parse.urlencode({"q": f"{_clean_title(title)} {_primary_artist(artist)}"})
    body = _http_get(
        f"https://api.genius.com/search?{params}",
        {"Authorization": f"Bearer {GENIUS_TOKEN}", "User-Agent": "PSudofy/1.0"},
    )
    if body:
        try:
            hits = json.loads(body)["response"]["hits"]
            if hits:
                return hits[0]["result"]["url"]
        except Exception:
            pass
    return None


def _genius_scrape(url: str) -> Optional[str]:
    if not BS4:
        return None
    body = _http_get(url, BROWSER_HEADERS)
    if not body:
        return None
    soup = BeautifulSoup(body, "html.parser")
    containers = soup.find_all("div", attrs={"data-lyrics-container": "true"})
    if containers:
        parts = []
        for c in containers:
            for br in c.find_all("br"):
                br.replace_with("\n")
            parts.append(c.get_text())
        text = "\n".join(parts).strip()
        if len(text) > 30:
            return text
    for script in soup.find_all("script"):
        src = script.string or ""
        m = re.search(r'"plainLyrics"\s*:\s*"((?:[^"\\]|\\.)*)"', src)
        if m:
            return m.group(1).replace("\\n", "\n").replace('\\"', '"').strip()
    return None


def _fetch_genius(title: str, artist: str) -> Optional[str]:
    url = _genius_search_url(title, artist)
    if not url:
        return None
    time.sleep(0.3)
    return _genius_scrape(url)


def fetch_lyrics_locally(missing_songs: list) -> dict:
    """
    Fetch lyrics from lrclib + Genius on local PC for all songs in the list.
    Returns a dict keyed by 'title|||artist' with lyrics data.
    """
    console.print()
    console.print(Rule("[bold cyan]Step 3 · Fetch Lyrics Locally[/bold cyan]", style="cyan"))
    console.print()

    if not missing_songs:
        console.print("[green]✓[/green]  No songs missing lyrics — skipping fetch.")
        return {}

    console.print(
        f"[cyan]⟳[/cyan]  Fetching lyrics for [bold cyan]{len(missing_songs)}[/bold cyan] songs  "
        f"[dim]· lrclib.net → Genius fallback[/dim]\n"
    )

    if not GENIUS_TOKEN:
        console.print("[yellow]⚠️[/yellow]  GENIUS_TOKEN not set in .env — Genius fallback disabled.")
    if not BS4:
        console.print("[yellow]⚠️[/yellow]  beautifulsoup4 not installed — Genius scraping disabled.")
        console.print("[dim]  Install with: pip install beautifulsoup4[/dim]\n")

    # Load existing cache so we can resume if interrupted
    LOCAL_DATA_DIR.mkdir(exist_ok=True)
    results = {}
    if LYRICS_DATA_PATH.exists():
        try:
            with open(LYRICS_DATA_PATH, encoding="utf-8") as f:
                results = json.load(f)
            console.print(f"[dim]ℹ️  Resuming — {len(results)} songs already in local cache.[/dim]\n")
        except Exception:
            pass

    stats = {"lrclib": 0, "genius": 0, "not_found": 0}
    failed_lyrics = []
    total = len(missing_songs)

    for i, song in enumerate(missing_songs, 1):
        title  = song.get("title", "") or ""
        artist = song.get("artist", "") or ""
        key    = f"{title}|||{artist}"

        if key in results:
            console.print(f"  [{i}/{total}] [dim]Already cached: {title}[/dim]")
            continue

        label = f"{title} — {_primary_artist(artist)}" if artist else title
        console.print(f"  [{i}/{total}] {label}", end=" ")
        sys.stdout.flush()

        plain, synced, source = None, None, None

        # Source 1: lrclib (synced lyrics)
        p, s = _fetch_lrclib(title, artist)
        if p or s:
            plain, synced, source = p, s, "lrclib"

        # Source 2: Genius fallback (better Bollywood coverage)
        if not plain and not synced:
            time.sleep(DELAY)
            g = _fetch_genius(title, artist)
            if g:
                plain, source = g, "genius"

        if plain or synced:
            results[key] = {
                "title":  title,
                "artist": artist,
                "source": source,
                "plain":  plain,
                "synced": synced,
            }
            stats[source] += 1
            tag = "synced" if synced else "plain"
            console.print(f"  [green]✅[/green] [{source}] ({tag})")
        else:
            stats["not_found"] += 1
            failed_lyrics.append(label)
            console.print("  [yellow]⚠️[/yellow]  not found")

        # Save after every song so we can resume safely if interrupted
        with open(LYRICS_DATA_PATH, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)

        time.sleep(DELAY)

    recovered = stats["lrclib"] + stats["genius"]
    console.print(
        f"\n[cyan]✓[/cyan]  Lyrics fetched — "
        f"[green]{recovered} found[/green]  [yellow]{stats['not_found']} not found[/yellow]  "
        f"[dim](lrclib: {stats['lrclib']}, genius: {stats['genius']})[/dim]"
    )

    results["_meta_failed"] = failed_lyrics
    return results


# ── Step 4: Upload lyrics_data.json and Embed on Server ──────────────────────

def upload_and_embed_lyrics(lyrics_data: dict) -> dict:
    """Upload lyrics_data.json to the server then run embed_lyrics_remote.py."""
    console.print()
    console.print(Rule("[bold cyan]Step 4 · Embed Lyrics on Server[/bold cyan]", style="cyan"))
    console.print()

    # Remove internal metadata key before upload
    upload_data = {k: v for k, v in lyrics_data.items() if not k.startswith("_meta")}

    if not upload_data:
        console.print("[green]✓[/green]  No new lyrics to embed.")
        return {"embedded": 0, "no_file": 0, "failed": 0}

    # Save clean copy locally
    clean_path = LOCAL_DATA_DIR / "lyrics_data_upload.json"
    with open(clean_path, "w", encoding="utf-8") as f:
        json.dump(upload_data, f, indent=2, ensure_ascii=False)

    console.print(f"[cyan]⟳[/cyan]  Uploading {len(upload_data)} lyrics entries to server…")
    try:
        scp_upload(str(clean_path), f"{REMOTE_DIR}/lyrics_data.json")
        console.print("[cyan]✓[/cyan]  Upload complete.")
    except subprocess.CalledProcessError as e:
        console.print(f"[red]❌  SCP upload failed:[/red] {e}")
        return {"embedded": 0, "no_file": 0, "failed": len(upload_data)}

    console.print("[cyan]⟳[/cyan]  Running embed_lyrics_remote.py on server…\n")

    embed_stats = {"embedded": 0, "no_file": 0, "failed": 0}
    cmd = f"cd {REMOTE_DIR} && python3 embed_lyrics_remote.py"

    for line in ssh_stream_lines(cmd):
        line_lower = line.lower()
        if "✅" in line or "embedded" in line_lower:
            embed_stats["embedded"] += 1
            console.print(f"  [green]✅[/green] {line}")
        elif "no file" in line_lower or "not found" in line_lower:
            embed_stats["no_file"] += 1
            console.print(f"  [yellow]⚠️[/yellow]  {line}")
        elif "failed" in line_lower or "❌" in line:
            embed_stats["failed"] += 1
            console.print(f"  [red]❌[/red] {line}")
        elif "---" in line or "complete" in line_lower or "embedded" in line_lower:
            console.print(f"  [dim]{line}[/dim]")

    return embed_stats


# ── Step 5: Email Notification ────────────────────────────────────────────────

def send_email_report(
    url: str,
    song_stats: dict,
    lyrics_missing: int,
    embed_stats: dict,
    elapsed: float,
    failed_songs: list,
    failed_lyrics: list,
):
    """Send a full HTML summary email via Gmail SMTP."""
    if not NOTIFY_EMAIL_FROM or not NOTIFY_EMAIL_TO or not NOTIFY_EMAIL_PASS:
        console.print("[yellow]⚠️[/yellow]  Email credentials not set in .env — skipping notification.")
        return

    mins, secs = divmod(int(elapsed), 60)
    time_str = f"{mins}m {secs}s" if mins else f"{secs}s"

    any_failure = bool(failed_songs or failed_lyrics)
    subject_icon = "⚠️" if any_failure else "✅"
    subject = f"{subject_icon} PSudofy Sync — {song_stats.get('downloaded', 0)} downloaded · {time_str}"

    # ── Plain-text body ───────────────────────────────────────────────────────
    body_lines = [
        "PSudofy Sync Report",
        "=" * 50,
        f"URL: {url}",
        f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"Duration: {time_str}",
        "",
        "🎵 Songs",
        f"  Downloaded : {song_stats.get('downloaded', 0)}",
        f"  Skipped    : {song_stats.get('skipped', 0)}  (already in archive)",
        f"  Failed     : {song_stats.get('failed', 0)}",
        f"  Total      : {song_stats.get('total', 0)}",
    ]

    if failed_songs:
        body_lines.append("\n  Failed song details:")
        for s in failed_songs:
            body_lines.append(f"    ❌ {s}")

    body_lines += [
        "",
        "📝 Lyrics",
        f"  Embedded   : {embed_stats.get('embedded', 0)}",
        f"  Not Found  : {len(failed_lyrics)}",
        f"  No File    : {embed_stats.get('no_file', 0)}",
    ]

    if failed_lyrics:
        body_lines.append("\n  Songs without lyrics:")
        for s in failed_lyrics[:20]:
            body_lines.append(f"    ⚠️  {s}")
        if len(failed_lyrics) > 20:
            body_lines.append(f"    … and {len(failed_lyrics) - 20} more")

    body_lines += [
        "",
        "✅ Navidrome rescanned — enjoy your music!",
    ]

    body = "\n".join(body_lines)

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = NOTIFY_EMAIL_FROM
        msg["To"]      = NOTIFY_EMAIL_TO
        msg.attach(MIMEText(body, "plain", "utf-8"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(NOTIFY_EMAIL_FROM, NOTIFY_EMAIL_PASS)
            smtp.sendmail(NOTIFY_EMAIL_FROM, NOTIFY_EMAIL_TO, msg.as_string())

        console.print(f"[cyan]✓[/cyan]  Summary email sent → [bold]{NOTIFY_EMAIL_TO}[/bold]")
    except Exception as e:
        console.print(f"[yellow]⚠️[/yellow]  Failed to send email: [dim]{e}[/dim]")


# ── Final Summary ─────────────────────────────────────────────────────────────

def show_final_summary(song_stats: dict, embed_stats: dict, failed_songs: list, failed_lyrics: list, elapsed: float):
    mins, secs = divmod(int(elapsed), 60)
    time_str = f"{mins}m {secs}s" if mins else f"{secs}s"

    table = Table(box=box.ROUNDED, border_style="cyan", padding=(0, 3), show_header=False)
    table.add_column(style="bold cyan", justify="left")
    table.add_column(justify="right")

    table.add_section()
    table.add_row("[bold]🎵  Songs[/bold]", "")
    table.add_row("  Downloaded",  f"[bold green]{song_stats.get('downloaded', 0)}[/bold green]")
    table.add_row("  Skipped",     f"[bold yellow]{song_stats.get('skipped', 0)}[/bold yellow]")
    table.add_row("  Failed",      f"[bold red]{song_stats.get('failed', 0)}[/bold red]")

    table.add_section()
    table.add_row("[bold]📝  Lyrics[/bold]", "")
    table.add_row("  Embedded",    f"[bold green]{embed_stats.get('embedded', 0)}[/bold green]")
    table.add_row("  Not Found",   f"[bold yellow]{len(failed_lyrics)}[/bold yellow]")

    table.add_section()
    table.add_row("⏱️   Total Time", f"[bold cyan]{time_str}[/bold cyan]")

    console.print()
    console.print(Rule(style="dim cyan"))
    console.print(
        Align.center(
            Panel(
                table,
                title="[bold cyan]✨  Sync Complete[/bold cyan]",
                border_style="cyan",
                padding=(1, 4),
            )
        )
    )

    if failed_songs:
        console.print(f"\n[red]Failed songs ({len(failed_songs)}):[/red]")
        for s in failed_songs:
            console.print(f"  [red]❌[/red] {s}")

    if failed_lyrics:
        console.print(f"\n[yellow]Songs without lyrics ({len(failed_lyrics)}):[/yellow]")
        for s in failed_lyrics[:10]:
            console.print(f"  [yellow]⚠️[/yellow]  {s}")
        if len(failed_lyrics) > 10:
            console.print(f"  [dim]… and {len(failed_lyrics) - 10} more[/dim]")

    console.print()


# ── Entry Point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="PSudofy One-Command Sync — download songs + lyrics to Oracle server"
    )
    parser.add_argument("url", help="Spotify or YouTube Music playlist/album/track URL")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would happen without actually downloading or embedding",
    )
    parser.add_argument(
        "--skip-lyrics", action="store_true",
        help="Download songs only, skip lyrics fetching",
    )
    parser.add_argument(
        "--skip-songs", action="store_true",
        help="Fetch and embed lyrics only, skip song downloading",
    )
    args = parser.parse_args()

    print_banner()

    start_time = time.time()
    song_stats   = {"downloaded": 0, "skipped": 0, "failed": 0, "total": 0, "_failed_list": []}
    embed_stats  = {"embedded": 0, "no_file": 0, "failed": 0}
    failed_songs  = []
    failed_lyrics = []

    # ── Step 1: Download songs on server ─────────────────────────────────────
    if not args.skip_songs:
        song_stats  = download_songs_on_server(args.url, dry_run=args.dry_run)
        failed_songs = song_stats.pop("_failed_list", [])
    else:
        console.print("[dim]ℹ️  Skipping song download (--skip-songs)[/dim]")

    # ── Step 2: Check only playlist songs for missing lyrics ────────────────────
    if not args.skip_lyrics and not args.dry_run:
        missing_songs, already_have = get_playlist_songs_missing_lyrics(args.url)

        # ── Step 3: Fetch lyrics on local PC ─────────────────────────────────
        lyrics_data = fetch_lyrics_locally(missing_songs)
        failed_lyrics = lyrics_data.pop("_meta_failed", [])

        # ── Step 4: Upload + embed on server ─────────────────────────────────
        embed_stats = upload_and_embed_lyrics(lyrics_data)

    elif args.dry_run:
        console.print("[dim]ℹ️  Dry Run — skipping lyrics and embed steps.[/dim]")
    else:
        console.print("[dim]ℹ️  Skipping lyrics (--skip-lyrics)[/dim]")

    # ── Navidrome rescan ──────────────────────────────────────────────────────
    console.print()
    console.print(Rule("[bold cyan]Step 5 · Navidrome Rescan[/bold cyan]", style="cyan"))
    console.print()
    if not args.dry_run:
        trigger_navidrome_scan()
    else:
        console.print("[dim]ℹ️  Dry Run — skipping Navidrome rescan.[/dim]")

    elapsed = time.time() - start_time

    # ── Final summary in terminal ─────────────────────────────────────────────
    show_final_summary(song_stats, embed_stats, failed_songs, failed_lyrics, elapsed)

    # ── Email notification ────────────────────────────────────────────────────
    if not args.dry_run:
        send_email_report(
            url=args.url,
            song_stats=song_stats,
            lyrics_missing=len(failed_lyrics),
            embed_stats=embed_stats,
            elapsed=elapsed,
            failed_songs=failed_songs,
            failed_lyrics=failed_lyrics,
        )


if __name__ == "__main__":
    main()
