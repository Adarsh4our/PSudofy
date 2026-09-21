#!/usr/bin/env python3
"""
PSudofy Wrapped Generator
=========================
Spotify Wrapped style stats aggregator, audio personality engine,
and high-res visual card generator using Pillow.
"""

import os
import io
import re
import math
import sqlite3
import datetime
import urllib.request
import urllib.parse
import json
from PIL import Image, ImageDraw, ImageFont

MUSIC_FOLDER = os.path.expanduser("~/PSudofy/music")
DB_PATH = os.path.expanduser("~/PSudofy/data/navidrome.db")
LB_USER_DEFAULT = os.getenv("LISTENBRAINZ_USER", "")

# ────────────────────────── Personality Profiles ──────────────────────────────

PERSONALITY_PROFILES = [
    {
        "name": "The Cosmic Dreamer",
        "badge": "THE COSMIC DREAMER",
        "tagline": "Atmospheric soundscapes, ethereal vocals, and interstellar frequencies.",
        "desc": "You live in reverberant synths and late-night introspective vibes. Your playlist feels like gazing at distant galaxies.",
        "artists": ["tame impala", "aurora", "syml", "novo amor", "lord huron", "cigarettes after sex", "beach house", "florence + the machine", "lorde"],
        "gradient": ((18, 12, 45), (68, 24, 120), (142, 45, 226)),  # Deep Purple / Violet
    },
    {
        "name": "The Soulful Romantic",
        "badge": "THE SOULFUL ROMANTIC",
        "tagline": "Cinematic melodies, heartfelt poetry, and uncontainable passion.",
        "desc": "Your heart beats in rich acoustic guitars, soulful vocals, and songs that could score a romantic blockbuster.",
        "artists": ["arijit singh", "pritam", "atif aslam", "jasleen royal", "mohit chauhan", "sonu nigam", "shreya ghoshal", "vishal-shekhar", "sachin-jigar"],
        "gradient": ((40, 10, 20), (140, 25, 50), (220, 50, 75)),  # Deep Crimson / Rose
    },
    {
        "name": "The Indie Poet",
        "badge": "THE INDIE POET",
        "tagline": "Warm coffee, honest acoustics, and songs written in journal entries.",
        "desc": "You appreciate raw storytelling, unplugged strums, and songs that feel like an intimate conversation at 2 AM.",
        "artists": ["anuv jain", "prateek kuhad", "the local train", "when chai met toast", "zaeden", "aditya a", "naalayak", "twin strings", "parvaaz"],
        "gradient": ((30, 20, 10), (120, 70, 25), (200, 125, 45)),  # Warm Amber / Golden Sand
    },
    {
        "name": "The Midnight Rebel",
        "badge": "THE MIDNIGHT REBEL",
        "tagline": "Overdriven guitars, relentless drums, and anthem-worthy choruses.",
        "desc": "When the lights go down, your volume goes all the way up. You thrive on gritty basslines and electric adrenaline.",
        "artists": ["linkin park", "imagine dragons", "queen", "nirvana", "coldplay", "arctic monkeys", "foals", "the strokes"],
        "gradient": ((15, 25, 40), (20, 75, 120), (0, 180, 216)),  # Electric Blue / Neon Cyan
    },
    {
        "name": "The Hitmaker Devotee",
        "badge": "THE HITMAKER DEVOTEE",
        "tagline": "Catchy hooks, polished production, and main-character energy.",
        "desc": "You have an ear for infectious rhythms, unstoppable pop hooks, and the biggest sounds in modern music.",
        "artists": ["the weeknd", "dua lipa", "billie eilish", "taylor swift", "drake", "post malone", "bruno mars", "ed sheeran"],
        "gradient": ((10, 35, 30), (15, 110, 80), (30, 215, 96)),   # Spotify Emerald / Mint
    },
    {
        "name": "The Sonic Explorer",
        "badge": "THE SONIC EXPLORER",
        "tagline": "Genre-fluid, curious, and endlessly dynamic music taste.",
        "desc": "Your library refuses to be put into a single box. You discover joy across every tempo, language, and era.",
        "artists": [],
        "gradient": ((20, 15, 40), (80, 30, 100), (255, 94, 98)),   # Sunset Nebula
    }
]


def determine_personality(top_artists: list[str]) -> dict:
    """Classify the user's audio personality based on top artists."""
    artist_blob = " ".join([a.lower() for a in top_artists])
    for profile in PERSONALITY_PROFILES[:-1]:
        for key_artist in profile["artists"]:
            if key_artist in artist_blob:
                return profile
    return PERSONALITY_PROFILES[-1]


# ────────────────────────── Data Aggregation ──────────────────────────────────

def get_wrapped_data(timeframe: str = "all_time", username: str | None = None) -> dict:
    """
    Fetch listening data from Navidrome SQLite DB + ListenBrainz.
    timeframe: 'all_time', 'month', 'year', or 'recent'
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    tf_label = "ALL-TIME"
    since_date = None

    if timeframe in ["month", "this_month", "monthly"]:
        tf_label = now.strftime("%B %Y").upper()
        since_date = (now - datetime.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    elif timeframe in ["year", "this_year", "yearly", "2026"]:
        tf_label = f"{now.year} EDITION"
        since_date = (now - datetime.timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        tf_label = f"{now.year} ALL-TIME"

    top_tracks = []
    top_artists = []
    total_seconds = 0
    total_plays = 0
    capsule_track_ids = []

    # 1. Query Navidrome SQLite Database
    try:
        if os.path.exists(DB_PATH):
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()

            date_clause = "AND a.play_date >= ?" if since_date else ""
            date_params = (since_date,) if since_date else ()

            # Top Tracks
            query = f"""
                SELECT mf.id, mf.title, mf.artist, mf.album, mf.duration, a.play_count, a.play_date
                FROM annotation a
                JOIN media_file mf ON a.item_id = mf.id
                WHERE a.item_type = 'media_file' AND a.play_count > 0 AND mf.missing = 0
                {date_clause}
                ORDER BY a.play_count DESC
                LIMIT 50
            """
            rows = cur.execute(query, date_params).fetchall()
            for r in rows:
                mid, title, artist, album, dur, count, pdate = r
                dur = int(dur or 210)
                top_tracks.append({
                    "id": mid,
                    "title": title or "Unknown Title",
                    "artist": artist or "Unknown Artist",
                    "album": album or "",
                    "duration": dur,
                    "plays": int(count or 1),
                })
                capsule_track_ids.append(mid)

            # Total Listening Time & Total Plays
            tot_row = cur.execute(f"""
                SELECT SUM(mf.duration * a.play_count), SUM(a.play_count)
                FROM annotation a
                JOIN media_file mf ON a.item_id = mf.id
                WHERE a.item_type = 'media_file' AND a.play_count > 0 AND mf.missing = 0
                {date_clause}
            """, date_params).fetchone()
            if tot_row:
                total_seconds = tot_row[0] or 0
                total_plays = tot_row[1] or 0

            # Top Artists
            art_query = f"""
                SELECT mf.artist, SUM(a.play_count) as tot_plays
                FROM annotation a
                JOIN media_file mf ON a.item_id = mf.id
                WHERE a.item_type = 'media_file' AND a.play_count > 0 AND mf.missing = 0
                {date_clause}
                GROUP BY lower(mf.artist)
                ORDER BY tot_plays DESC
                LIMIT 10
            """
            art_rows = cur.execute(art_query, date_params).fetchall()
            for ar in art_rows:
                if ar[0]:
                    top_artists.append({"artist": ar[0], "plays": ar[1]})

            conn.close()
    except Exception as e:
        print(f"[Navidrome Wrapped Query Error] {e}", flush=True)

    # 2. Fallback / Enrichment with ListenBrainz if local DB has few/no plays
    lb_user = username or LB_USER_DEFAULT
    if len(top_tracks) < 3 and lb_user:
        try:
            lb_range = "all_time"
            if "month" in timeframe:
                lb_range = "this_month"
            elif "year" in timeframe:
                lb_range = "this_year"

            url = f"https://api.listenbrainz.org/1/stats/user/{urllib.parse.quote(lb_user)}/artists?range={lb_range}"
            req = urllib.request.Request(url, headers={"User-Agent": "PSudofy-Wrapped/1.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                lb_artists = data.get("payload", {}).get("artists", [])
                if lb_artists and not top_artists:
                    for la in lb_artists[:8]:
                        top_artists.append({
                            "artist": la.get("artist_name", "Artist"),
                            "plays": la.get("listen_count", 1)
                        })

            # Also fetch top recordings
            r_url = f"https://api.listenbrainz.org/1/stats/user/{urllib.parse.quote(lb_user)}/recordings?range={lb_range}"
            r_req = urllib.request.Request(r_url, headers={"User-Agent": "PSudofy-Wrapped/1.0"})
            with urllib.request.urlopen(r_req, timeout=5) as r_resp:
                r_data = json.loads(r_resp.read().decode("utf-8"))
                recordings = r_data.get("payload", {}).get("recordings", [])
                if recordings and not top_tracks:
                    for rec in recordings[:20]:
                        top_tracks.append({
                            "id": None,
                            "title": rec.get("track_name", "Track"),
                            "artist": rec.get("artist_name", "Artist"),
                            "album": rec.get("release_name", ""),
                            "duration": 210,
                            "plays": rec.get("listen_count", 1)
                        })
                    total_plays = sum(t["plays"] for t in top_tracks)
                    total_seconds = total_plays * 210
        except Exception as e:
            print(f"[ListenBrainz Wrapped Enrichment Error] {e}", flush=True)

    # 3. Final Fallback if everything is empty: populate with top library tracks
    if not top_tracks:
        try:
            if os.path.exists(DB_PATH):
                conn = sqlite3.connect(DB_PATH)
                cur = conn.cursor()
                lib_rows = cur.execute("SELECT id, title, artist, album, duration FROM media_file WHERE missing=0 ORDER BY RANDOM() LIMIT 20").fetchall()
                for lr in lib_rows:
                    top_tracks.append({
                        "id": lr[0],
                        "title": lr[1] or "Track",
                        "artist": lr[2] or "Artist",
                        "album": lr[3] or "",
                        "duration": lr[4] or 200,
                        "plays": 1
                    })
                    capsule_track_ids.append(lr[0])
                total_plays = len(top_tracks)
                total_seconds = total_plays * 200
                conn.close()
        except Exception:
            pass

    # Extract artist names for personality check
    artist_names = [a["artist"] for a in top_artists] or [t["artist"] for t in top_tracks]
    personality = determine_personality(artist_names)

    total_minutes = int(total_seconds // 60)
    total_hours = round(total_minutes / 60, 1)

    return {
        "timeframe": timeframe,
        "timeframe_label": tf_label,
        "username": lb_user,
        "total_plays": total_plays,
        "total_minutes": total_minutes,
        "total_hours": total_hours,
        "top_artists": top_artists[:5],
        "top_tracks": top_tracks[:10],
        "capsule_track_ids": capsule_track_ids[:50],
        "personality": personality,
    }


# ────────────────────────── Visual Card Generator ─────────────────────────────

def _get_font(size: int, bold: bool = False):
    """Load a system TrueType font or fallback gracefully."""
    font_names = [
        "arialbd.ttf" if bold else "arial.ttf",
        "segui_bold.ttf" if bold else "segoeui.ttf",
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
        "Helvetica.ttf",
        "Arial.ttf",
    ]
    for fn in font_names:
        try:
            return ImageFont.truetype(fn, size)
        except Exception:
            continue
    return ImageFont.load_default()


def generate_wrapped_card(data: dict) -> io.BytesIO:
    """
    Generate a high-res 1080x1350 Spotify Wrapped style visual card.
    Returns a BytesIO stream containing the PNG image.
    """
    width, height = 1080, 1350
    img = Image.new("RGBA", (width, height), (12, 14, 20, 255))
    draw = ImageDraw.Draw(img)

    # 1. Gradient Background
    personality = data.get("personality", PERSONALITY_PROFILES[-1])
    c_top, c_mid, c_bot = personality.get("gradient", ((20, 15, 40), (80, 30, 100), (255, 94, 98)))

    for y in range(height):
        t = y / height
        if t < 0.5:
            # Interpolate c_top -> c_mid
            factor = t * 2.0
            r = int(c_top[0] + (c_mid[0] - c_top[0]) * factor)
            g = int(c_top[1] + (c_mid[1] - c_top[1]) * factor)
            b = int(c_top[2] + (c_mid[2] - c_top[2]) * factor)
        else:
            # Interpolate c_mid -> c_bot
            factor = (t - 0.5) * 2.0
            r = int(c_mid[0] + (c_bot[0] - c_mid[0]) * factor)
            g = int(c_mid[1] + (c_bot[1] - c_mid[1]) * factor)
            b = int(c_mid[2] + (c_bot[2] - c_mid[2]) * factor)
        draw.line([(0, y), (width, y)], fill=(r, g, b, 255))

    # Overlay subtle dark glass gradient
    glass = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    g_draw = ImageDraw.Draw(glass)
    g_draw.rectangle([(40, 40), (width - 40, height - 40)], fill=(15, 18, 28, 180), outline=(255, 255, 255, 35), width=2)
    img = Image.alpha_composite(img, glass)
    draw = ImageDraw.Draw(img)

    # Fonts
    font_badge = _get_font(22, bold=True)
    font_title = _get_font(48, bold=True)
    font_sec = _get_font(28, bold=True)
    font_item_title = _get_font(26, bold=True)
    font_item_sub = _get_font(20, bold=False)
    font_stat_num = _get_font(52, bold=True)
    font_stat_lbl = _get_font(18, bold=True)
    font_footer = _get_font(18, bold=False)

    # 2. Header
    # Pill Tag with dynamic width
    tf_text = data.get("timeframe_label", "2026 EDITION")
    pill_label = f"PSUDOFY • {tf_text}"
    try:
        text_w = draw.textlength(pill_label, font=font_badge)
    except Exception:
        text_w = len(pill_label) * 13
    pill_w = int(text_w) + 36
    pill_h = 42
    draw.rounded_rectangle([(70, 75), (70 + pill_w, 75 + pill_h)], radius=21, fill=(29, 185, 84, 230))
    draw.text((88, 84), pill_label, fill=(255, 255, 255), font=font_badge)

    draw.text((70, 135), "Your Wrapped", fill=(255, 255, 255), font=font_title)
    draw.line([(70, 195), (280, 195)], fill=(29, 185, 84), width=4)

    # 3. Audio Personality Card
    draw.rounded_rectangle([(70, 220), (width - 70, 360)], radius=24, fill=(25, 30, 48, 220), outline=(255, 255, 255, 50), width=1)
    draw.text((95, 240), "AUDIO PERSONALITY", fill=(180, 190, 215), font=font_badge)
    draw.text((95, 270), personality["badge"], fill=(255, 255, 255), font=font_sec)
    
    # Description
    desc_line = personality.get("tagline", "")
    draw.text((95, 315), desc_line[:75], fill=(210, 220, 240), font=font_item_sub)

    # 4. Stat Highlights Row (Minutes Listened + Total Scrobbles)
    box_w = (width - 140 - 20) // 2
    
    # Box 1: Minutes Listened
    draw.rounded_rectangle([(70, 385), (70 + box_w, 505)], radius=20, fill=(25, 30, 48, 200), outline=(29, 185, 84, 80), width=1)
    mins = data.get("total_minutes", 0)
    hrs = data.get("total_hours", 0)
    stat_val = f"{mins:,}" if mins < 10000 else f"{hrs}h"
    draw.text((95, 400), "MINUTES PLAYED", fill=(29, 185, 84), font=font_stat_lbl)
    draw.text((95, 425), stat_val, fill=(255, 255, 255), font=font_stat_num)
    draw.text((95, 480), f"≈ {hrs} hours on PSudofy", fill=(160, 170, 190), font=font_footer)

    # Box 2: Total Plays
    box2_x = 70 + box_w + 20
    draw.rounded_rectangle([(box2_x, 385), (box2_x + box_w, 505)], radius=20, fill=(25, 30, 48, 200), outline=(142, 45, 226, 80), width=1)
    plays = data.get("total_plays", 0)
    draw.text((box2_x + 25, 400), "TRACKS SCROBBLED", fill=(175, 110, 255), font=font_stat_lbl)
    draw.text((box2_x + 25, 425), f"{plays:,}", fill=(255, 255, 255), font=font_stat_num)
    draw.text((box2_x + 25, 480), "across your library", fill=(160, 170, 190), font=font_footer)

    # 5. Top Artists (Left Column)
    col_w = (width - 140 - 25) // 2
    artists_x = 70
    artists_y = 535
    draw.text((artists_x, artists_y), "TOP ARTISTS", fill=(255, 255, 255), font=font_sec)

    top_art = data.get("top_artists", [])
    item_y = artists_y + 45
    if not top_art:
        draw.text((artists_x, item_y + 10), "No artist plays this timeframe", fill=(160, 175, 200), font=font_item_sub)
        item_y += 72
    else:
        for i, a in enumerate(top_art[:5], 1):
            # Background entry pill
            draw.rounded_rectangle([(artists_x, item_y), (artists_x + col_w, item_y + 60)], radius=12, fill=(20, 24, 38, 180))
            # Rank badge
            draw.rounded_rectangle([(artists_x + 8, item_y + 10), (artists_x + 48, item_y + 50)], radius=8, fill=(29, 185, 84) if i == 1 else (45, 55, 80))
            draw.text((artists_x + 16, item_y + 18), f"#{i}", fill=(255, 255, 255), font=font_item_sub)
            # Artist name (cleaned)
            raw_name = a.get("artist", "Artist")
            art_name = re.split(r"[,/|]", raw_name)[0].strip()
            draw.text((artists_x + 58, item_y + 10), art_name[:20], fill=(255, 255, 255), font=font_item_title)
            # Play count
            p_cnt = a.get("plays", 0)
            draw.text((artists_x + 58, item_y + 36), f"{p_cnt} plays", fill=(160, 175, 200), font=font_footer)
            item_y += 72

    # 6. Top Tracks (Right Column)
    tracks_x = 70 + col_w + 25
    tracks_y = 535
    draw.text((tracks_x, tracks_y), "TOP TRACKS", fill=(255, 255, 255), font=font_sec)

    top_trk = data.get("top_tracks", [])
    t_item_y = tracks_y + 45
    if not top_trk:
        draw.text((tracks_x, t_item_y + 10), "No track plays this timeframe", fill=(160, 175, 200), font=font_item_sub)
        t_item_y += 72
    else:
        for i, t in enumerate(top_trk[:5], 1):
            # Entry pill
            draw.rounded_rectangle([(tracks_x, t_item_y), (tracks_x + col_w, t_item_y + 60)], radius=12, fill=(20, 24, 38, 180))
            # Rank badge
            draw.rounded_rectangle([(tracks_x + 8, t_item_y + 10), (tracks_x + 48, t_item_y + 50)], radius=8, fill=(142, 45, 226) if i == 1 else (45, 55, 80))
            draw.text((tracks_x + 16, t_item_y + 18), f"#{i}", fill=(255, 255, 255), font=font_item_sub)
            # Track Title & Artist
            t_title = t.get("title", "Title")
            raw_art = t.get("artist", "Artist")
            t_artist = re.split(r"[,/|]", raw_art)[0].strip()
            draw.text((tracks_x + 58, t_item_y + 10), t_title[:18], fill=(255, 255, 255), font=font_item_title)
            draw.text((tracks_x + 58, t_item_y + 36), t_artist[:22], fill=(160, 175, 200), font=font_footer)
            t_item_y += 72

    # 7. Time Capsule Banner
    capsule_y = max(item_y, t_item_y) + 20
    draw.rounded_rectangle([(70, capsule_y), (width - 70, capsule_y + 90)], radius=16, fill=(18, 30, 25, 220), outline=(29, 185, 84, 180), width=1)
    draw.text((95, capsule_y + 20), "TIME CAPSULE PLAYLIST READY", fill=(29, 185, 84), font=font_badge)
    draw.text((95, capsule_y + 48), "Type !capsule in Discord to save your Top 50 playlist in Navidrome", fill=(210, 230, 220), font=font_item_sub)

    # 8. Footer
    draw.line([(70, height - 90), (width - 70, height - 90)], fill=(255, 255, 255, 40), width=1)
    draw.text((70, height - 70), "PSudofy • Self-Hosted Music Cloud", fill=(170, 180, 205), font=font_footer)
    draw.text((width - 320, height - 70), "Powered by Navidrome & ListenBrainz", fill=(140, 150, 175), font=font_footer)

    # Export to BytesIO
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf
