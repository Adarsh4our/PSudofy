#!/usr/bin/env python3
"""
PSudofy Headless YouTube Share Receiver API
============================================
Runs in the background on port 5001.
Receives share links from Android 'HTTP Shortcuts' app.
Downloads music, embeds lyrics, and triggers Navidrome scan.
No web UI / frontend.
"""

import os
import sys
import time
import json
import re
import urllib.request
import urllib.parse
import hashlib
import secrets
import subprocess
from datetime import datetime

from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv(os.path.expanduser("~/PSudofy/.env"))

app = Flask(__name__)

MUSIC_FOLDER = os.path.expanduser("~/PSudofy/music")
ARCHIVE_FILE = os.path.expanduser("~/PSudofy/downloaded_yt.txt")
NAVIDROME_URL = os.getenv("NAVIDROME_URL", "http://localhost:4533")
NAVI_USER = os.getenv("NAVI_USER", "adarsh4our")
NAVI_PASS = os.getenv("NAVI_PASS", "adarsh_@1234")

# Mutagen for lyrics
try:
    from mutagen.id3 import ID3, USLT, SYLT, Encoding, ID3NoHeaderError
    MUTAGEN = True
except ImportError:
    MUTAGEN = False


def _subsonic_token_params() -> dict:
    salt = secrets.token_hex(8)
    token = hashlib.md5((NAVI_PASS + salt).encode()).hexdigest()
    return {
        "u": NAVI_USER,
        "t": token,
        "s": salt,
        "v": "1.16.1",
        "c": "psudofy-share",
        "f": "json",
    }


def trigger_navidrome_scan():
    try:
        params = urllib.parse.urlencode(_subsonic_token_params())
        urllib.request.urlopen(f"{NAVIDROME_URL}/rest/startScan.view?{params}", timeout=5)
        return True
    except Exception:
        return False


def clean_lyric_title(title: str) -> str:
    t = re.sub(r"\s*\(from\s+[^)]+\)", "", title, flags=re.IGNORECASE)
    t = re.sub(r"\s*-\s*from\s+.+", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*\(feat\.?\s+[^)]+\)", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*\(ft\.?\s+[^)]+\)", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*\(official.*?\)", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*\[official.*?\]", "", t, flags=re.IGNORECASE)
    return t.strip()


def fetch_lyrics_lrclib(title: str, artist: str) -> tuple:
    try:
        params = urllib.parse.urlencode({
            "track_name": clean_lyric_title(title),
            "artist_name": artist.split('/')[0].split(',')[0].strip(),
        })
        url = f"https://lrclib.net/api/get?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": "PSudofy-Server/1.0"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("plainLyrics"), data.get("syncedLyrics")
    except Exception:
        pass
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


def embed_lyrics(mp3_path: str, plain: str, synced: str) -> bool:
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


def process_download(query_or_url: str):
    import yt_dlp

    query_or_url = query_or_url.strip()
    url_match = re.search(r"(https?://[^\s]+)", query_or_url)
    if url_match:
        target = url_match.group(1)
    else:
        target = f"ytsearch1:{query_or_url} song official audio"

    output_template = os.path.join(MUSIC_FOLDER, "%(artist,uploader)s", "%(title)s.%(ext)s")

    ydl_opts = {
        "format": "bestaudio/best",
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"},
            {"key": "FFmpegMetadata"},
            {"key": "EmbedThumbnail"},
        ],
        "outtmpl": output_template,
        "download_archive": ARCHIVE_FILE,
        "writethumbnail": True,
        "quiet": True,
        "no_warnings": True,
    }
    
    cookies_path = os.path.expanduser("~/PSudofy/cookies.txt")
    if os.path.exists(cookies_path):
        ydl_opts["cookiefile"] = cookies_path

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(target, download=True)
            if not info:
                raise Exception("No search results found")

            if "entries" in info:
                entry = info["entries"][0] if info["entries"] else info
            else:
                entry = info

            title = entry.get("title", "Unknown Title")
            artist = entry.get("artist") or entry.get("uploader") or "Unknown Artist"
            filename = ydl.prepare_filename(entry)
            mp3_path = os.path.splitext(filename)[0] + ".mp3"

            if os.path.exists(mp3_path):
                plain, synced = fetch_lyrics_lrclib(title, artist)
                if plain or synced:
                    embed_lyrics(mp3_path, plain, synced)

            trigger_navidrome_scan()

            return True, {
                "title": title,
                "artist": artist,
                "status": "success",
                "message": "Downloaded & added to library"
            }

    except Exception as e:
        return False, {
            "title": query_or_url[:40],
            "artist": "Error",
            "status": "error",
            "message": str(e)[:100]
        }


@app.route("/api/download", methods=["GET", "POST"])
def api_download():
    query = ""
    data = request.get_json(silent=True) or {}
    if isinstance(data, dict):
        query = data.get("query") or data.get("url") or data.get("text") or data.get("value") or ""
    
    if not query and request.form:
        query = request.form.get("query") or request.form.get("url") or request.form.get("text") or ""
    
    if not query:
        query = request.args.get("query") or request.args.get("url") or request.args.get("q") or request.args.get("text") or ""

    if not query:
        raw = request.get_data(as_text=True) or ""
        if raw.strip():
            m = re.search(r'"query":\s*"(.*?)"', raw, re.DOTALL)
            if m:
                query = m.group(1)
            else:
                query = raw.strip()

    print(f"[PSudofy API] Received download query: {query}", flush=True)

    if not query:
        return jsonify({"status": "error", "message": "No query or URL provided"}), 400

    ok, record = process_download(query)
    if ok:
        return jsonify({
            "status": "success",
            "title": record.get("title"),
            "artist": record.get("artist"),
            "message": record.get("message")
        })
    else:
        return jsonify({
            "status": "error",
            "message": record.get("message")
        }), 500


@app.route("/api/scan", methods=["GET", "POST"])
def api_scan():
    ok = trigger_navidrome_scan()
    return jsonify({"status": "ok" if ok else "error"})


@app.route("/api/health", methods=["GET"])
def api_health():
    return jsonify({"status": "ok", "service": "psudofy-share-receiver"})


@app.route("/shortcut.json", methods=["GET"])
def get_shortcut():
    return jsonify({
        "version": 4,
        "categories": [
            {
                "id": 1,
                "name": "PSudofy",
                "shortcuts": [
                    {
                        "id": 1,
                        "name": "PSudofy Download",
                        "description": "Download shared song to PSudofy library",
                        "url": "http://168.138.114.54:5001/api/download",
                        "method": "POST",
                        "bodyType": "custom_text",
                        "bodyContent": "{\"query\": \"{{text}}\"}",
                        "contentType": "application/json",
                        "responseHandling": {
                            "uiType": "toast",
                            "successOutput": "message"
                        }
                    }
                ]
            }
        ],
        "variables": [
            {
                "id": 1,
                "key": "text",
                "type": "text",
                "shareText": True,
                "shareTitle": True
            }
        ]
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=False)
