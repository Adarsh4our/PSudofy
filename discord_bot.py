#!/usr/bin/env python3
"""
PSudofy Discord Bot
====================
Download songs, albums, and playlists straight from Discord.
Runs on your Oracle Cloud server 24/7.
Downloads happen on the SERVER's internet - your phone just sends the command!
"""

import os
import io
import json
import re
import asyncio
import subprocess
import threading
import urllib.request
import urllib.parse
import hashlib
import secrets
import sqlite3
import datetime

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
import shutil

from wrapped_generator import get_wrapped_data, generate_wrapped_card

load_dotenv(os.path.expanduser("~/PSudofy/.env"))

DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
MUSIC_FOLDER  = os.path.expanduser("~/PSudofy/music")
DB_PATH       = os.path.expanduser("~/PSudofy/data/navidrome.db")
NAVIDROME_URL = os.getenv("NAVIDROME_URL", "http://localhost:4533")
NAVI_USER     = os.getenv("NAVI_USER",    "")
NAVI_PASS     = os.getenv("NAVI_PASS",    "")
ARCHIVE_FILE  = os.path.expanduser("~/PSudofy/downloaded_yt.txt")
SPOTDL_BIN    = os.path.expanduser("~/PSudofy/venv/bin/spotdl")
COOKIES_PATH  = os.path.expanduser("~/PSudofy/cookies.txt")
DL_LOG_FILE   = os.path.expanduser("~/PSudofy/download_log.json")

ACTIVE_SUBPROCESSES = []
ACTIVE_TASKS = set()
_json_lock = threading.Lock()          # protects all JSON log file I/O
_download_lock: asyncio.Lock | None = None  # initialized on first use; protects concurrent playlist downloads
MAX_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "2"))
_download_semaphore: asyncio.Semaphore | None = None  # limits concurrent single downloads
bot_start_time: datetime.datetime | None = None  # set in on_ready; used by !health for uptime
WEEKLY_CHANNEL_ID: int | None = int(os.getenv("WEEKLY_CHANNEL_ID", "0")) or None  # Discord channel for weekly auto-downloads


def kill_all_download_processes():
    """Kill any running spotdl, yt-dlp, or ffmpeg processes."""
    global ACTIVE_SUBPROCESSES
    for p in list(ACTIVE_SUBPROCESSES):
        try:
            p.terminate()
            p.kill()
        except Exception:
            pass
    ACTIVE_SUBPROCESSES.clear()

    # Also system kill any lingering background workers
    for name in ["spotdl", "yt-dlp", "ffmpeg"]:
        try:
            subprocess.run(["pkill", "-9", "-f", name], capture_output=True)
        except Exception:
            pass



def get_spotify_tracks(url: str) -> tuple[str, list[dict]]:
    """Extract playlist, album, artist top tracks, or single track from Spotify embed API in <0.5s."""
    m = re.search(r"/(playlist|album|artist|track)/([a-zA-Z0-9]+)", url)
    if not m:
        return "Spotify Collection", []
    kind, pid = m.group(1), m.group(2)
    embed_url = f"https://open.spotify.com/embed/{kind}/{pid}"
    req = urllib.request.Request(embed_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            html = resp.read().decode("utf-8")
            match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>', html)
            if match:
                data = json.loads(match.group(1))
                entity = data.get("props", {}).get("pageProps", {}).get("state", {}).get("data", {}).get("entity", {})
                name = entity.get("name", "Spotify Collection")
                if kind == "artist":
                    name = f"{name} - Top Tracks"
                raw_tracks = entity.get("trackList", [])
                tracks = []
                for t in raw_tracks:
                    tracks.append({
                        "title": t.get("title", ""),
                        "artist": t.get("subtitle", "").replace("\u00a0", " ")
                    })
                return name, tracks
    except Exception as e:
        print(f"[Spotify Extract Error] {e}", flush=True)
    return "Spotify Collection", []


def run_spotify_playlist_download(url: str, progress_cb=None, create_playlist: bool = True, custom_name: str | None = None) -> dict:
    """
    Lightning-fast Spotify playlist sync & download:
    1. Instantly fetches track list from Spotify.
    2. Matches existing tracks in local Navidrome database in 0.1s.
    3. Downloads only truly missing songs via yt-dlp + lyrics.
    4. Auto-creates the Navidrome playlist (unless create_playlist=False).
    """
    playlist_title, tracks = get_spotify_tracks(url)
    if custom_name:
        playlist_title = custom_name
    total_count = len(tracks)

    if not tracks:
        return {"success": False, "error": "Could not extract tracks from Spotify link."}

    if progress_cb:
        progress_cb(0, total_count, f"Found {total_count} songs in \"{playlist_title}\"")

    # Connect to Navidrome DB to check existing tracks
    matched_ids = []
    downloaded_tracks = []
    missing_to_download = []

    try:
        conn = sqlite3.connect(DB_PATH)
        try:
            cur = conn.cursor()

            for idx, t in enumerate(tracks, 1):
                title = t["title"]
                artist = t["artist"]
                clean_t = clean_title_for_lyrics(title)

                # Check DB for song by title or path
                row = cur.execute("""
                    SELECT id, title, artist FROM media_file 
                    WHERE missing = 0 AND (
                        lower(title) LIKE lower(?) OR 
                        lower(title) LIKE lower(?) OR
                        lower(path) LIKE lower(?)
                    )
                    LIMIT 1
                """, (f"%{clean_t}%", f"{clean_t}%", f"%{clean_t}%")).fetchone()

                if row:
                    matched_ids.append(row[0])
                    downloaded_tracks.append({
                        "title": row[1] or title,
                        "artist": row[2] or artist,
                        "lyrics": "✅ In library",
                        "navidrome_id": row[0]
                    })
                    if progress_cb:
                        progress_cb(idx, total_count, f"Matched: {title}")
                else:
                    missing_to_download.append(t)
        finally:
            conn.close()

    except Exception as e:
        print(f"[Navidrome DB match error] {e}", flush=True)

    # Download any missing tracks
    failed_count = 0
    if missing_to_download:
        for idx, t in enumerate(missing_to_download, len(downloaded_tracks) + 1):
            query = f"{t['title']} {t['artist']}"
            if progress_cb:
                progress_cb(idx, total_count, f"Downloading missing: {t['title']}")
            res = run_single_download(query)
            if res.get("success"):
                downloaded_tracks.append(res)
            else:
                failed_count += 1

        trigger_navidrome_scan()
        navidrome_wait_scan(timeout=90)

        # Match newly downloaded tracks in Navidrome AFTER scan completes
        for track in downloaded_tracks:
            if "navidrome_id" not in track or not track.get("navidrome_id"):
                sid = navidrome_search_track(track["title"], track["artist"])
                if sid:
                    track["navidrome_id"] = sid
                    if sid not in matched_ids:
                        matched_ids.append(sid)

    # Create the Navidrome playlist if requested
    playlist_created = False
    if create_playlist and matched_ids:
        playlist_created = navidrome_create_playlist(playlist_title, matched_ids)

    for track in downloaded_tracks:
        write_dl_log({
            "type":     "spotify_playlist",
            "title":    track["title"],
            "artist":   track["artist"],
            "lyrics":   track.get("lyrics", "—"),
            "status":   "ok",
            "playlist": playlist_title if create_playlist else "None (Raw Library)",
        })

    return {
        "success":          True,
        "is_playlist":      True,
        "playlist_title":   playlist_title,
        "downloaded":       downloaded_tracks,
        "failed_count":     failed_count,
        "total":            len(downloaded_tracks),
        "playlist_created": playlist_created,
        "playlist_songs":   len(matched_ids),
        "skip_playlist":    not create_playlist,
    }


# (DL_LOG_FILE and COOKIES_PATH defined at top of file)

# Mutagen for lyrics embedding
try:
    from mutagen.id3 import ID3, USLT, SYLT, Encoding, ID3NoHeaderError
    MUTAGEN = True
except ImportError:
    MUTAGEN = False

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=["!", "/"], intents=intents, help_command=None)


# ─────────────────────────────── Helpers ──────────────────────────────────────

def _subsonic_params() -> dict:
    salt  = secrets.token_hex(8)
    token = hashlib.md5((NAVI_PASS + salt).encode()).hexdigest()
    return {"u": NAVI_USER, "t": token, "s": salt,
            "v": "1.16.1", "c": "psudofy-discord", "f": "json"}


def trigger_navidrome_scan(full: bool = False) -> bool:
    """Trigger an instant rescan in Navidrome via docker CLI or Subsonic API."""
    try:
        cmd = ["docker", "exec", "psudofy_navidrome_1", "/app/navidrome", "scan"]
        if full:
            cmd.append("--full")
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
        if res.returncode == 0:
            return True
    except Exception as e:
        print(f"[Navidrome Docker Scan Error] {e}", flush=True)

    # Fallback to subsonic endpoint
    try:
        params = urllib.parse.urlencode(_subsonic_params())
        with urllib.request.urlopen(f"{NAVIDROME_URL}/rest/startScan.view?{params}", timeout=5) as resp:
            return True
    except Exception:
        return False


def navidrome_wait_scan(timeout: int = 30):
    """Trigger a Navidrome scan and wait for it to complete (polls getScanStatus every 2s)."""
    trigger_navidrome_scan()
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            params = urllib.parse.urlencode({**_subsonic_params(), "f": "json"})
            with urllib.request.urlopen(
                f"{NAVIDROME_URL}/rest/getScanStatus.view?{params}", timeout=4
            ) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                status = data.get("subsonic-response", {}).get("scanStatus", {})
                if not status.get("scanning", True):
                    return  # scan finished
        except Exception:
            pass
        time.sleep(2)


def get_artist_tokens(artist: str) -> set[str]:
    """Extract artist name variations and significant word tokens."""
    if not artist:
        return set()
    parts = re.split(r"[,/&+\|]|\bfeat\.?\b|\bft\.?\b|\bwith\b", artist, flags=re.IGNORECASE)
    tokens = set()
    for p in parts:
        norm = " ".join(re.sub(r"[^\w\s]", " ", p).lower().split())
        if norm:
            tokens.add(norm)
            for word in norm.split():
                if len(word) > 2:
                    tokens.add(word)
    return tokens


def navidrome_search_track(title: str, artist: str, album_artist: str | None = None) -> str | None:
    """Search Navidrome SQLite DB directly for a track. Requires both title AND artist matching (so cover versions aren't mixed up)."""
    clean_t = clean_title_for_lyrics(title)
    clean_a = clean_artist_for_lyrics(artist)

    generic_channels = ["release - topic", "various artists", "topic", "youtube", "music"]
    if clean_a.lower() in generic_channels and album_artist:
        clean_a = clean_artist_for_lyrics(album_artist)

    if not clean_t:
        return None

    try:
        conn = sqlite3.connect(DB_PATH)
        try:
            cur = conn.cursor()
            rows = cur.execute("""
                SELECT id, title, artist, path FROM media_file
                WHERE missing = 0 AND (lower(title) LIKE lower(?) OR lower(path) LIKE lower(?))
            """, (f"%{clean_t}%", f"%{clean_t}%")).fetchall()

            in_tokens = get_artist_tokens(clean_a) if clean_a and clean_a.lower() not in generic_channels else set()

            for r in rows:
                mid, db_title, db_artist, db_path = r
                db_tokens = get_artist_tokens(db_artist or "")

                # If artist is known, require matching artist tokens or sub-string match
                if in_tokens and db_tokens:
                    if in_tokens & db_tokens:
                        return mid
                    if clean_a.lower() in (db_artist or "").lower() or (db_artist or "").lower() in clean_a.lower():
                        return mid
                elif not in_tokens:
                    # If artist is completely unknown, only match if clean title is exact
                    if clean_t.lower() == (db_title or "").lower():
                        return mid
        finally:
            conn.close()
    except Exception as e:
        print(f"[Navidrome search track error] {e}", flush=True)
    return None


def _get_owner_id() -> str:
    """Retrieve owner user id from environment or Navidrome DB."""
    env_owner = os.getenv("NAVI_OWNER_ID")
    if env_owner:
        return env_owner
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute("SELECT id FROM user WHERE is_admin = 1 LIMIT 1").fetchone()
        conn.close()
        if row and row[0]:
            return row[0]
    except Exception:
        pass
    print("[WARN] NAVI_OWNER_ID not found in .env or database user table.", flush=True)
    return ""


def navidrome_create_playlist(name: str, song_ids: list[str]) -> bool:
    """Create (or replace) a Navidrome playlist by name directly in the database."""
    if not song_ids:
        return False
    # Deduplicate while preserving order
    song_ids = list(dict.fromkeys(song_ids))
    try:
        import uuid
        now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.execute("PRAGMA journal_mode=WAL")

            existing = conn.execute("SELECT id FROM playlist WHERE name = ? AND owner_id = ?", (name, _get_owner_id())).fetchone()
            if existing:
                pl_id = existing[0]
                conn.execute("DELETE FROM playlist_tracks WHERE playlist_id = ?", (pl_id,))
                conn.execute("UPDATE playlist SET updated_at = ?, comment = ? WHERE id = ?", (now, "Imported by PSudofy Discord Bot", pl_id))
            else:
                pl_id = uuid.uuid4().hex[:20]
                conn.execute("""
                    INSERT INTO playlist (id, name, comment, public, path, sync, owner_id, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (pl_id, name, "Imported by PSudofy Discord Bot", 0, "", 0, _get_owner_id(), now, now))

            for pos, sid in enumerate(song_ids, 1):
                conn.execute("INSERT OR IGNORE INTO playlist_tracks (id, playlist_id, media_file_id) VALUES (?, ?, ?)", (pos, pl_id, sid))

            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as e:
        print(f"[Playlist DB write error] {e}", flush=True)
        return False


FAILED_DOWNLOADS_FILE = os.path.expanduser("~/PSudofy/failed_downloads.json")
MISSING_LYRICS_FILE   = os.path.expanduser("~/PSudofy/missing_lyrics.json")


def log_failed_download(query: str, reason: str):
    """Save a failed download to failed_downloads.json."""
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    item = {"query": query, "reason": str(reason)[:200], "timestamp": ts}
    try:
        with _json_lock:
            data = []
            if os.path.exists(FAILED_DOWNLOADS_FILE):
                with open(FAILED_DOWNLOADS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
            data = [x for x in data if x.get("query") != query]
            data.append(item)
            with open(FAILED_DOWNLOADS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[Failed DL Log Error] {e}", flush=True)


def remove_from_failed_downloads(query: str):
    """Remove a query from failed_downloads.json upon success."""
    try:
        with _json_lock:
            if os.path.exists(FAILED_DOWNLOADS_FILE):
                with open(FAILED_DOWNLOADS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                data = [x for x in data if x.get("query") != query]
                with open(FAILED_DOWNLOADS_FILE, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def log_missing_lyrics(title: str, artist: str, mp3_path: str = "", reason: str = "Not on LRCLib"):
    """Save a song that is missing lyrics to missing_lyrics.json."""
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    key = f"{title} - {artist}".lower()
    item = {"title": title, "artist": artist, "path": mp3_path, "reason": reason, "timestamp": ts}
    try:
        with _json_lock:
            data = []
            if os.path.exists(MISSING_LYRICS_FILE):
                with open(MISSING_LYRICS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
            data = [x for x in data if f"{x.get('title')} - {x.get('artist')}".lower() != key]
            data.append(item)
            with open(MISSING_LYRICS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[Missing Lyrics Log Error] {e}", flush=True)


def remove_from_missing_lyrics(title: str, artist: str):
    """Remove song from missing_lyrics.json once lyrics are embedded."""
    key = f"{title} - {artist}".lower()
    try:
        with _json_lock:
            if os.path.exists(MISSING_LYRICS_FILE):
                with open(MISSING_LYRICS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                data = [x for x in data if f"{x.get('title')} - {x.get('artist')}".lower() != key]
                with open(MISSING_LYRICS_FILE, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def get_failed_downloads() -> list:
    """Return all failed downloads from failed_downloads.json."""
    try:
        if os.path.exists(FAILED_DOWNLOADS_FILE):
            with open(FAILED_DOWNLOADS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return []


def get_missing_lyrics() -> list:
    """Return all tracks missing lyrics from missing_lyrics.json."""
    try:
        if os.path.exists(MISSING_LYRICS_FILE):
            with open(MISSING_LYRICS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return []


def write_dl_log(entry: dict):
    """Append a download result to the JSON log (max 100 entries kept)."""
    entry["ts"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    try:
        with _json_lock:
            if os.path.exists(DL_LOG_FILE):
                with open(DL_LOG_FILE, "r") as f:
                    log = json.load(f)
            else:
                log = []
            log.append(entry)
            log = log[-100:]   # keep only last 100
            with open(DL_LOG_FILE, "w") as f:
                json.dump(log, f, indent=2)
    except Exception:
        pass


def read_dl_log(n: int = 10) -> list:
    """Return the last n entries from the download log."""
    try:
        if not os.path.exists(DL_LOG_FILE):
            return []
        with open(DL_LOG_FILE, "r") as f:
            log = json.load(f)
        return log[-n:]
    except Exception:
        return []


def is_playlist_url(url: str) -> bool:
    """Detect YouTube/Spotify/SoundCloud playlist, album, artist, and channel URLs."""
    if not url:
        return False
    # 1. YouTube playlists & albums (supports PL..., OLAK..., RD..., etc.)
    if re.search(r"([?&]list=|/playlist\b)", url, re.IGNORECASE):
        return True
    # 2. Spotify playlists, albums, artists
    if is_spotify_url(url) and any(k in url for k in ["/playlist/", "/album/", "/artist/"]):
        return True
    # 3. SoundCloud sets / albums
    if "soundcloud.com" in url and "/sets/" in url:
        return True
    # 4. YouTube channel / artist pages: /@channelname, /channel/UCxxx, /c/name, /user/name
    if re.search(r"youtube\.com/(@[^/?#]+|channel/[^/?#]+|c/[^/?#]+|user/[^/?#]+)", url, re.IGNORECASE):
        return True
    return False


def channel_url_to_uploads(url: str) -> str:
    """Convert a YouTube channel/artist URL to its uploads playlist URL (/videos)."""
    if re.search(r"youtube\.com/(@[^/?#]+|channel/[^/?#]+|c/[^/?#]+|user/[^/?#]+)", url):
        base = re.split(r"[?#]", url)[0].rstrip("/")
        return base + "/videos"
    return url


def is_spotify_url(url: str) -> bool:
    """Detect Spotify URLs."""
    return "open.spotify.com" in url or "spotify.com" in url


# ─────────────────────────────── Lyrics ───────────────────────────────────────

def clean_title_for_lyrics(title: str) -> str:
    for pat in [
        r"\s*\(from\s+[^)]+\)", r"\s*-\s*from\s+.+",
        r"\s*\(feat\.?\s+[^)]+\)", r"\s*\(ft\.?\s+[^)]+\)",
        r"\s*\(official.*?\)", r"\s*\[official.*?\]",
        r"\s*\(lyrics?\)", r"\s*\[lyrics?\]",
        r"\s*\(audio\)", r"\s*\[audio\]",
        r"\s*\(visualizer\)", r"\s*\[visualizer\]",
        r"\s*\(lyric video\)", r"\s*\[lyric video\]",
        r"\s*\|\s*.+",
    ]:
        title = re.sub(pat, "", title, flags=re.IGNORECASE)
    return title.strip()


def clean_artist_for_lyrics(artist: str) -> str:
    """Use only the first credited artist for better matching."""
    return re.split(r"[,/&]|\bfeat\b|\bft\b", artist, flags=re.IGNORECASE)[0].strip()


def fetch_lyrics(title: str, artist: str) -> tuple[str | None, str | None]:
    """Fetch lyrics from lrclib.net. Returns (plain, synced)."""
    clean_t = clean_title_for_lyrics(title)
    clean_a = clean_artist_for_lyrics(artist)

    # Try with artist
    for params_dict in [
        {"track_name": clean_t, "artist_name": clean_a},
        {"track_name": clean_t},                        # fallback: title only
    ]:
        try:
            url = f"https://lrclib.net/api/get?{urllib.parse.urlencode(params_dict)}"
            req = urllib.request.Request(url, headers={"User-Agent": "PSudofy-Bot/1.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    if data.get("syncedLyrics") or data.get("plainLyrics"):
                        return data.get("plainLyrics"), data.get("syncedLyrics")
        except Exception:
            pass
    return None, None


def parse_lrc(lrc: str):
    result = []
    for line in lrc.splitlines():
        m = re.match(r"\[(\d+):(\d+)(?:\.(\d+))?\](.*)", line.strip())
        if m:
            ms = (int(m.group(1)) * 60 + int(m.group(2))) * 1000 + int(m.group(3) or 0) * 10
            text = m.group(4).strip()
            if text:
                result.append((text, ms))
    return result


def embed_lyrics_to_file(mp3_path: str, plain: str | None, synced: str | None, title: str = "", artist: str = "") -> str:
    """Embed lyrics into mp3 tags and manage missing_lyrics.json."""
    if not MUTAGEN:
        return "⚠️ mutagen not installed"
    if not os.path.exists(mp3_path):
        return "⚠️ File not found after download"
    if not plain and not synced:
        if title:
            log_missing_lyrics(title, artist, mp3_path, "Not found on LRCLib")
        return "❌ Not found on LRCLib"

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
                tags.add(SYLT(encoding=Encoding.UTF8, lang="eng",
                              format=2, type=1, desc="", text=parsed))

        tags.save(mp3_path, v2_version=3)

        if title:
            remove_from_missing_lyrics(title, artist)

        if synced:
            return "✅ Synced (time-stamped)"
        return "✅ Plain text"
    except Exception as e:
        if title:
            log_missing_lyrics(title, artist, mp3_path, f"Embed error: {e}")
        return f"⚠️ Embed error: {e}"


def _get_active_proxy() -> str | None:
    """Check if Cloudflare WARP proxy is running locally on port 40000 with remote DNS resolution."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", 40000))
        s.close()
        return "socks5h://127.0.0.1:40000"
    except Exception:
        return None


def _build_ydl_opts(progress_hook=None, ignore_errors: bool = True, logger=None, use_archive: bool = True) -> dict:
    opts = {
        "format": "bestaudio/best",
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"},
            {"key": "FFmpegMetadata"},
            {"key": "EmbedThumbnail"},
        ],
        "outtmpl":            os.path.join(MUSIC_FOLDER, "%(artist,uploader)s", "%(title)s.%(ext)s"),
        "writethumbnail":     True,
        "quiet":              True,
        "no_warnings":        True,
        "ignoreerrors":       ignore_errors,
        "extract_flat":       False,
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "web_creator", "mweb", "web"],
            }
        },
    }
    proxy = _get_active_proxy()
    if proxy:
        opts["proxy"] = proxy
    if use_archive:
        opts["download_archive"] = ARCHIVE_FILE
    if logger:
        opts["logger"] = logger
    if os.path.exists(COOKIES_PATH) and os.path.getsize(COOKIES_PATH) > 0:
        opts["cookiefile"] = COOKIES_PATH
    if progress_hook:
        opts["progress_hooks"] = [progress_hook]
    return opts


def process_single_entry(ydl, entry: dict | None) -> dict:
    """Download lyrics and embed them for a single track entry."""
    if entry is None:
        return {"title": "Unknown", "artist": "Unknown", "thumb": None, "lyrics": "—", "mp3": ""}

    title  = entry.get("title") or "Unknown Title"
    artist = entry.get("artist") or entry.get("uploader") or entry.get("channel") or "Unknown Artist"
    thumb  = entry.get("thumbnail")

    filename = ydl.prepare_filename(entry)
    mp3_path = os.path.splitext(filename)[0] + ".mp3"

    plain, synced = fetch_lyrics(title, artist)
    lyrics_status = embed_lyrics_to_file(mp3_path, plain, synced, title=title, artist=artist)

    return {
        "title":   title,
        "artist":  artist,
        "thumb":   thumb,
        "lyrics":  lyrics_status,
        "mp3":     mp3_path,
    }


YT_BOT_PHRASES = ["Sign in to confirm", "bot", "cookies", "not a bot", "automated queries", "captcha", "429"]

def _is_yt_blocked(err: str) -> bool:
    return any(p.lower() in err.lower() for p in YT_BOT_PHRASES)


def run_single_download(query: str) -> dict:
    import yt_dlp

    query = query.strip()
    url_match = re.search(r"(https?://[^\s]+)", query)
    url = url_match.group(1) if url_match else None

    # Handle single Spotify track links: extract title+artist, then search YouTube
    if url and is_spotify_url(url) and "/track/" in url:
        name, tracks = get_spotify_tracks(url)
        if tracks:
            t = tracks[0]
            query = f"{t['title']} {t['artist']}"
            target = f"ytsearch1:{query} song official audio"
        else:
            return {"success": False, "error": "NO_RESULTS"}
    else:
        target = url if url else f"ytsearch1:{query} song official audio"

    captured_errors = []

    class SingleLogger:
        def debug(self, msg):   pass
        def info(self, msg):    pass
        def warning(self, msg): pass
        def error(self, msg):   captured_errors.append(msg)

    try:
        ydl_opts = _build_ydl_opts(ignore_errors=False, logger=SingleLogger(), use_archive=False)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                info = ydl.extract_info(target, download=True)
            except Exception as e_dl:
                # If proxy connection refused/failed, retry once with direct fallback
                if ("Socks" in str(e_dl) or "proxy" in str(e_dl).lower() or "111" in str(e_dl)) and "proxy" in ydl_opts:
                    print(f"[run_single_download proxy error: {e_dl}] Retrying without proxy...", flush=True)
                    fallback_opts = dict(ydl_opts)
                    fallback_opts.pop("proxy", None)
                    with yt_dlp.YoutubeDL(fallback_opts) as fallback_ydl:
                        info = fallback_ydl.extract_info(target, download=True)
                else:
                    raise e_dl

            if not info:
                err_joined = " ".join(captured_errors)
                print(f"[run_single_download failed: {query}] {err_joined}", flush=True)
                if _is_yt_blocked(err_joined):
                    log_failed_download(query, "YouTube blocked server IP")
                    write_dl_log({"type": "single", "title": query, "artist": "?", "lyrics": "—", "status": "FAILED: YouTube blocked server IP"})
                    return {"success": False, "error": "YT_BLOCKED"}
                log_failed_download(query, err_joined or "No search results found")
                return {"success": False, "error": "NO_RESULTS"}

            # Handle entries list — skip any None entries
            if "entries" in info:
                entries = [e for e in (info["entries"] or []) if e is not None]
                entry = entries[0] if entries else None
            else:
                entry = info

            if entry is None:
                err_joined = " ".join(captured_errors)
                print(f"[run_single_download empty entry: {query}] {err_joined}", flush=True)
                if _is_yt_blocked(err_joined):
                    log_failed_download(query, "YouTube blocked server IP")
                    write_dl_log({"type": "single", "title": query, "artist": "?", "lyrics": "—", "status": "FAILED: YouTube blocked server IP"})
                    return {"success": False, "error": "YT_BLOCKED"}
                log_failed_download(query, err_joined or "No search results found")
                return {"success": False, "error": "NO_RESULTS"}

            track = process_single_entry(ydl, entry)
            trigger_navidrome_scan()

        write_dl_log({
            "type":   "single",
            "title":  track["title"],
            "artist": track["artist"],
            "lyrics": track["lyrics"],
            "status": "ok",
        })
        remove_from_failed_downloads(query)
        return {"success": True, **track}

    except Exception as e:
        err_str = str(e)
        print(f"[run_single_download exception: {query}] {err_str}", flush=True)
        if _is_yt_blocked(err_str) or any(_is_yt_blocked(err) for err in captured_errors):
            log_failed_download(query, "YouTube blocked server IP")
            write_dl_log({"type": "single", "title": query, "artist": "?", "lyrics": "—", "status": "FAILED: YouTube blocked server IP"})
            return {"success": False, "error": "YT_BLOCKED"}
        log_failed_download(query, err_str)
        write_dl_log({"type": "single", "title": query, "artist": "?", "lyrics": "—", "status": f"FAILED: {e}"})
        return {"success": False, "error": err_str}


def run_playlist_download(url: str, progress_cb=None, create_playlist: bool = True, custom_name: str | None = None) -> dict:
    """
    Download a full YouTube/YouTube Music playlist/album.
    Extracts tracklist instantly, checks library for existing songs,
    and downloads only missing tracks with live Discord progress updates.
    """
    import yt_dlp

    class PlaylistLogger:
        def debug(self, msg):   pass
        def info(self, msg):    pass
        def warning(self, msg): pass
        def error(self, msg):   pass

    opts = _build_ydl_opts()
    opts["logger"] = PlaylistLogger()

    try:
        # 1. Fast metadata extraction (extract_flat=True takes ~1-2 seconds)
        flat_entries_meta = []
        flat_opts = {**opts, "extract_flat": True, "quiet": True}
        try:
            with yt_dlp.YoutubeDL(flat_opts) as ydl:
                flat = ydl.extract_info(url, download=False)
        except Exception as e_flat:
            if ("Socks" in str(e_flat) or "proxy" in str(e_flat).lower() or "111" in str(e_flat)) and "proxy" in flat_opts:
                print(f"[run_playlist_download flat extract proxy error: {e_flat}] Retrying without proxy...", flush=True)
                flat_opts.pop("proxy", None)
                with yt_dlp.YoutubeDL(flat_opts) as fallback_ydl:
                    flat = fallback_ydl.extract_info(url, download=False)
            else:
                raise e_flat

        if not flat:
            return {"success": False, "error": "Could not fetch playlist metadata"}

        playlist_title = flat.get("title", "Playlist")
        entries = flat.get("entries") or [flat]
        for e in entries:
            if e:
                flat_entries_meta.append({
                    "title":    e.get("title") or "Unknown Title",
                    "artist":   e.get("artist") or e.get("uploader") or e.get("channel") or "Unknown Artist",
                    "video_id": e.get("id") or e.get("video_id") or "",
                    "url":      e.get("url") or (f"https://www.youtube.com/watch?v={e.get('id')}" if e.get("id") else ""),
                })

        total_count = len(flat_entries_meta)
        if custom_name:
            playlist_title = custom_name
        elif playlist_title.isdigit() and total_count > 0:
            playlist_title = f"Playlist ({total_count} tracks)"

        if progress_cb:
            progress_cb(0, total_count, f"Found {total_count} songs in \"{playlist_title}\"")

        album_artist = flat.get("uploader") or flat.get("artist") or ""
        if "sanam" in playlist_title.lower():
            album_artist = "SANAM"

        # 2. Check local Navidrome library for existing tracks
        matched_ids = []
        downloaded_tracks = []
        missing_to_download = []

        for idx, t in enumerate(flat_entries_meta, 1):
            sid = navidrome_search_track(t["title"], t["artist"], album_artist=album_artist)
            if sid:
                matched_ids.append(sid)
                downloaded_tracks.append({
                    "title":        t["title"],
                    "artist":       t["artist"],
                    "lyrics":       "✅ In library",
                    "navidrome_id": sid,
                })
                if progress_cb:
                    progress_cb(idx, total_count, f"In library: {t['title']}")
            else:
                missing_to_download.append(t)

        # 3. Download truly missing tracks with live per-song progress
        failed_count = 0
        if missing_to_download:
            for idx, t in enumerate(missing_to_download, len(downloaded_tracks) + 1):
                if progress_cb:
                    progress_cb(idx, total_count, f"Downloading: {t['title']}")
                target = t.get("url") or (f"https://www.youtube.com/watch?v={t['video_id']}" if t.get("video_id") else f"{t['title']} {t['artist']}")
                res = run_single_download(target)
                if res.get("success"):
                    downloaded_tracks.append(res)
                else:
                    failed_count += 1

            # 4. Trigger library scan after downloads finish
            trigger_navidrome_scan()
            navidrome_wait_scan(timeout=90)

            # 5. Match newly downloaded tracks in Navidrome
            for track in downloaded_tracks:
                if not track.get("navidrome_id"):
                    sid = navidrome_search_track(track["title"], track["artist"], album_artist=album_artist)
                    if sid:
                        track["navidrome_id"] = sid
                        if sid not in matched_ids:
                            matched_ids.append(sid)

        # 6. Create Navidrome playlist
        playlist_created = False
        if create_playlist and matched_ids:
            playlist_created = navidrome_create_playlist(playlist_title, matched_ids)

        for track in downloaded_tracks:
            write_dl_log({
                "type":     "playlist",
                "title":    track["title"],
                "artist":   track["artist"],
                "lyrics":   track.get("lyrics", "—"),
                "status":   "ok",
                "playlist": playlist_title if create_playlist else "None (Raw Library)",
            })

        return {
            "success":          True,
            "is_playlist":      True,
            "playlist_title":   playlist_title,
            "downloaded":       downloaded_tracks,
            "failed_count":     failed_count,
            "total":            len(downloaded_tracks),
            "playlist_created": playlist_created,
            "playlist_songs":   len(matched_ids),
            "skip_playlist":    not create_playlist,
        }
    except Exception as e:
        write_dl_log({"type": "playlist", "title": url, "artist": "?", "lyrics": "—", "status": f"FAILED: {e}"})
        return {"success": False, "error": str(e)}


# ─────────────────────────────── Bot Events ───────────────────────────────────

@bot.event
async def on_ready():
    global _download_lock, _download_semaphore, bot_start_time
    _download_lock = asyncio.Lock()  # must be created inside the running loop
    _download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
    bot_start_time = datetime.datetime.now(datetime.timezone.utc)
    print(f"[PSudofy Bot] Logged in as {bot.user.name} ({bot.user.id})", flush=True)
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.listening,
            name="!dl <song / playlist link>"
        )
    )
    if WEEKLY_CHANNEL_ID:
        if not weekly_autodl.is_running():
            weekly_autodl.start()
        print(f"[PSudofy Bot] Weekly auto-download scheduled → channel {WEEKLY_CHANNEL_ID}", flush=True)
    else:
        print("[PSudofy Bot] WEEKLY_CHANNEL_ID not set — weekly auto-download disabled", flush=True)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return  # silently ignore typos like !dls
    embed = discord.Embed(
        title="⚠️ Something went wrong",
        description=f"```{str(error)[:300]}```",
        color=0xef4444
    )
    await ctx.reply(embed=embed)


# ─────────────────────────────── Commands ─────────────────────────────────────

@bot.command(name="dl", aliases=["download", "d", "song", "add"])
async def download_cmd(ctx, *, query: str = None):
    if not query:
        embed = discord.Embed(
            title="❓ How to Download",
            description=(
                "**Single song:**\n"
                "`!dl Kesariya Arijit Singh`\n"
                "`!dl Tame Impala - Loser`\n"
                "`!dl https://youtu.be/...`\n\n"
                "**Full playlist / album (auto-groups into playlist):**\n"
                "`!dl https://open.spotify.com/playlist/...`\n"
                "`!dl https://youtube.com/playlist?list=...`\n\n"
                "**Download songs WITHOUT creating a playlist:**\n"
                "`!dl <link> --no-playlist` (or `!dlnp <link>`)\n\n"
                "**Custom playlist name:**\n"
                "`!dl <link> --as \"Workout Hits\"`\n\n"
                "ℹ️ Downloads happen on the **server** — uses zero phone data!"
            ),
            color=0x38bdf8
        )
        await ctx.reply(embed=embed)
        return

    create_playlist = True
    custom_name = None

    # Parse --no-playlist / -np / --raw / --solo flags
    if re.search(r"(?:--no-playlist|-np|--nopl|--solo|--raw)\b", query, re.IGNORECASE):
        create_playlist = False
        query = re.sub(r"(?:--no-playlist|-np|--nopl|--solo|--raw)\b", "", query, flags=re.IGNORECASE).strip()

    # Parse --as "Custom Name" (quotes required to avoid eating the URL)
    m_as = re.search(r'(?:--as|-as)\s+["\']([^"\']+)["\']', query, re.IGNORECASE)
    if m_as:
        custom_name = m_as.group(1).strip()
        query = re.sub(r'(?:--as|-as)\s+["\']([^"\']+)["\']', "", query, flags=re.IGNORECASE).strip()

    url_match = re.search(r"(https?://[^\s]+)", query)
    url = url_match.group(1) if url_match else None

    # Expand YouTube channel/artist pages (/@handle, /channel/UC...) to /videos
    if url:
        expanded = channel_url_to_uploads(url)
        if expanded != url:
            query = query.replace(url, expanded)
            url = expanded

    is_playlist = url and is_playlist_url(url)

    # Single Spotify track links should not be treated as playlists
    if url and is_spotify_url(url) and "/track/" in url:
        is_playlist = False

    if is_playlist:
        await _handle_playlist(ctx, url, create_playlist=create_playlist, custom_name=custom_name)
    else:
        await _handle_single(ctx, query)


@bot.command(name="dlnp", aliases=["dlraw", "nopl", "songs"])
async def download_no_playlist_cmd(ctx, *, query: str = None):
    """Download songs from link directly to library without creating any playlist."""
    if not query:
        await ctx.reply("ℹ️ Usage: `!dlnp <playlist or song link>` — downloads every track straight into your library without grouping into a playlist.")
        return
    await ctx.invoke(download_cmd, query=f"{query} --no-playlist")


async def _handle_single(ctx, query: str):
    global _download_semaphore
    if _download_semaphore is None:
        _download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

    msg = await ctx.reply(embed=discord.Embed(
        title="⏳  Fetching Audio & Lyrics…",
        description=(
            f"**Query:** `{query[:100]}`"
        ),
        color=0x5865F2
    ).set_footer(text="PSudofy  •  Downloading on server — your device uses zero data"))

    loop = asyncio.get_running_loop()
    async with _download_semaphore:
        result = await loop.run_in_executor(None, run_single_download, query)

    if result.get("success"):
        lyrics = result.get("lyrics", "—")
        if "✅" in lyrics and "Synced" in lyrics:
            lyrics_line = "🎤 Synced (time-stamped)"
        elif "✅" in lyrics:
            lyrics_line = "📄 Plain text"
        else:
            lyrics_line = f"⚠️ {lyrics}"

        embed = discord.Embed(
            title=f"🎵  {result['title']}",
            color=0x1DB954,   # Spotify green
        )
        embed.add_field(name="👤 Artist",  value=result["artist"], inline=True)
        embed.add_field(name="🎤 Lyrics",  value=lyrics_line,       inline=True)
        embed.add_field(name="\u200b",    value="\u200b",            inline=True)  # spacer
        embed.add_field(
            name="📲 Ready in Symfonium",
            value="Pull to refresh → the track is live in your library!",
            inline=False
        )
        if result.get("thumb"):
            embed.set_thumbnail(url=result["thumb"])
        embed.set_footer(text=f"PSudofy  •  Added {datetime.datetime.now().strftime('%H:%M')}")
        await msg.edit(embed=embed)

    elif result.get("error") == "YT_BLOCKED":
        embed = discord.Embed(
            title="🚫  YouTube Blocked the Server",
            description=(
                "Your Oracle Cloud IP is flagged as a datacenter — YouTube wants cookie auth.\n\n"
                "**Quick fix (2 min):**\n"
                "1. Install **[Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbngbenkjcffliehddfacccg)**\n"
                "2. Visit [youtube.com](https://youtube.com) while **logged in**\n"
                "3. Click the extension → **Export** → save as `cookies.txt` in your `PSudofy/` folder\n"
                "4. Tell me **done** and I'll upload it instantly!\n\n"
                "*After that, all downloads will work permanently.* 🚀"
            ),
            color=0xFEE75C,
        )
        embed.set_footer(text="PSudofy  •  Auth cookies expire every ~3 months")
        await msg.edit(embed=embed)

    elif result.get("error") == "NO_RESULTS":
        embed = discord.Embed(
            title="🔍  No Results",
            description=(
                f"Nothing found for `{query[:100]}`\n\n"
                "**Tips:**\n"
                "• Add artist name → `!dl Song Name - Artist`\n"
                "• Use a direct YouTube or Spotify link\n"
                "• Check spelling"
            ),
            color=0xED4245,
        )
        embed.set_footer(text="PSudofy")
        await msg.edit(embed=embed)

    else:
        err = result.get("error", "Unknown error")[:300]
        embed = discord.Embed(
            title="❌  Download Failed",
            description=f"```\n{err}\n```",
            color=0xED4245,
        )
        embed.set_footer(text="PSudofy  •  Try again or use a direct link")
        await msg.edit(embed=embed)



async def _handle_playlist(ctx, url: str, create_playlist: bool = True, custom_name: str | None = None):
    global _download_lock
    if _download_lock is None:
        _download_lock = asyncio.Lock()

    # Prevent concurrent playlist downloads on limited-RAM server
    if _download_lock.locked():
        await ctx.reply(embed=discord.Embed(
            title="⏳ Download Already Running",
            description="Another playlist is downloading right now. Use `!cancel` to stop it, or wait for it to finish.",
            color=0xf59e0b
        ))
        return

    async with _download_lock:
        await _handle_playlist_inner(ctx, url, create_playlist, custom_name)


async def _handle_playlist_inner(ctx, url: str, create_playlist: bool, custom_name: str | None):
    source = "Spotify" if is_spotify_url(url) else "YouTube"
    source_emoji = "🟢" if source == "Spotify" else "🔴"

    msg = await ctx.reply(embed=discord.Embed(
        title=f"{source_emoji}  Scanning {source} Playlist…",
        description=f"`{url[:80]}`",
        color=0x5865F2
    ).set_footer(text="PSudofy  •  This may take a moment for large playlists"))

    progress_queue: asyncio.Queue = asyncio.Queue()

    def progress_cb(done, total, current_title):
        asyncio.run_coroutine_threadsafe(
            progress_queue.put((done, total, current_title)),
            bot.loop
        )

    loop = asyncio.get_running_loop()
    if is_spotify_url(url):
        dl_task = loop.run_in_executor(None, run_spotify_playlist_download, url, progress_cb, create_playlist, custom_name)
    else:
        dl_task = loop.run_in_executor(None, run_playlist_download, url, progress_cb, create_playlist, custom_name)

    ACTIVE_TASKS.add(dl_task)

    # Update Discord every time a track finishes
    last_edit = asyncio.get_event_loop().time()
    try:
        while not dl_task.done():
            try:
                done, total, title = await asyncio.wait_for(progress_queue.get(), timeout=2.5)
                now = asyncio.get_event_loop().time()
                # Throttle edits to max 1 per 2.5 seconds (Discord rate limit)
                if now - last_edit >= 2.5:
                    pct = int((done / total) * 100) if total else 0
                    filled = round(pct / 10)
                    bar = "█" * filled + "░" * (10 - filled)
                    embed = discord.Embed(
                        title="📥  Downloading…",
                        description=(
                            f"`{bar}`  **{pct}%**\n"
                            f"**{done}** of **{total}** tracks processed\n"
                            f"▶  *{title[:60]}*"
                        ),
                        color=0x5865F2
                    )
                    embed.set_footer(text="PSudofy  •  Use !cancel to stop  •  Your device uses zero data")
                    await msg.edit(embed=embed)
                    last_edit = now
            except asyncio.TimeoutError:
                pass

        result = await dl_task
    except asyncio.CancelledError:
        ACTIVE_TASKS.discard(dl_task)
        await msg.edit(embed=discord.Embed(
            title="🛑  Download Cancelled",
            description=(
                f"Stopped playlist download for:\n`{url[:80]}`\n\n"
                "Tracks downloaded so far have been kept. Use `!dl` to start a new download."
            ),
            color=0xED4245
        ).set_footer(text="PSudofy"))
        return

    ACTIVE_TASKS.discard(dl_task)

    if result.get("success"):
        tracks     = result["downloaded"]
        total_n    = result["total"]
        lyrics_ok  = sum(1 for t in tracks if "✅" in t.get("lyrics", ""))
        failed_n   = result["failed_count"]
        pl_created = result.get("playlist_created", False)
        pl_songs   = result.get("playlist_songs", 0)
        pl_name    = result.get("playlist_title", "Playlist")
        skipped_pl = result.get("skip_playlist", False)

        # Intermediate "building" message only when creating playlist
        if not skipped_pl:
            await msg.edit(embed=discord.Embed(
                title="🗂️  Building Playlist…",
                description=f"Processed **{total_n}** tracks. Writing **\"{pl_name[:50]}\"** to Navidrome…",
                color=0x5865F2
            ))

        # Pick title/color based on outcome
        if total_n == 0:
            embed_color = 0xED4245
            embed_title = f"❌  {pl_name[:52]} — all failed"
        elif pl_created and pl_songs > 0 and failed_n == 0:
            embed_color = 0x1DB954
            embed_title = f"✅  {pl_name[:55]}"
        elif skipped_pl and total_n > 0 and failed_n == 0:
            embed_color = 0x1DB954
            embed_title = f"✅  {pl_name[:55]}"
        elif pl_created or pl_songs > 0 or (skipped_pl and total_n > 0):
            embed_color = 0xFEE75C
            embed_title = f"⚠️  {pl_name[:52]} (partial)"
        elif total_n > 0:
            embed_color = 0xFEE75C
            embed_title = f"⚠️  {pl_name[:52]} (no playlist)"
        else:
            embed_color = 0xED4245
            embed_title = f"❌  {pl_name[:52]} — all failed"

        embed = discord.Embed(title=embed_title, color=embed_color)

        # Stats row
        embed.add_field(name="🎵 Tracks",    value=f"**{total_n}**",                    inline=True)
        embed.add_field(name="📝 Lyrics",    value=f"**{lyrics_ok}/{total_n}**",         inline=True)
        embed.add_field(name="⚠️ Not Found", value=f"**{failed_n}**",                    inline=True)

        # Playlist status
        if skipped_pl:
            pl_value = "⏭️ Songs added directly to library — no playlist created"
        elif pl_created:
            pl_value = f"📑 **\"{pl_name[:45]}\"** — {pl_songs} tracks ready in Symfonium!"
        else:
            pl_value = "⚠️ Playlist creation failed — tracks are still in your library"
        embed.add_field(name="Navidrome", value=pl_value, inline=False)

        # Track preview (title + lyrics status)
        if tracks:
            def _track_line(i, t):
                lyr = t.get("lyrics", "—")
                lyr_icon = "🎤" if "Synced" in lyr else ("📄" if "✅" in lyr else "⚠️")
                return f"`{i+1:2}.` **{t['title'][:40]}** — {t.get('artist','')[:22]}  {lyr_icon}"

            preview_lines = [_track_line(i, t) for i, t in enumerate(tracks[:6])]
            if len(tracks) > 6:
                preview_lines.append(f"*…and {len(tracks) - 6} more tracks*")
            embed.add_field(name="🎶 Tracks", value="\n".join(preview_lines), inline=False)

        embed.set_footer(text=f"PSudofy  •  Completed {datetime.datetime.now().strftime('%H:%M')}  •  Pull to refresh in Symfonium")
        await msg.edit(embed=embed)

    else:
        await msg.edit(embed=discord.Embed(
            title="❌  Playlist Failed",
            description=f"```\n{result.get('error', 'Unknown error')[:400]}\n```",
            color=0xED4245
        ).set_footer(text="PSudofy  •  Try again or use a direct link"))


@bot.command(name="scan")
async def scan_cmd(ctx):
    status_msg = await ctx.reply(embed=discord.Embed(
        title="🔄  Scanning Library…",
        description="Navidrome is scanning your music folder for new songs & albums…",
        color=0x5865F2
    ).set_footer(text="PSudofy  •  Checking local files"))

    loop = asyncio.get_running_loop()
    ok = await loop.run_in_executor(None, trigger_navidrome_scan)

    # Fetch live counts from Navidrome DB
    try:
        conn = sqlite3.connect(DB_PATH)
        try:
            cur = conn.cursor()
            songs = cur.execute("SELECT COUNT(*) FROM media_file WHERE missing=0").fetchone()[0]
            artists = cur.execute("SELECT COUNT(DISTINCT artist) FROM media_file WHERE missing=0").fetchone()[0]
            albums = cur.execute("SELECT COUNT(DISTINCT album) FROM media_file WHERE missing=0").fetchone()[0]
        finally:
            conn.close()
    except Exception:
        songs, artists, albums = 0, 0, 0

    if ok:
        embed = discord.Embed(
            title="✅  Library Scan Complete",
            description=(
                f"Your library now has **{songs:,}** songs across **{artists:,}** artists & **{albums:,}** albums.\n\n"
                "📲 **In Symfonium:** Pull down to refresh to see your new music!"
            ),
            color=0x1DB954
        )
    else:
        embed = discord.Embed(
            title="⚠️  Scan Completed",
            description=(
                f"Your library has **{songs:,}** songs.\n\n"
                "📲 **In Symfonium:** Pull down to refresh to check for new tracks."
            ),
            color=0xFEE75C
        )
    embed.set_footer(text=f"PSudofy  •  Scanned at {datetime.datetime.now().strftime('%H:%M')}")
    await status_msg.edit(embed=embed)


@bot.command(name="stats")
async def stats_cmd(ctx):
    try:
        conn = sqlite3.connect(DB_PATH)
        try:
            cur     = conn.cursor()
            songs   = cur.execute("SELECT COUNT(*) FROM media_file WHERE missing=0").fetchone()[0]
            artists = cur.execute("SELECT COUNT(DISTINCT artist) FROM media_file WHERE missing=0").fetchone()[0]
            albums  = cur.execute("SELECT COUNT(DISTINCT album) FROM media_file WHERE missing=0").fetchone()[0]
            lists   = cur.execute("SELECT COUNT(*) FROM playlist").fetchone()[0]
        finally:
            conn.close()

        embed = discord.Embed(
            title="📊  Library Stats",
            color=0x5865F2
        )
        embed.add_field(name="🎵 Songs",     value=f"**{songs:,}**",    inline=True)
        embed.add_field(name="👤 Artists",   value=f"**{artists:,}**",  inline=True)
        embed.add_field(name="💿 Albums",    value=f"**{albums:,}**",   inline=True)
        embed.add_field(name="📑 Playlists", value=f"**{lists}**",      inline=True)
        embed.set_footer(text=f"PSudofy  •  Oracle Cloud Server  •  {datetime.datetime.now().strftime('%d %b %Y')}")
        await ctx.reply(embed=embed)
    except Exception as e:
        await ctx.reply(embed=discord.Embed(
            title="❌  Stats Error",
            description=f"```{e}```",
            color=0xED4245
        ))


@bot.command(name="tunnel", aliases=["url", "link", "cf", "cloudflare"])
async def tunnel_cmd(ctx):
    """Retrieve the current Cloudflare tunnel URL for Navidrome."""
    try:
        cmd = "sudo journalctl -u cloudflared-navidrome --no-pager | grep -o 'https://[a-zA-Z0-9-]*.trycloudflare.com' | tail -1"
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
        url = res.stdout.strip()

        # If not found or empty, check service status and try restarting
        if not url or not url.startswith("https://"):
            status_res = subprocess.run(["sudo", "systemctl", "is-active", "cloudflared-navidrome"], capture_output=True, text=True)
            status = status_res.stdout.strip()
            if status != "active":
                embed = discord.Embed(
                    title="⚠️  Cloudflare Tunnel Offline",
                    description=f"Service status: `{status}`\nRestarting tunnel service...",
                    color=0xFEE75C
                )
                msg = await ctx.reply(embed=embed)
                subprocess.run(["sudo", "systemctl", "restart", "cloudflared-navidrome"], timeout=15)
                import asyncio
                await asyncio.sleep(6)
                res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
                url = res.stdout.strip()
                if url and url.startswith("https://"):
                    embed = discord.Embed(
                        title="🌐  Cloudflare Tunnel URL",
                        description=f"**Tunnel URL (Bypasses Campus Wi-Fi & Firewalls):**\n`{url}`\n\n🔗 **[Open Navidrome Web UI]({url}/app/)**",
                        color=0x57F287
                    )
                    embed.add_field(
                        name="📱 Symfonium Setup",
                        value=f"1. Open Symfonium ➔ **Settings (⚙️)** ➔ **Media providers**\n2. Tap your server ➔ **Edit**\n3. Set Server URL: `{url}`\n4. Save and Sync!",
                        inline=False
                    )
                    embed.set_footer(text="PSudofy  •  Cloudflare Tunnel  •  Works on any network")
                    await msg.edit(embed=embed)
                    return
                else:
                    embed.description = "❌ Tunnel could not be started or URL not found in logs."
                    embed.color = 0xED4245
                    await msg.edit(embed=embed)
                    return

        if url and url.startswith("https://"):
            embed = discord.Embed(
                title="🌐  Cloudflare Tunnel URL",
                description=f"**Tunnel URL (Bypasses Campus Wi-Fi & Firewalls):**\n`{url}`\n\n🔗 **[Open Navidrome Web UI]({url}/app/)**",
                color=0x57F287
            )
            embed.add_field(
                name="📱 Symfonium Setup",
                value=f"1. Open Symfonium ➔ **Settings (⚙️)** ➔ **Media providers**\n2. Tap your server ➔ **Edit**\n3. Set Server URL: `{url}`\n4. Save and Sync!",
                inline=False
            )
            embed.set_footer(text="PSudofy  •  Cloudflare Tunnel  •  Works on any network")
            await ctx.reply(embed=embed)
        else:
            await ctx.reply("❌ Could not extract Cloudflare Tunnel URL. Check if `cloudflared-navidrome` service is running.")
    except Exception as e:
        await ctx.reply(f"❌ Error getting tunnel URL: `{e}`")


@bot.command(name="log", aliases=["logs", "history", "recent"])
async def log_cmd(ctx, count: int = 10):
    count = min(max(count, 1), 25)
    entries = read_dl_log(count)

    if not entries:
        await ctx.reply(embed=discord.Embed(
            title="📋  Download History",
            description="Nothing downloaded yet — use `!dl <song>` to get started!",
            color=0x5865F2
        ))
        return

    lines = []
    for e in reversed(entries):
        status = e.get("status", "?")
        lyrics = e.get("lyrics", "—")
        ts     = e.get("ts", "")
        title  = e.get("title", "Unknown")[:38]
        artist = e.get("artist", "?")[:22]

        ok = status == "ok"
        dl_icon  = "✅" if ok else "❌"
        lyr_icon = "🎤" if "Synced" in lyrics else ("📄" if "✅" in lyrics else "⚠️")

        line = f"{dl_icon} **{title}** — {artist}\n{lyr_icon} `{lyrics[:40]}` · *{ts}*"
        if not ok:
            line += f"\n> _{status[:80]}_"
        lines.append(line)

    embed = discord.Embed(
        title=f"📋  Last {len(entries)} Downloads",
        description="\n\n".join(lines)[:4000],
        color=0x5865F2
    )
    embed.set_footer(text="✅ ok  ❌ failed  🎤 synced lyrics  📄 plain lyrics  ⚠️ no lyrics")
    await ctx.reply(embed=embed)


@bot.command(name="cancel", aliases=["stop", "abort", "kill"])
async def cancel_cmd(ctx):
    global ACTIVE_TASKS
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, kill_all_download_processes)

    cancelled = 0
    for task in list(ACTIVE_TASKS):
        if not task.done():
            task.cancel()
            cancelled += 1
    ACTIVE_TASKS.clear()

    embed = discord.Embed(
        title="🛑  All Downloads Stopped",
        description=(
            f"Killed **{cancelled}** active task(s) and terminated all `yt-dlp`, `ffmpeg`, and `spotdl` processes.\n\n"
            "Any tracks that finished downloading before this are still in your library."
        ),
        color=0xED4245
    )
    embed.set_footer(text="PSudofy  •  Server is clean — ready for new downloads")
    await ctx.reply(embed=embed)


@bot.command(name="failed", aliases=["errors", "faillist", "failedsongs"])
async def failed_cmd(ctx):
    items = get_failed_downloads()
    if not items:
        await ctx.reply(embed=discord.Embed(
            title="🎉  Clean Record!",
            description="No failed downloads — everything went through successfully.",
            color=0x1DB954
        ).set_footer(text="PSudofy"))
        return

    lines = []
    for idx, item in enumerate(reversed(items[-15:]), 1):
        q  = item.get("query", "Unknown")[:50]
        r  = item.get("reason", "Unknown")[:60]
        ts = item.get("timestamp", "")
        lines.append(f"`{idx:2}.` **{q}**\n> _{r}_ · *{ts}*")

    embed = discord.Embed(
        title=f"❌  Failed Downloads — {len(items)} total",
        description="\n\n".join(lines)[:4000],
        color=0xED4245
    )
    embed.add_field(
        name="💡 Auto-retry",
        value="Run **`!retryfailed`** to attempt downloading all failed tracks automatically.",
        inline=False
    )
    embed.set_footer(text=f"PSudofy  •  Showing newest {min(len(items), 15)} of {len(items)}")
    await ctx.reply(embed=embed)


@bot.command(name="retryfailed", aliases=["retrydl", "redownloadfailed", "fixfailed"])
async def retryfailed_cmd(ctx):
    """Automatically retry downloading all songs recorded in failed_downloads.json with live progress."""
    items = get_failed_downloads()
    if not items:
        await ctx.reply(embed=discord.Embed(
            title="🎉  All Good!",
            description="No failed downloads found — nothing to retry.",
            color=0x1DB954
        ).set_footer(text="PSudofy"))
        return

    total = len(items)
    msg = await ctx.reply(embed=discord.Embed(
        title="📥  Retrying Downloads…",
        description=(
            f"`░░░░░░░░░░` **0%**\n"
            f"**0** of **{total}** tracks processed\n"
            f"▶  *Starting download retry…*"
        ),
        color=0x5865F2
    ).set_footer(text="PSudofy  •  Downloading on server"))

    progress_queue: asyncio.Queue = asyncio.Queue()

    def progress_cb(done, total_cnt, current_title):
        asyncio.run_coroutine_threadsafe(
            progress_queue.put((done, total_cnt, current_title)),
            bot.loop
        )

    loop = asyncio.get_running_loop()

    def do_retry():
        fixed = 0
        failed_again = 0
        for idx, item in enumerate(items, 1):
            query = item.get("query", "").strip()
            if not query:
                continue
            progress_cb(idx, total, query)
            res = run_single_download(query)
            if res.get("success"):
                fixed += 1
            else:
                failed_again += 1
        if fixed > 0:
            trigger_navidrome_scan()
            navidrome_wait_scan(timeout=60)
        return fixed, failed_again

    retry_task = loop.run_in_executor(None, do_retry)
    ACTIVE_TASKS.add(retry_task)

    last_edit = asyncio.get_event_loop().time()
    try:
        while not retry_task.done():
            try:
                done, total_cnt, title = await asyncio.wait_for(progress_queue.get(), timeout=2.5)
                now = asyncio.get_event_loop().time()
                if now - last_edit >= 2.0:
                    pct = int((done / total_cnt) * 100) if total_cnt else 0
                    filled = round(pct / 10)
                    bar = "█" * filled + "░" * (10 - filled)
                    embed = discord.Embed(
                        title="📥  Retrying Downloads…",
                        description=(
                            f"`{bar}`  **{pct}%**\n"
                            f"**{done}** of **{total_cnt}** tracks processed\n"
                            f"▶  *{title[:60]}*"
                        ),
                        color=0x5865F2
                    )
                    embed.set_footer(text="PSudofy  •  Use !cancel to stop  •  Downloading on server")
                    await msg.edit(embed=embed)
                    last_edit = now
            except asyncio.TimeoutError:
                pass

        fixed, failed_again = await retry_task
    except asyncio.CancelledError:
        ACTIVE_TASKS.discard(retry_task)
        await msg.edit(embed=discord.Embed(
            title="🛑  Retry Cancelled",
            description="Cancelled retrying failed downloads.",
            color=0xED4245
        ).set_footer(text="PSudofy"))
        return

    ACTIVE_TASKS.discard(retry_task)
    remaining = len(get_failed_downloads())

    if fixed > 0:
        color = 0x1DB954
        desc = f"Successfully downloaded **{fixed}** of **{total}** track(s)!"
    else:
        color = 0xED4245
        desc = "Could not download any of the failed tracks."

    if remaining:
        desc += f"\n\n**{remaining}** track(s) still failing."

    await msg.edit(embed=discord.Embed(
        title="📥  Retry Downloads Complete",
        description=desc,
        color=color
    ).set_footer(text="PSudofy  •  Pull to refresh in Symfonium"))


@bot.command(name="nolyrics", aliases=["missinglyrics", "nolrc", "lyricsneeded"])
async def nolyrics_cmd(ctx):
    items = get_missing_lyrics()
    if not items:
        await ctx.reply(embed=discord.Embed(
            title="🎤  All Tracks Have Lyrics!",
            description="Every downloaded track has lyrics embedded — nothing is missing.",
            color=0x1DB954
        ).set_footer(text="PSudofy"))
        return

    lines = []
    for idx, item in enumerate(reversed(items[-15:]), 1):
        t  = item.get("title",  "Unknown")[:38]
        a  = item.get("artist", "?")[:22]
        ts = item.get("timestamp", "")
        r  = item.get("reason",  "")
        lines.append(f"`{idx:2}.` **{t}** — {a}\n> _{r}_ · *{ts}*")

    embed = discord.Embed(
        title=f"📝  Missing Lyrics — {len(items)} tracks",
        description="\n\n".join(lines)[:4000],
        color=0xFEE75C
    )
    embed.add_field(
        name="💡 Auto-fix",
        value="Run `!retrylyrics` to attempt fetching lyrics for all of these from LRCLib.",
        inline=False
    )
    embed.set_footer(text=f"PSudofy  •  Showing newest {min(len(items), 15)} of {len(items)}")
    await ctx.reply(embed=embed)


@bot.command(name="retrylyrics", aliases=["fixlyrics", "fetchlyrics"])
async def retrylyrics_cmd(ctx):
    items = get_missing_lyrics()
    if not items:
        await ctx.reply(embed=discord.Embed(
            title="🎤  All Good!",
            description="No tracks are missing lyrics — nothing to retry.",
            color=0x1DB954
        ))
        return

    status_msg = await ctx.reply(embed=discord.Embed(
        title="🔄  Retrying Lyrics…",
        description=f"Searching LRCLib for **{len(items)}** tracks…",
        color=0x5865F2
    ).set_footer(text="PSudofy  •  This may take a minute"))

    fixed = 0
    loop  = asyncio.get_running_loop()

    def do_retry():
        nonlocal fixed
        for item in list(items):
            title  = item.get("title",  "")
            artist = item.get("artist", "")
            path   = item.get("path",   "")
            if not path or not os.path.exists(path):
                continue
            plain, synced = fetch_lyrics(title, artist)
            if plain or synced:
                st = embed_lyrics_to_file(path, plain, synced, title=title, artist=artist)
                if "✅" in st:
                    fixed += 1

    await loop.run_in_executor(None, do_retry)
    remaining = len(get_missing_lyrics())

    if fixed > 0:
        color = 0x1DB954
        desc  = f"Added lyrics to **{fixed}** track(s)!"
    else:
        color = 0xFEE75C
        desc  = "No new lyrics found on LRCLib this time."

    if remaining:
        desc += f"\n\n**{remaining}** track(s) still have no lyrics — they may not be on LRCLib."

    await status_msg.edit(content=None, embed=discord.Embed(
        title="🎤  Lyrics Retry Complete",
        description=desc,
        color=color
    ).set_footer(text="PSudofy"))


# ─────────────────────────── Recommendations Engine ───────────────────────────

LAST_RECOMMENDATIONS = []

SIMILAR_ARTISTS_MAP = {
    "syml":           ["The Paper Kites", "Novo Amor", "Lord Huron", "Vancouver Sleep Clinic", "Sleeping At Last"],
    "arijit singh":   ["Mohit Chauhan", "Jasleen Royal", "Atif Aslam", "Pritam", "B Praak"],
    "pritam":         ["Arijit Singh", "Mohit Chauhan", "Amit Trivedi", "Sachin-Jigar", "Vishal-Shekhar"],
    "vishal-shekhar": ["Pritam", "Shankar-Ehsaan-Loy", "Salim-Sulaiman", "Amit Trivedi"],
    "aditya a":       ["Anuv Jain", "Zaeden", "The Local Train", "Twin Strings", "Prateek Kuhad"],
    "naalayak":       ["The Local Train", "When Chai Met Toast", "Anuv Jain", "Parvaaz"],
    "sonu nigam":     ["Kumar Sanu", "Udit Narayan", "Shaan", "Lucky Ali", "KK"],
    "tame impala":    ["Pond", "Unknown Mortal Orchestra", "MGMT", "Mac DeMarco", "Foster the People"],
    "aurora":         ["Florence + the Machine", "Lorde", "Billie Eilish", "Marina", "First Aid Kit"],
}


def _get_library_info() -> tuple[list[str], set[str]]:
    """Get top artists and normalized set of existing track titles from Navidrome."""
    artists = []
    existing_titles = set()
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        rows = cur.execute("""
            SELECT artist, COUNT(*) as cnt FROM media_file
            WHERE missing = 0 AND artist IS NOT NULL AND artist != ''
            GROUP BY artist ORDER BY cnt DESC LIMIT 15
        """).fetchall()
        artists = [r[0] for r in rows]

        titles = cur.execute("SELECT title FROM media_file WHERE missing = 0").fetchall()
        for t in titles:
            if t[0]:
                existing_titles.add(re.sub(r"[^a-z0-9]", "", t[0].lower()))
        conn.close()
    except Exception as e:
        print(f"[Navidrome DB recs query error] {e}", flush=True)
    return artists, existing_titles


def _fetch_listenbrainz_artists(username: str, limit: int = 40) -> list[str]:
    """Fetch user's recently scrobbled artists from ListenBrainz."""
    try:
        url = f"https://api.listenbrainz.org/1/user/{urllib.parse.quote(username)}/listens?count={limit}"
        req = urllib.request.Request(url, headers={"User-Agent": "PSudofy-Discord/1.0"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                listens = data.get("payload", {}).get("listens", [])
                artists = []
                for l in listens:
                    tm = l.get("track_metadata", {})
                    a = tm.get("artist_name", "").strip()
                    if a and a not in artists:
                        artists.append(a)
                return artists
    except Exception as e:
        print(f"[ListenBrainz API error] {e}", flush=True)
    return []


def _discover_recommendations(username: str | None = None, max_recs: int = 8) -> list[dict]:
    """Discover new songs not in library based on ListenBrainz / Navidrome taste profile."""
    import yt_dlp
    global LAST_RECOMMENDATIONS

    lb_user = username or os.getenv("LISTENBRAINZ_USER", "adarsh4our")
    target_artists = []

    if lb_user:
        target_artists = _fetch_listenbrainz_artists(lb_user)

    db_artists, existing_titles = _get_library_info()

    for da in db_artists:
        if da not in target_artists:
            target_artists.append(da)

    expanded_artists = []
    for a in target_artists:
        if a not in expanded_artists:
            expanded_artists.append(a)
        k = a.lower()
        if k in SIMILAR_ARTISTS_MAP:
            for sa in SIMILAR_ARTISTS_MAP[k]:
                if sa not in expanded_artists:
                    expanded_artists.append(sa)

    if not expanded_artists:
        expanded_artists = ["Tame Impala", "Arijit Singh", "AURORA", "Anuv Jain"]

    ydl_opts = {
        "quiet": True,
        "extract_flat": True,
        "skip_download": True,
        "no_warnings": True,
        "default_search": "ytsearch5:",
    }
    proxy = _get_active_proxy()
    if proxy:
        ydl_opts["proxy"] = proxy

    discovered = []
    seen_titles = set()

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        for artist in expanded_artists:
            if len(discovered) >= max_recs:
                break
            query = f"ytsearch5:{artist} song official audio"
            try:
                info = ydl.extract_info(query, download=False)
                entries = info.get("entries", []) if info else []
                for e in entries:
                    if not e or len(discovered) >= max_recs:
                        continue
                    raw_title = e.get("title", "")
                    clean_norm = re.sub(r"[^a-z0-9]", "", raw_title.lower())
                    if not clean_norm or clean_norm in seen_titles:
                        continue

                    # Check if already in library
                    in_lib = False
                    for ext in existing_titles:
                        if len(ext) > 4 and (ext in clean_norm or clean_norm in ext):
                            in_lib = True
                            break
                    if in_lib:
                        continue

                    duration = e.get("duration") or 0
                    if duration > 600 or any(kw in raw_title.lower() for kw in ["jukebox", "full album", "nonstop", "compilation", "greatest hits"]):
                        continue

                    seen_titles.add(clean_norm)
                    vid_id = e.get("id") or e.get("video_id") or ""
                    discovered.append({
                        "title": raw_title,
                        "artist": artist,
                        "url": f"https://www.youtube.com/watch?v={vid_id}" if vid_id else "",
                    })
                    break
            except Exception:
                pass

    LAST_RECOMMENDATIONS = discovered
    return discovered


@bot.command(name="recs", aliases=["recommend", "discover", "recommendations"])
async def recs_cmd(ctx, *, args: str | None = None):
    """Show new recommended songs based on ListenBrainz/Navidrome listening history.
    Usage: !recs [count] [username or artist]
    Examples: !recs | !recs 15 | !recs 20 Arijit Singh
    """
    max_recs = 8
    user_or_artist = None

    if args:
        parts = args.strip().split(None, 1)
        if parts[0].isdigit():
            max_recs = min(int(parts[0]), 25)  # cap at 25 for embed readability
            user_or_artist = parts[1] if len(parts) > 1 else None
        else:
            user_or_artist = args

    status_msg = await ctx.reply(embed=discord.Embed(
        title="✨  Discovering Music Recommendations…",
        description=f"Analyzing your ListenBrainz & Navidrome taste profile to find **{max_recs}** new songs not yet in your library…",
        color=0x5865F2
    ).set_footer(text="PSudofy • Music Discovery Engine"))

    loop = asyncio.get_running_loop()
    recs = await loop.run_in_executor(None, _discover_recommendations, user_or_artist, max_recs)

    if not recs:
        await status_msg.edit(embed=discord.Embed(
            title="ℹ️  No New Recommendations Found",
            description="All top songs from your favorite artists are already in your library! Try scrobbling more music on ListenBrainz or check back later.",
            color=0xFEE75C
        ).set_footer(text="PSudofy"))
        return

    embed = discord.Embed(
        title="✨  Recommended Songs for You",
        description="Here are new tracks tailored to your taste that are **not in your library**:\n",
        color=0x1DB954
    )

    lines = []
    for i, r in enumerate(recs, 1):
        link = f"([Listen]({r['url']}))" if r.get("url") else ""
        lines.append(f"`{i}.` **{r['title'][:40]}** — *{r['artist'][:25]}* {link}")

    embed.add_field(name="🎶 New Discoveries", value="\n".join(lines), inline=False)
    embed.add_field(
        name="📥 How to Download",
        value="• Type **`!dlrecs`** to automatically download all these songs with synced lyrics!\n• Or type **`!dl <song name>`** to download any specific one.",
        inline=False
    )
    embed.set_footer(text=f"PSudofy • {len(recs)} new recommendations ready • Powered by ListenBrainz & MusicBrainz")
    await status_msg.edit(embed=embed)


@bot.command(name="dlrecs", aliases=["downloadrecs", "addrecs"])
async def dlrecs_cmd(ctx):
    """Download all songs from the latest recommendations with live progress."""
    global LAST_RECOMMENDATIONS
    if not LAST_RECOMMENDATIONS:
        await ctx.reply(embed=discord.Embed(
            title="⚠️  No Active Recommendations",
            description="Please run **`!recs`** first to discover new songs, then run `!dlrecs` to download them.",
            color=0xFEE75C
        ).set_footer(text="PSudofy"))
        return

    recs_to_dl = list(LAST_RECOMMENDATIONS)
    total = len(recs_to_dl)

    msg = await ctx.reply(embed=discord.Embed(
        title="📥  Downloading Recommended Tracks…",
        description=(
            f"`░░░░░░░░░░` **0%**\n"
            f"**0** of **{total}** tracks processed\n"
            f"▶  *Starting downloads…*"
        ),
        color=0x5865F2
    ).set_footer(text="PSudofy • Downloading to server"))

    progress_queue: asyncio.Queue = asyncio.Queue()

    def progress_cb(done, total_cnt, current_title):
        asyncio.run_coroutine_threadsafe(
            progress_queue.put((done, total_cnt, current_title)),
            bot.loop
        )

    loop = asyncio.get_running_loop()

    def do_download():
        success_tracks = []
        failed_count = 0
        for idx, r in enumerate(recs_to_dl, 1):
            progress_cb(idx, total, r.get("title", "Song"))
            target = r.get("url") or f"{r['title']} {r['artist']}"
            res = run_single_download(target)
            if res.get("success"):
                success_tracks.append(res)
            else:
                failed_count += 1
        trigger_navidrome_scan()
        navidrome_wait_scan(timeout=60)
        return success_tracks, failed_count

    dl_task = loop.run_in_executor(None, do_download)
    ACTIVE_TASKS.add(dl_task)

    last_edit = asyncio.get_event_loop().time()
    try:
        while not dl_task.done():
            try:
                done, total_cnt, title = await asyncio.wait_for(progress_queue.get(), timeout=2.5)
                now = asyncio.get_event_loop().time()
                if now - last_edit >= 2.0:
                    pct = int((done / total_cnt) * 100) if total_cnt else 0
                    filled = round(pct / 10)
                    bar = "█" * filled + "░" * (10 - filled)
                    embed = discord.Embed(
                        title="📥  Downloading Recommended Tracks…",
                        description=(
                            f"`{bar}`  **{pct}%**\n"
                            f"**{done}** of **{total_cnt}** tracks processed\n"
                            f"▶  *{title[:60]}*"
                        ),
                        color=0x5865F2
                    )
                    embed.set_footer(text="PSudofy  •  Use !cancel to stop  •  Downloading on server")
                    await msg.edit(embed=embed)
                    last_edit = now
            except asyncio.TimeoutError:
                pass

        successes, failed_n = await dl_task
    except asyncio.CancelledError:
        ACTIVE_TASKS.discard(dl_task)
        await msg.edit(embed=discord.Embed(
            title="🛑  Download Cancelled",
            description="Cancelled downloading recommendations.",
            color=0xED4245
        ).set_footer(text="PSudofy"))
        return

    ACTIVE_TASKS.discard(dl_task)

    embed = discord.Embed(
        title=f"✅  Downloaded {len(successes)} of {total} Recommended Songs",
        color=0x1DB954 if len(successes) > 0 else 0xED4245
    )
    if successes:
        preview = [
            f"`{i+1}.` **{t['title'][:40]}** — *{t.get('artist','')[:25]}* " + ("🎤" if "Synced" in t.get("lyrics","") else "📄")
            for i, t in enumerate(successes[:6])
        ]
        if len(successes) > 6:
            preview.append(f"*…and {len(successes) - 6} more*")
        embed.add_field(name="🎶 Added to Navidrome", value="\n".join(preview), inline=False)
    if failed_n > 0:
        embed.add_field(name="⚠️ Not Downloaded", value=f"{failed_n} tracks", inline=True)

    embed.set_footer(text="PSudofy • Pull to refresh in Symfonium to listen!")
    await msg.edit(embed=embed)


# ────────────────────────── New Feature Commands ──────────────────────────────

@bot.command(name="search", aliases=["find", "lookup", "lib"])
async def search_cmd(ctx, *, query: str = None):
    """Search your Navidrome library for a song before downloading.
    Usage: !search <song name or artist>
    """
    if not query:
        await ctx.reply(embed=discord.Embed(
            title="🔍  Search Your Library",
            description=(
                "**Usage:** `!search <song name or artist>`\n\n"
                "**Examples:**\n"
                "`!search Tum Hi Ho`\n"
                "`!search Arijit Singh`\n"
                "`!search Blinding Lights Weeknd`\n\n"
                "Searches your Navidrome library by title, artist, and album."
            ),
            color=0x38bdf8
        ))
        return

    try:
        clean_q = re.sub(r"[^a-z0-9\s]", "", query.lower()).strip()
        conn = sqlite3.connect(DB_PATH)
        try:
            cur = conn.cursor()
            rows = cur.execute("""
                SELECT title, artist, album FROM media_file
                WHERE missing = 0 AND (
                    lower(title)  LIKE lower(?) OR
                    lower(artist) LIKE lower(?) OR
                    lower(album)  LIKE lower(?)
                )
                ORDER BY lower(title)
                LIMIT 8
            """, (f"%{query}%", f"%{query}%", f"%{query}%")).fetchall()
        finally:
            conn.close()
    except Exception as e:
        await ctx.reply(embed=discord.Embed(
            title="❌  Search Error",
            description=f"```{e}```",
            color=0xED4245
        ))
        return

    if not rows:
        await ctx.reply(embed=discord.Embed(
            title="🔍  Not Found in Library",
            description=(
                f"No matches for **`{query[:60]}`** in your library.\n\n"
                "💡 Use `!dl` to download it:\n"
                f"`!dl {query[:60]}`"
            ),
            color=0xFEE75C
        ).set_footer(text="PSudofy"))
        return

    lines = []
    for i, (title, artist, album) in enumerate(rows, 1):
        album_str = f" • *{album[:25]}*" if album else ""
        lines.append(f"`{i}.` **{title[:40]}** — {artist[:25]}{album_str}")

    embed = discord.Embed(
        title=f"🔍  Found {len(rows)} match{'es' if len(rows) != 1 else ''} in Library",
        description="\n".join(lines),
        color=0x1DB954
    )
    embed.set_footer(text=f"PSudofy • Search: \"{query[:40]}\" • Pull to refresh in Symfonium")
    await ctx.reply(embed=embed)


@bot.command(name="health", aliases=["status", "overview", "summary"])
async def health_cmd(ctx):
    """Show a full library health report: stats, storage, issues, uptime."""
    msg = await ctx.reply(embed=discord.Embed(
        title="📊  Checking Library Health…",
        description="Scanning library, logs, and storage…",
        color=0x5865F2
    ))

    try:
        # ── Library counts from Navidrome DB ──
        songs = artists = albums = playlists = dupes = 0
        try:
            conn = sqlite3.connect(DB_PATH)
            try:
                cur = conn.cursor()
                songs     = cur.execute("SELECT COUNT(*) FROM media_file WHERE missing=0").fetchone()[0]
                artists   = cur.execute("SELECT COUNT(DISTINCT artist) FROM media_file WHERE missing=0").fetchone()[0]
                albums    = cur.execute("SELECT COUNT(DISTINCT album) FROM media_file WHERE missing=0").fetchone()[0]
                playlists = cur.execute("SELECT COUNT(*) FROM playlist").fetchone()[0]
                dupes     = cur.execute("""
                    SELECT COUNT(*) FROM (
                        SELECT lower(title), lower(artist) FROM media_file
                        WHERE missing=0 AND title IS NOT NULL AND title != ''
                        GROUP BY lower(title), lower(artist) HAVING COUNT(*) > 1
                    )
                """).fetchone()[0]
            finally:
                conn.close()
        except Exception:
            pass

        # ── Diagnostics from JSON logs ──
        failed_count  = len(get_failed_downloads())
        nolyrics_count = len(get_missing_lyrics())

        # ── Disk usage ──
        disk_used_gb = disk_total_gb = disk_pct = 0
        try:
            usage = shutil.disk_usage(MUSIC_FOLDER)
            disk_total_gb = usage.total / (1024 ** 3)
            disk_used_gb  = (usage.total - usage.free) / (1024 ** 3)
            disk_pct      = int(disk_used_gb / disk_total_gb * 100) if disk_total_gb else 0
        except Exception:
            pass

        filled = round(disk_pct / 10)
        disk_bar = "█" * filled + "░" * (10 - filled)

        # ── Bot uptime ──
        uptime_str = "—"
        if bot_start_time:
            delta = datetime.datetime.now(datetime.timezone.utc) - bot_start_time
            d, rem = divmod(int(delta.total_seconds()), 86400)
            h, rem = divmod(rem, 3600)
            m = rem // 60
            uptime_str = f"{d}d {h}h {m}m" if d else (f"{h}h {m}m" if h else f"{m}m")

        # ── Build embed ──
        embed = discord.Embed(
            title="📊  Library Health Report",
            color=0x5865F2
        )

        # Library stats row
        embed.add_field(name="🎵 Songs",     value=f"**{songs:,}**",    inline=True)
        embed.add_field(name="👤 Artists",   value=f"**{artists:,}**",  inline=True)
        embed.add_field(name="💿 Albums",    value=f"**{albums:,}**",   inline=True)
        embed.add_field(name="📑 Playlists", value=f"**{playlists}**",  inline=True)
        embed.add_field(name="🤖 Bot Uptime",value=f"**{uptime_str}**", inline=True)
        embed.add_field(name="\u200b",       value="\u200b",             inline=True)

        # Storage bar
        embed.add_field(
            name="💾  Disk Usage",
            value=f"`{disk_bar}` **{disk_pct}%**  ({disk_used_gb:.1f} GB / {disk_total_gb:.0f} GB)",
            inline=False
        )

        # Issues
        issues = []
        if failed_count:
            issues.append(f"❌ **{failed_count}** failed download(s) — run `!retryfailed`")
        if nolyrics_count:
            issues.append(f"📝 **{nolyrics_count}** track(s) missing lyrics — run `!retrylyrics`")
        if dupes:
            issues.append(f"🔁 **{dupes}** duplicate group(s) found — run `!dupes` to review")

        if issues:
            embed.add_field(name="⚠️  Issues Detected", value="\n".join(issues), inline=False)
        else:
            embed.add_field(name="✅  All Good!", value="No failed downloads, no missing lyrics, no duplicates.", inline=False)

        embed.set_footer(text=f"PSudofy • {datetime.datetime.now().strftime('%d %b %Y %H:%M')}")
        await msg.edit(embed=embed)

    except Exception as e:
        await msg.edit(embed=discord.Embed(
            title="❌  Health Check Error",
            description=f"```{e}```",
            color=0xED4245
        ))


@bot.command(name="dupes", aliases=["duplicates", "dups"])
async def dupes_cmd(ctx):
    """Find duplicate songs in your Navidrome library."""
    try:
        conn = sqlite3.connect(DB_PATH)
        try:
            cur = conn.cursor()
            rows = cur.execute("""
                SELECT title, artist, COUNT(*) as cnt
                FROM media_file
                WHERE missing = 0 AND title IS NOT NULL AND title != ''
                GROUP BY lower(title), lower(artist)
                HAVING COUNT(*) > 1
                ORDER BY cnt DESC, lower(title)
                LIMIT 20
            """).fetchall()
            total_groups = cur.execute("""
                SELECT COUNT(*) FROM (
                    SELECT lower(title), lower(artist) FROM media_file
                    WHERE missing = 0 AND title IS NOT NULL AND title != ''
                    GROUP BY lower(title), lower(artist) HAVING COUNT(*) > 1
                )
            """).fetchone()[0]
        finally:
            conn.close()
    except Exception as e:
        await ctx.reply(embed=discord.Embed(
            title="❌  Duplicates Error",
            description=f"```{e}```",
            color=0xED4245
        ))
        return

    if not rows:
        await ctx.reply(embed=discord.Embed(
            title="✅  No Duplicates Found!",
            description="Your library is clean — every song appears exactly once.",
            color=0x1DB954
        ).set_footer(text="PSudofy"))
        return

    lines = []
    for i, (title, artist, cnt) in enumerate(rows, 1):
        lines.append(f"`{i:2}.` **{title[:40]}** — {artist[:25]}  ×{cnt}")

    embed = discord.Embed(
        title=f"🔁  Duplicate Songs — {total_groups} group{'s' if total_groups != 1 else ''}",
        description="\n".join(lines),
        color=0xFEE75C
    )
    if total_groups > 20:
        embed.description += f"\n\n*…showing top 20 of {total_groups} duplicate groups*"
    embed.add_field(
        name="ℹ️ What to do",
        value=(
            "These songs have multiple entries in the DB.\n"
            "You can use `!dl` to re-download the clean version, "
            "or SSH into the server and run `python3 dedup_manager.py` to clean up."
        ),
        inline=False
    )
    embed.set_footer(text=f"PSudofy • {datetime.datetime.now().strftime('%d %b %Y')}")
    await ctx.reply(embed=embed)


@bot.command(name="smartplaylist", aliases=["smartpl", "genplaylists", "playlists"])
async def smartplaylist_cmd(ctx):
    """Generate smart themed playlists from your library using Last.fm tags + ListenBrainz history."""
    lastfm_key = os.getenv("LASTFM_API_KEY", "")
    if not lastfm_key:
        await ctx.reply(embed=discord.Embed(
            title="⚠️  Last.fm API Key Missing",
            description=(
                "Smart playlists use Last.fm crowd-tags to categorize your songs into moods and genres.\n\n"
                "Add your key to `.env`:\n"
                "```\nLASTFM_API_KEY=your_key_here\n```\n"
                "Get a free key at [last.fm/api](https://www.last.fm/api/account/create)."
            ),
            color=0xFEE75C
        ))
        return

    msg = await ctx.reply(embed=discord.Embed(
        title="🎵  Generating Smart Playlists…",
        description=(
            "Fetching Last.fm tags for your library songs...\n"
            "This can take a few minutes for large libraries."
        ),
        color=0x5865F2
    ).set_footer(text="PSudofy • Smart Playlist Generator"))

    loop = asyncio.get_running_loop()

    def run_smart_playlists():
        """Run smart_playlists.py --run as subprocess and capture output."""
        script_path = os.path.expanduser("~/PSudofy/smart_playlists.py")
        if not os.path.exists(script_path):
            return False, "smart_playlists.py not found on server"
        env = os.environ.copy()
        result = subprocess.run(
            ["python3", script_path, "--run"],
            capture_output=True, text=True, timeout=300,
            cwd=os.path.expanduser("~/PSudofy"), env=env
        )
        combined = (result.stdout or "") + (result.stderr or "")
        return result.returncode == 0, combined[:800]

    try:
        ok, output = await loop.run_in_executor(None, run_smart_playlists)
    except Exception as e:
        ok, output = False, str(e)

    if ok:
        # Count playlists created from output if possible
        created = len(re.findall(r"(?i)(created|updated|playlist)", output))
        embed = discord.Embed(
            title="✅  Smart Playlists Generated!",
            description=(
                "Your library has been analysed and playlists have been written to Navidrome.\n\n"
                "**Themes created:** Sad & Emotional, Romantic Vibes, Party & Upbeat, Chill & Relax,\n"
                "Late Night Feels, Workout Energy, Bollywood & Hindi, Indie & Alt, Nostalgia,\n"
                "Trending Picks, Top Rated, Daily Mix\n\n"
                "📲 Pull to refresh in Symfonium to see your new playlists!"
            ),
            color=0x1DB954
        )
    else:
        embed = discord.Embed(
            title="❌  Smart Playlist Generation Failed",
            description=f"```\n{output[:600]}\n```",
            color=0xED4245
        )
    embed.set_footer(text=f"PSudofy • {datetime.datetime.now().strftime('%H:%M')}")
    await msg.edit(embed=embed)


# ────────────────────────── Weekly Auto-Download ───────────────────────────────

# Fires every Sunday at 15:30 UTC = 21:00 IST (9 PM India Standard Time)
_IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
_WEEKLY_TIME = datetime.time(hour=21, minute=0, tzinfo=_IST)


@tasks.loop(time=_WEEKLY_TIME)
async def weekly_autodl():
    """Every Sunday at 9 PM IST: auto-discover and download new recommendations."""
    # Only run on Sundays (weekday() == 6)
    if datetime.datetime.now(_IST).weekday() != 6:
        return

    if not WEEKLY_CHANNEL_ID:
        return

    channel = bot.get_channel(WEEKLY_CHANNEL_ID)
    if not channel:
        print(f"[Weekly AutoDL] Channel {WEEKLY_CHANNEL_ID} not found — skipping", flush=True)
        return

    announce = await channel.send(embed=discord.Embed(
        title="🎵  Weekly Music Discovery",
        description=(
            "Good evening! 🌙 It's Sunday — time for your weekly new music drop.\n"
            "Discovering fresh recommendations from your ListenBrainz taste profile…"
        ),
        color=0x5865F2
    ).set_footer(text="PSudofy • Weekly Auto-Download"))

    loop = asyncio.get_running_loop()
    recs = await loop.run_in_executor(None, _discover_recommendations, None, 8)

    if not recs:
        await announce.edit(embed=discord.Embed(
            title="ℹ️  Weekly Discovery — Nothing New",
            description="All top songs from your artists are already in your library. Check back next week!",
            color=0xFEE75C
        ).set_footer(text="PSudofy • Weekly Auto-Download"))
        return

    lines = []
    for i, r in enumerate(recs, 1):
        link = f"([Listen]({r['url']}))" if r.get("url") else ""
        lines.append(f"`{i}.` **{r['title'][:40]}** — *{r['artist'][:25]}* {link}")

    preview_embed = discord.Embed(
        title=f"✨  {len(recs)} Songs Discovered — Starting Downloads…",
        description="\n".join(lines),
        color=0x1DB954
    )
    preview_embed.set_footer(text="PSudofy • Downloading now…")
    await announce.edit(embed=preview_embed)

    # Download all recs
    success_tracks = []
    failed_count = 0

    def do_weekly_download():
        nonlocal failed_count
        for r in recs:
            target = r.get("url") or f"{r['title']} {r['artist']}"
            res = run_single_download(target)
            if res.get("success"):
                success_tracks.append(res)
            else:
                failed_count += 1
        if success_tracks:
            trigger_navidrome_scan()
            navidrome_wait_scan(timeout=60)

    await loop.run_in_executor(None, do_weekly_download)

    color = 0x1DB954 if failed_count == 0 else 0xFEE75C
    result_embed = discord.Embed(
        title="🎵  Weekly Music Drop Complete!",
        description=(
            f"**{len(success_tracks)}** new songs added to your library"
            + (f" ({failed_count} failed)" if failed_count else "") + ".\n\n"
            "📲 Pull to refresh in Symfonium to listen!"
        ),
        color=color
    )
    result_embed.set_footer(text=f"PSudofy • Weekly Auto-Download • {datetime.datetime.now(_IST).strftime('%d %b %Y')}")
    await announce.edit(embed=result_embed)


# ────────────────────────── PSudofy Wrapped & Time Capsule ────────────────────

class WrappedPaginatorView(discord.ui.View):
    def __init__(self, ctx, data: dict, card_bytes: bytes):
        super().__init__(timeout=240)
        self.ctx = ctx
        self.data = data
        self.card_bytes = card_bytes
        self.current_page = 0
        self.pages = self._build_pages()

    def _build_pages(self) -> list[discord.Embed]:
        data = self.data
        tf_label = data.get("timeframe_label", "2026 EDITION")
        pers = data.get("personality", {})
        mins = data.get("total_minutes", 0)
        hrs = data.get("total_hours", 0)
        plays = data.get("total_plays", 0)
        top_art = data.get("top_artists", [])
        top_trk = data.get("top_tracks", [])

        # Slide 1: Overview & Visual Card
        embed1 = discord.Embed(
            title=f"✨  PSudofy Wrapped • {tf_label}",
            description=(
                f"**Here is your personal listening story for {tf_label}!**\n\n"
                f"⏱️ **Listening Time:** **{mins:,}** mins (~{hrs} hrs)\n"
                f"🎵 **Total Scrobbles:** **{plays:,}** tracks played\n"
                f"🔮 **Audio Personality:** **{pers.get('badge', 'THE SONIC EXPLORER')}**\n\n"
                f"*{pers.get('tagline', '')}*\n\n"
                "👇 *Use the buttons below to browse your top artists, tracks, and audio personality!*"
            ),
            color=0x1DB954
        )
        embed1.set_image(url="attachment://wrapped_card.png")
        embed1.set_footer(text="Slide 1 of 4 • PSudofy Wrapped • Use buttons to navigate")

        # Slide 2: Top Artists
        lines_art = []
        max_p = max([a.get("plays", 1) for a in top_art], default=1) or 1
        for i, a in enumerate(top_art[:5], 1):
            pct = int((a.get("plays", 0) / max_p) * 10)
            bar = "█" * pct + "░" * (10 - pct)
            lines_art.append(f"`#{i}` **{a['artist']}**\n`{bar}` **{a.get('plays', 0)}** plays")

        embed2 = discord.Embed(
            title=f"👑  Top Artists • {tf_label}",
            description="The voices and creators that defined your soundtrack:\n\n" + ("\n\n".join(lines_art) if lines_art else "No artist play history recorded yet."),
            color=0x9B59B6
        )
        if top_art:
            embed2.add_field(name="🥇 Top Artist", value=f"**{top_art[0]['artist']}** was your #1 soundtrack!", inline=False)
        embed2.set_thumbnail(url="attachment://wrapped_card.png")
        embed2.set_footer(text="Slide 2 of 4 • PSudofy Wrapped")

        # Slide 3: Top Tracks
        lines_trk = []
        for i, t in enumerate(top_trk[:5], 1):
            dur_total = int(t.get("duration", 210) or 210)
            dur_m = dur_total // 60
            dur_s = dur_total % 60
            p_cnt = int(t.get("plays", 1) or 1)
            lines_trk.append(f"`#{i}` **{t['title'][:36]}** — *{t['artist'][:22]}*\n└ 🔁 **{p_cnt}** plays · ⏱️ {dur_m}:{dur_s:02d}")

        embed3 = discord.Embed(
            title=f"🎶  Top Tracks • {tf_label}",
            description="Your most replayed songs on repeat:\n\n" + ("\n\n".join(lines_trk) if lines_trk else "No track play history recorded yet."),
            color=0x3498DB
        )
        if top_trk:
            embed3.add_field(name="🔥 Your #1 Song", value=f"**{top_trk[0]['title']}** by **{top_trk[0]['artist']}**", inline=False)
        embed3.set_thumbnail(url="attachment://wrapped_card.png")
        embed3.set_footer(text="Slide 3 of 4 • PSudofy Wrapped")

        # Slide 4: Audio Personality
        art_sample = ", ".join([f"**{a['artist']}**" for a in top_art[:4]]) or "Your favorites"
        embed4 = discord.Embed(
            title=f"🔮  Audio Personality: {pers.get('name', 'The Sonic Explorer')}",
            description=(
                f"### {pers.get('badge', '🎧 THE SONIC EXPLORER')}\n\n"
                f"*{pers.get('tagline', '')}*\n\n"
                f"**The Vibe:**\n{pers.get('desc', '')}\n\n"
                f"**Defining Artists in Your Orbit:**\n{art_sample}"
            ),
            color=0xE91E63
        )
        embed4.set_thumbnail(url="attachment://wrapped_card.png")
        embed4.set_footer(text="Slide 4 of 4 • PSudofy Wrapped")

        return [embed1, embed2, embed3, embed4]

    async def _update_message(self, interaction: discord.Interaction):
        embed = self.pages[self.current_page]
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary, custom_id="wrapped_prev")
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("Only the person who ran !wrapped can flip slides!", ephemeral=True)
            return
        self.current_page = (self.current_page - 1) % len(self.pages)
        await self._update_message(interaction)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.primary, custom_id="wrapped_next")
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("Only the person who ran !wrapped can flip slides!", ephemeral=True)
            return
        self.current_page = (self.current_page + 1) % len(self.pages)
        await self._update_message(interaction)

    @discord.ui.button(label="🎵 Create Time Capsule", style=discord.ButtonStyle.success, custom_id="wrapped_capsule")
    async def capsule_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        track_ids = self.data.get("capsule_track_ids", [])
        tf_label = self.data.get("timeframe_label", "2026")
        pl_name = f"PSudofy Wrapped: {tf_label}"

        ok = False
        if track_ids:
            ok = navidrome_create_playlist(pl_name, track_ids)
            trigger_navidrome_scan()

        if ok:
            button.disabled = True
            button.label = "✅ Capsule Created!"
            await interaction.response.edit_message(view=self)
            await interaction.followup.send(embed=discord.Embed(
                title=f"📑  Time Capsule Created: \"{pl_name}\"",
                description=(
                    f"Added **{len(track_ids)}** top tracks into Navidrome!\n\n"
                    "📲 **In Symfonium:** Pull down to refresh and listen to your Time Capsule!"
                ),
                color=0x1DB954
            ), ephemeral=False)
        else:
            await interaction.response.send_message("⚠️ Could not create playlist in database (tracks may not have local media IDs).", ephemeral=True)

    @discord.ui.button(label="🎨 Share Card", style=discord.ButtonStyle.secondary, custom_id="wrapped_share")
    async def share_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        file = discord.File(io.BytesIO(self.card_bytes), filename="wrapped_card.png")
        await interaction.response.send_message(
            content="Here is your visual **PSudofy Wrapped Share Card** 🎨✨",
            file=file,
            ephemeral=False
        )


@bot.command(name="wrapped", aliases=["spotifywrapped", "mywrapped", "yearinreview"])
async def wrapped_cmd(ctx, *, timeframe: str = "all_time"):
    """Generate your multi-slide Spotify Wrapped breakdown and high-res share card!
    Usage: !wrapped [month | year | all_time]
    Examples: !wrapped | !wrapped month | !wrapped 2026
    """
    clean_tf = timeframe.strip().lower()
    status_msg = await ctx.reply(embed=discord.Embed(
        title="✨  Generating Your PSudofy Wrapped…",
        description="Crunching your listening hours, top artists, tracks & audio personality…",
        color=0x5865F2
    ).set_footer(text="PSudofy • Spotify Wrapped Engine"))

    loop = asyncio.get_running_loop()

    def do_generate():
        data = get_wrapped_data(clean_tf)
        card_buf = generate_wrapped_card(data)
        return data, card_buf.getvalue()

    try:
        data, card_bytes = await loop.run_in_executor(None, do_generate)
        view = WrappedPaginatorView(ctx, data, card_bytes)
        embed = view.pages[0]
        file = discord.File(io.BytesIO(card_bytes), filename="wrapped_card.png")
        await status_msg.edit(content=None, embed=embed, view=view, attachments=[file])
    except Exception as e:
        await status_msg.edit(embed=discord.Embed(
            title="❌  Wrapped Generation Error",
            description=f"```{e}```",
            color=0xED4245
        ))


@bot.command(name="capsule", aliases=["timecapsule", "wrappedplaylist", "top50"])
async def capsule_cmd(ctx, *, name_or_tf: str | None = None):
    """Create a 1-click Time Capsule playlist of your top songs in Navidrome & Symfonium.
    Usage: !capsule [month | year | "Custom Name"]
    """
    timeframe = "all_time"
    custom_name = None

    if name_or_tf:
        clean = name_or_tf.strip().strip('"\'')
        if clean.lower() in ["month", "this_month", "monthly"]:
            timeframe = "month"
        elif clean.lower() in ["year", "this_year", "yearly", "2026"]:
            timeframe = "year"
        else:
            custom_name = clean

    status_msg = await ctx.reply(embed=discord.Embed(
        title="⏳  Crafting Your Time Capsule…",
        description="Selecting your most-played tracks from your listening history…",
        color=0x5865F2
    ).set_footer(text="PSudofy • Time Capsule Engine"))

    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, get_wrapped_data, timeframe)
    track_ids = data.get("capsule_track_ids", [])

    if not track_ids:
        # Fallback to top library tracks
        try:
            conn = sqlite3.connect(DB_PATH)
            track_ids = [r[0] for r in conn.execute("SELECT id FROM media_file WHERE missing=0 ORDER BY RANDOM() LIMIT 50").fetchall()]
            conn.close()
        except Exception:
            pass

    if not track_ids:
        await status_msg.edit(embed=discord.Embed(
            title="⚠️  No Tracks Found",
            description="Could not find enough tracks to generate a Time Capsule playlist.",
            color=0xFEE75C
        ))
        return

    tf_label = data.get("timeframe_label", "2026")
    pl_name = custom_name or f"PSudofy Wrapped: {tf_label}"

    ok = navidrome_create_playlist(pl_name, track_ids)
    trigger_navidrome_scan()

    if ok:
        top_preview = data.get("top_tracks", [])[:5]
        lines = [f"`{i+1}.` **{t['title'][:36]}** — *{t['artist'][:22]}*" for i, t in enumerate(top_preview)]
        
        embed = discord.Embed(
            title=f"📑  Time Capsule Created: \"{pl_name}\"",
            description=(
                f"Successfully compiled **{len(track_ids)}** of your favorite songs into a playlist!\n\n"
                "**Preview:**\n" + ("\n".join(lines) if lines else "• All favorite tracks compiled")
                + ("\n*...and more*" if len(track_ids) > 5 else "") + "\n\n"
                "📲 **In Symfonium:** Pull down to refresh — your Time Capsule is ready to stream!"
            ),
            color=0x1DB954
        )
        embed.set_footer(text=f"PSudofy • {len(track_ids)} tracks • {datetime.datetime.now().strftime('%d %b %Y')}")
        await status_msg.edit(embed=embed)
    else:
        await status_msg.edit(embed=discord.Embed(
            title="❌  Playlist Creation Failed",
            description="Could not write the playlist to the Navidrome database.",
            color=0xED4245
        ))


@bot.command(name="help")
async def help_cmd(ctx):
    embed = discord.Embed(
        title="🎵  PSudofy — Command Reference",
        description="Your personal self-hosted music server.\nAll downloads happen **on the server** — your device uses zero data!",
        color=0x5865F2
    )

    embed.add_field(
        name="📥  Downloading",
        value=(
            "`!dl <name or link>` — download a song, album, or playlist\n"
            "`!dlnp <link>` — download playlist **without** creating a Navidrome playlist\n"
            "`!dl <link> --as \"Name\"` — use a custom playlist name (quotes required)\n"
            "`!dlrecs` — download all songs from the latest `!recs` list"
        ),
        inline=False
    )
    embed.add_field(
        name="✨  Recommendations & Discovery",
        value=(
            "`!recs` — discover 8 new songs tailored to your taste\n"
            "`!recs <count>` — e.g. `!recs 15` for 15 recommendations (max 25)\n"
            "`!recs <count> <artist>` — e.g. `!recs 20 Arijit Singh`\n"
            "`!dlrecs` — one-click download of all recommended songs"
        ),
        inline=False
    )
    embed.add_field(
        name="📊  Spotify Wrapped & Time Capsule",
        value=(
            "`!wrapped` — multi-slide Spotify Wrapped breakdown + visual share card\n"
            "`!wrapped month` / `!wrapped year` — Wrapped for specific timeframe\n"
            "`!capsule` — create a 1-click Top 50 Time Capsule playlist in Navidrome"
        ),
        inline=False
    )
    embed.add_field(
        name="🔍  Library & Management",
        value=(
            "`!search <query>` — check if a song is already in your library\n"
            "`!health` — full library health report (stats, storage, issues)\n"
            "`!dupes` — find duplicate songs in your library\n"
            "`!stats` — song / artist / album / playlist counts\n"
            "`!scan` — force Navidrome to pick up new files\n"
            "`!smartplaylist` — generate smart themed playlists from your library\n"
            "`!tunnel` (or `!url`) — get active Cloudflare URL to bypass campus firewalls"
        ),
        inline=False
    )
    embed.add_field(
        name="🛑  Control",
        value="`!cancel` — immediately stop all running downloads",
        inline=False
    )
    embed.add_field(
        name="🔎  Diagnostics",
        value=(
            "`!log [n]` — last n downloads (default 10, max 25)\n"
            "`!failed` — list all songs that failed to download\n"
            "`!retryfailed` — auto-retry downloading all failed songs\n"
            "`!nolyrics` — tracks with no lyrics embedded\n"
            "`!retrylyrics` — auto-retry fetching lyrics for all missing tracks"
        ),
        inline=False
    )
    embed.add_field(
        name="🗓️  Automation",
        value=(
            "Weekly auto-download fires every **Sunday at 9 PM IST** and automatically\n"
            "discovers & downloads 8 new songs to your library.\n"
            "Set `WEEKLY_CHANNEL_ID` in `.env` to enable it."
        ),
        inline=False
    )
    embed.set_footer(text="PSudofy  •  Works in channels and DMs")
    await ctx.reply(embed=embed)


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("[ERROR] DISCORD_BOT_TOKEN is not set. Please add DISCORD_BOT_TOKEN to your .env file.", flush=True)
        import sys
        sys.exit(1)
    bot.run(DISCORD_TOKEN)

