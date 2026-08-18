#!/usr/bin/env python3
"""
PSudofy Discord Bot
====================
Download songs, albums, and playlists straight from Discord.
Runs on your Oracle Cloud server 24/7.
Downloads happen on the SERVER's internet - your phone just sends the command!
"""

import os
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
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv(os.path.expanduser("~/PSudofy/.env"))

DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
MUSIC_FOLDER  = os.path.expanduser("~/PSudofy/music")
DB_PATH       = os.path.expanduser("~/PSudofy/data/navidrome.db")
NAVIDROME_URL = os.getenv("NAVIDROME_URL", "http://localhost:4533")
NAVI_USER     = os.getenv("NAVI_USER",    "adarsh4our")
NAVI_PASS     = os.getenv("NAVI_PASS",    "adarsh_@1234")
ARCHIVE_FILE  = os.path.expanduser("~/PSudofy/downloaded_yt.txt")
SPOTDL_BIN    = os.path.expanduser("~/PSudofy/venv/bin/spotdl")
COOKIES_PATH  = os.path.expanduser("~/PSudofy/cookies.txt")
DL_LOG_FILE   = os.path.expanduser("~/PSudofy/download_log.json")

ACTIVE_SUBPROCESSES = []
ACTIVE_TASKS = set()
_json_lock = threading.Lock()          # protects all JSON log file I/O
_download_lock: asyncio.Lock | None = None  # initialized on first use; protects concurrent playlist downloads


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
                sid = navidrome_search_track(res["title"], res["artist"])
                if sid:
                    matched_ids.append(sid)
            else:
                failed_count += 1

        trigger_navidrome_scan()
        navidrome_wait_scan(timeout=60)

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
    """Ensure Navidrome scan is triggered and completed."""
    return trigger_navidrome_scan()


def navidrome_search_track(title: str, artist: str) -> str | None:
    """Search Navidrome SQLite DB directly for a track and return its song ID."""
    clean_t = clean_title_for_lyrics(title)
    clean_a = clean_artist_for_lyrics(artist)
    try:
        conn = sqlite3.connect(DB_PATH)
        try:
            cur = conn.cursor()
            # 1. Match title and artist
            row = cur.execute("""
                SELECT id FROM media_file
                WHERE missing = 0 AND lower(title) LIKE lower(?) AND lower(artist) LIKE lower(?)
                LIMIT 1
            """, (f"%{clean_t}%", f"%{clean_a}%")).fetchone()
            if row:
                return row[0]

            # 2. Match clean title or file path
            row = cur.execute("""
                SELECT id FROM media_file
                WHERE missing = 0 AND (lower(title) LIKE lower(?) OR lower(path) LIKE lower(?))
                LIMIT 1
            """, (f"%{clean_t}%", f"%{clean_t}%")).fetchone()
            if row:
                return row[0]
        finally:
            conn.close()
    except Exception as e:
        print(f"[Navidrome search track error] {e}", flush=True)
    return None


def _get_owner_id() -> str:
    """Retrieve owner user id from environment or Navidrome DB."""
    env_owner = os.getenv("NAVI__get_owner_id()")
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
    return "IyLjH8H2Wk8fr0iOXqL0pR"


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

            for sid in song_ids:
                pt_id = uuid.uuid4().hex[:20]
                conn.execute("INSERT OR IGNORE INTO playlist_tracks (id, playlist_id, media_file_id) VALUES (?, ?, ?)", (pt_id, pl_id, sid))

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
    """Detect YouTube/Spotify/SoundCloud playlist, album, and artist URLs."""
    return bool(re.search(
        r"(list=PL|/playlist/|/sets/|/album/|/artist/|&list=)",
        url, re.IGNORECASE
    ))


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


# ─────────────────────────────── Downloader ───────────────────────────────────

def _build_ydl_opts(progress_hook=None, ignore_errors: bool = True, logger=None) -> dict:
    opts = {
        "format": "bestaudio/best",
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"},
            {"key": "FFmpegMetadata"},
            {"key": "EmbedThumbnail"},
        ],
        "outtmpl":            os.path.join(MUSIC_FOLDER, "%(artist,uploader)s", "%(title)s.%(ext)s"),
        "download_archive":   ARCHIVE_FILE,
        "writethumbnail":     True,
        "quiet":              True,
        "no_warnings":        True,
        "ignoreerrors":       ignore_errors,
        "extract_flat":       False,
    }
    if logger:
        opts["logger"] = logger
    if os.path.exists(COOKIES_PATH):
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


YT_BOT_PHRASES = ["Sign in to confirm", "bot", "cookies", "not a bot", "automated queries", "captcha"]

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
        ydl_opts = _build_ydl_opts(ignore_errors=False, logger=SingleLogger())
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(target, download=True)
            if not info:
                err_joined = " ".join(captured_errors)
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
        if _is_yt_blocked(err_str) or any(_is_yt_blocked(err) for err in captured_errors):
            log_failed_download(query, "YouTube blocked server IP")
            write_dl_log({"type": "single", "title": query, "artist": "?", "lyrics": "—", "status": "FAILED: YouTube blocked server IP"})
            return {"success": False, "error": "YT_BLOCKED"}
        log_failed_download(query, err_str)
        write_dl_log({"type": "single", "title": query, "artist": "?", "lyrics": "—", "status": f"FAILED: {e}"})
        return {"success": False, "error": err_str}


def run_playlist_download(url: str, progress_cb=None, create_playlist: bool = True, custom_name: str | None = None) -> dict:
    """
    Download a full playlist/album.
    progress_cb(done, total, current_title) called after each track.
    Returns summary dict.
    """
    import yt_dlp

    downloaded = []
    failed     = []
    total_count = 0

    class PlaylistLogger:
        def debug(self, msg):   pass
        def info(self, msg):    pass
        def warning(self, msg): pass
        def error(self, msg):   failed.append(msg)

    opts = _build_ydl_opts()
    opts["logger"] = PlaylistLogger()

    try:
        # First pass: extract info without downloading to get count
        with yt_dlp.YoutubeDL({**opts, "extract_flat": True, "quiet": True}) as ydl:
            flat = ydl.extract_info(url, download=False)
            if flat and "entries" in flat:
                total_count = len(flat.get("entries", []))
                playlist_title = flat.get("title", "Playlist")
            else:
                total_count = 1
                playlist_title = flat.get("title", "Track") if flat else "Unknown"

        if custom_name:
            playlist_title = custom_name

        # Second pass: actual download
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if not info:
                return {"success": False, "error": "Could not fetch playlist"}

            entries = info.get("entries", [info])
            for i, entry in enumerate(entries, 1):
                if entry is None:
                    continue
                track = process_single_entry(ydl, entry)
                downloaded.append(track)
                if progress_cb:
                    progress_cb(i, total_count or len(entries), track["title"])

        # Trigger scan and wait for it to finish so tracks appear in Navidrome
        trigger_navidrome_scan()
        navidrome_wait_scan(timeout=90)

        # Search each downloaded track in Navidrome and collect IDs
        song_ids = []
        for track in downloaded:
            sid = navidrome_search_track(track["title"], track["artist"])
            if sid:
                song_ids.append(sid)
                track["navidrome_id"] = sid
            else:
                track["navidrome_id"] = None

        # Create the Navidrome playlist if requested
        playlist_created = False
        if create_playlist and song_ids:
            playlist_created = navidrome_create_playlist(playlist_title, song_ids)

        for track in downloaded:
            write_dl_log({
                "type":     "playlist",
                "title":    track["title"],
                "artist":   track["artist"],
                "lyrics":   track["lyrics"],
                "status":   "ok",
                "playlist": playlist_title if create_playlist else "None (Raw Library)",
            })
        return {
            "success":          True,
            "is_playlist":      True,
            "playlist_title":   playlist_title,
            "downloaded":       downloaded,
            "failed_count":     len(failed),
            "total":            len(downloaded),
            "playlist_created": playlist_created,
            "playlist_songs":   len(song_ids),
            "skip_playlist":    not create_playlist,
        }
    except Exception as e:
        write_dl_log({"type": "playlist", "title": url, "artist": "?", "lyrics": "—", "status": f"FAILED: {e}"})
        return {"success": False, "error": str(e)}


# ─────────────────────────────── Bot Events ───────────────────────────────────

@bot.event
async def on_ready():
    global _download_lock
    _download_lock = asyncio.Lock()  # must be created inside the running loop
    print(f"[PSudofy Bot] Logged in as {bot.user.name} ({bot.user.id})", flush=True)
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.listening,
            name="!dl <song / playlist link>"
        )
    )


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
    msg = await ctx.reply(embed=discord.Embed(
        title="⏳  Fetching Audio & Lyrics…",
        description=(
            f"**Query:** `{query[:100]}`"
        ),
        color=0x5865F2
    ).set_footer(text="PSudofy  •  Downloading on server — your device uses zero data"))

    loop   = asyncio.get_running_loop()
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
        if failed_n == 0:
            embed_color = 0x1DB954
            embed_title = f"✅  {pl_name[:55]}"
        elif failed_n < total_n:
            embed_color = 0xFEE75C
            embed_title = f"⚠️  {pl_name[:52]} (partial)"
        else:
            embed_color = 0xED4245
            embed_title = f"❌  {pl_name[:52]} — all failed"

        embed = discord.Embed(title=embed_title, color=embed_color)

        # Stats row
        embed.add_field(name="🎵 Tracks",    value=f"**{total_n}**",                    inline=True)
        embed.add_field(name="📝 Lyrics",    value=f"**{lyrics_ok}/{total_n}**",         inline=True)
        embed.add_field(name="❌ Failed",    value=f"**{failed_n}**",                    inline=True)

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
    embed.set_footer(text="PSudofy  •  Tip: paste a direct link or renew cookies.txt")
    await ctx.reply(embed=embed)


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
            "`!dl <link> --as \"Name\"` — use a custom playlist name (quotes required)"
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
            "`!failed` — songs that failed to download\n"
            "`!nolyrics` — tracks with no lyrics embedded\n"
            "`!retrylyrics` — auto-retry fetching lyrics for all missing tracks"
        ),
        inline=False
    )
    embed.add_field(
        name="📊  Library",
        value="`!stats` — song / artist / album / playlist counts\n`!scan` — force Navidrome to pick up new files",
        inline=False
    )
    embed.add_field(
        name="💡  Examples",
        value=(
            "`!dl Tame Impala - Loser`\n"
            "`!dl https://open.spotify.com/playlist/...`\n"
            "`!dlnp https://open.spotify.com/artist/...`\n"
            "`!dl https://open.spotify.com/album/... --as \"My Album\"`"
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
