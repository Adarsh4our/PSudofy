# -*- coding: utf-8 -*-
"""
PSudofy Smart Music Downloader & De-duplication Manager
======================================================
Strictly prevents duplicates and unwanted version variants (acoustic, live,
remix, reprise, slowed, etc.) unless explicitly requested by the user.
"""

import os
import re
import sqlite3
from typing import Tuple, Optional, Dict, Any, Set

DB_PATH = os.path.expanduser(os.getenv("DB_PATH", "~/PSudofy/data/navidrome.db"))
MUSIC_FOLDER = os.path.expanduser(os.getenv("MUSIC_FOLDER", "~/PSudofy/music"))

# Variant Patterns (pattern, category label)
VARIANT_PATTERNS = [
    (r"\b(acoustic|unplugged|piano|guitar|orchestral)(\s+version|\s+edit|\s+mix)?\b", "Acoustic / Unplugged"),
    (r"\b(remix|club\s+mix|vip\s+mix|radio\s+edit|extended(\s+mix)?|dj\s+mix|dance\s+mix)\b", "Remix / Edit"),
    (r"\b(reprise|female\s+version|male\s+version|duet\s+version|film\s+version)\b", "Vocal Variation / Reprise"),
    (r"\b(slowed(\s*(&|\+)?\s*reverb)?|sped\s+up|speed\s+up|nightcore|lo-?fi(\s+version|\s+remix)?|8d\s+audio)\b", "Tempo / Pitch Modification"),
    (r"\b(live(\s+at|\s+in|\s+session)?|concert\s+recording|tour\s+edition)\b", "Live Cut"),
    (r"\b(mashup|jukebox|full\s+album|non-?stop)\b", "Compilation / Mashup"),
]

CLEAN_TAG_PATTERNS = [
    r"\s*\(from\s+[^)]+\)", r"\s*-\s*from\s+.+",
    r"\s*\(original\s+motion\s+picture\s+soundtrack\)",
    r"\s*\(soundtrack\)", r"\s*\[soundtrack\]",
    r"\s*\(ost\)", r"\s*\[ost\]",
    r"\s*\(feat\.?\s+[^)]+\)", r"\s*\(ft\.?\s+[^)]+\)", r"\s*\(with\s+[^)]+\)",
    r"\s*\[feat\.?\s+[^]]+\]", r"\s*\[ft\.?\s+[^]]+\]", r"\s*\[with\s+[^]]+\]",
    r"\s*\(official\s*(music\s*)?(video|audio|visualizer|lyric\s*video)?\)",
    r"\s*\[official\s*(music\s*)?(video|audio|visualizer|lyric\s*video)?\]",
    r"\s*\(lyrics?\)", r"\s*\[lyrics?\]",
    r"\s*\(audio\)", r"\s*\[audio\]",
    r"\s*\(hd\)", r"\s*\[hd\]", r"\s*\(4k\)", r"\s*\[4k\]",
    r"\s*\|\s*.+",
]

def normalize_core_title(title: str) -> str:
    """Extract core song title by stripping metadata, soundtrack tags, and variant words."""
    if not title:
        return ""
    t = title
    for pat in CLEAN_TAG_PATTERNS:
        t = re.sub(pat, "", t, flags=re.IGNORECASE)
    for pat, _ in VARIANT_PATTERNS:
        t = re.sub(pat, "", t, flags=re.IGNORECASE)
    t = re.sub(r"\bversion\b", "", t, flags=re.IGNORECASE)
    t = re.sub(r"[^\w\s]", " ", t)
    return " ".join(t.lower().split())

def get_artist_tokens(artist: str) -> Set[str]:
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

def detect_variant_type(text: str) -> Optional[str]:
    """Detect if title or query explicitly specifies a variant (acoustic, remix, slowed, etc.)."""
    if not text:
        return None
    for pat, label in VARIANT_PATTERNS:
        if re.search(pat, text, flags=re.IGNORECASE):
            return label
    return None

def check_duplicate(title: str, artist: str, user_query: str = "", connection: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """
    Check if a song or variant already exists in Navidrome DB.
    
    Returns:
        {
            "action": "DOWNLOAD" | "SKIP_DUPLICATE" | "SKIP_VARIANT",
            "reason": str,
            "existing_title": str,
            "existing_artist": str,
            "existing_path": str,
            "core_title": str,
            "variant_type": Optional[str]
        }
    """
    core_title = normalize_core_title(title)
    in_tokens = get_artist_tokens(artist)
    detected_variant = detect_variant_type(title)
    user_explicit_variant = detect_variant_type(user_query) is not None

    if not core_title:
        return {"action": "DOWNLOAD", "reason": "New track", "core_title": core_title, "variant_type": None}

    conn = connection
    should_close = False
    
    if conn is None:
        db_candidates = [
            DB_PATH,
            os.path.expanduser("~/PSudofy/data/navidrome.db"),
            "./data/navidrome.db",
            "data/navidrome.db"
        ]
        db_file = None
        for cand in db_candidates:
            if os.path.exists(cand):
                db_file = cand
                break

        if not db_file:
            return {"action": "DOWNLOAD", "reason": "DB not found", "core_title": core_title, "variant_type": detected_variant}

        try:
            conn = sqlite3.connect(db_file)
            should_close = True
        except Exception:
            return {"action": "DOWNLOAD", "reason": "DB connect error", "core_title": core_title, "variant_type": detected_variant}

    try:
        cur = conn.cursor()
        rows = cur.execute("""
            SELECT id, title, artist, path FROM media_file
            WHERE missing = 0
        """).fetchall()

        for r in rows:
            db_id, db_title, db_artist, db_path = r
            db_core = normalize_core_title(db_title or "")
            db_tokens = get_artist_tokens(db_artist or "")
            db_variant = detect_variant_type(db_title or "")

            # Title matching: exact core match or strong inclusion
            title_match = False
            if core_title and db_core:
                if core_title == db_core:
                    title_match = True
                elif len(core_title) > 4 and core_title in db_core:
                    title_match = True
                elif len(db_core) > 4 and db_core in core_title:
                    title_match = True

            # Artist matching: overlap or matching primary token
            artist_match = not in_tokens or not db_tokens or bool(in_tokens & db_tokens)

            if title_match and artist_match:
                # 1. Exact / Standard Duplicate
                if not detected_variant or (detected_variant and db_variant == detected_variant):
                    return {
                        "action": "SKIP_DUPLICATE",
                        "reason": f"Standard version already exists in library: `{db_artist} - {db_title}`",
                        "existing_title": db_title,
                        "existing_artist": db_artist,
                        "existing_path": db_path,
                        "core_title": core_title,
                        "variant_type": detected_variant
                    }

                # 2. Unwanted Variant when Standard Version is already in library
                if detected_variant and not user_explicit_variant:
                    return {
                        "action": "SKIP_VARIANT",
                        "reason": f"Ignored {detected_variant} variant because original is already present: `{db_artist} - {db_title}`",
                        "existing_title": db_title,
                        "existing_artist": db_artist,
                        "existing_path": db_path,
                        "core_title": core_title,
                        "variant_type": detected_variant
                    }

    except Exception as e:
        print(f"[Dedup DB check error] {e}", flush=True)
    finally:
        if should_close and conn:
            conn.close()

    return {
        "action": "DOWNLOAD",
        "reason": "New track added to library",
        "core_title": core_title,
        "variant_type": detected_variant
    }
