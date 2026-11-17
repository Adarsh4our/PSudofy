#!/usr/bin/env python3
"""
PSudofy Synced Lyrics Filler & Shared Matcher
=============================================
Finds synced (time-stamped) lyrics for MP3s and new downloads via:
  1. lrclib.net /api/get     (exact match across cleaned title/artist variants)
  2. lrclib.net /api/search  (fuzzy match + duration check)
  3. syncedlyrics package    (Musixmatch, NetEase, Megalobiz) - optional fallback

Also imported by discord_bot.py and web_downloader.py via find_best_lyrics().

Usage:
    python3 fill_synced_lyrics.py                    # strict dry run
    python3 fill_synced_lyrics.py --apply            # strict write
    python3 fill_synced_lyrics.py --loose --apply    # accept MV length mismatches
"""

import argparse
import difflib
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

if __name__ == "__main__" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

try:
    from mutagen.id3 import ID3, SYLT, USLT, Encoding, ID3NoHeaderError
    from mutagen.mp3 import MP3
    MUTAGEN = True
except ImportError:
    MUTAGEN = False

try:
    import syncedlyrics  # type: ignore
except ImportError:
    syncedlyrics = None

LRCLIB = "https://lrclib.net/api"
UA = {"User-Agent": "PSudofy-SyncedLyricsFiller/1.1 (https://github.com/PSudofy)"}
TS_RE = re.compile(r"\[\d+:\d+")
LRC_LINE = re.compile(r"\[(\d+):(\d+)(?:[.:](\d+))?\](.*)")
_SL_LOCK = threading.Lock()
_SL_LAST = 0.0

LABELS = {
    "t-series", "sony music india", "sonymusicindiavevo", "zee music company",
    "tips official", "saregama music", "saregama mix station", "yrf", "speed records",
    "best of lyrics and music", "best-series", "various artists", "desi music factory",
    "white hill music", "times music", "eros now music", "lyricallyfe toons",
    "unknown.artists", "unknown artist", "mashooriya originals",
}

NOISE_PARENS = (
    r"official|audio|video|lyric|lyrics|visuali[sz]er|4k|hd|hq|full song|full video|"
    r"from|feat\.?|ft\.?|with|prod\.?|vocals only|remaster|explicit|music video|"
    r"film version|soundtrack|song|bass boosted"
)


# ── Title / artist cleaning ───────────────────────────────────────────────────

def normalize(s: str) -> str:
    s = s or ""
    for a, b in [("｜", "|"), ("＂", '"'), ("，", ","), ("：", ":"), ("？", "?"),
                 ("–", "-"), ("—", "-"), ("’", "'"), (".{ext}", ""), ("{ext}", "")]:
        s = s.replace(a, b)
    s = re.sub(r"^[^\w(\[\"']+", "", s)  # strip leading emojis/bullets
    return re.sub(r"\s+", " ", s).strip()


def clean_title(t: str) -> str:
    t = normalize(t).split("|")[0]
    t = re.sub(r"^(?:lyrical|full video|full audio|bacardi sessions)\s*:\s*", "", t, flags=re.I)
    t = re.sub(rf"[\(\[][^\)\]]*\b({NOISE_PARENS})\b[^\)\]]*[\)\]]", "", t, flags=re.I)
    t = re.sub(r"\b(?:song\s+by|sung\s+by)\s+.+$", "", t, flags=re.I)
    t = re.sub(r"\s+(?:feat\.?|ft\.?|with|prod\.?\s*by)\s+.*$", "", t, flags=re.I)
    t = re.sub(r"\s+\bl\s+.+$", "", t)  # YouTube "Song l Movie l Actor" separator
    t = re.sub(
        r"[\s:-]*(?:full\s+video\s+song|full\s+song|lyrical\s+video|video\s+song|"
        r"best\s+audio\s+song|with\s+lyrics|lyrical|title\s+song|title\s+track)\b.*$",
        "", t, flags=re.I,
    )
    t = re.sub(r"\s+\b(?:song|lyrics|audio|video)$", "", t, flags=re.I)
    t = t.strip(" -.\"'")
    return re.sub(r"\s+", " ", t).strip()


def artists_of(a: str) -> list:
    a = re.sub(r"@\w+", lambda m: m.group(0)[1:], normalize(a))
    parts = re.split(
        r"\s*(?:,|/|&|;|•|\bfeat\.?\b|\bft\.?\b|\bwith\b|\bx\b|\band\b)\s*",
        a, flags=re.I,
    )
    return [p.strip(" -.\"'") for p in parts if p.strip(" -.\"'") and p.strip(" -.\"'").lower() not in LABELS]


def sim(a: str, b: str) -> float:
    a = re.sub(r"[^a-z0-9 ]", "", (a or "").lower()).strip()
    b = re.sub(r"[^a-z0-9 ]", "", (b or "").lower()).strip()
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if len(shorter) >= max(8, int(len(longer) * 0.65)) and re.search(rf"\b{re.escape(shorter)}\b", longer):
        return 0.9
    return difflib.SequenceMatcher(None, a, b).ratio()


def candidates(tag_title: str, tag_artist: str, folder: str = "", filename: str = "") -> tuple:
    artists = artists_of(tag_artist) + artists_of(folder)
    pipe_hints = []
    titles = []

    for raw in (tag_title, filename):
        if not raw:
            continue
        norm = normalize(raw)
        segs = [x.strip() for x in norm.split("|") if x.strip()]
        head = segs[0] if segs else norm
        pipe_hints.extend(segs[1:])

        m_by = re.search(r"\b(?:song|sung)\s+by\s+([^|(\[\"]+)", head, flags=re.I)
        if m_by:
            artists.extend(artists_of(m_by.group(1)))

        # Split "Artist - Title" or "Artist- Title"
        hyph = re.split(r"\s+-\s+|\s*-\s+(?=[A-Z\"'])", head, maxsplit=1)
        if len(hyph) == 2:
            left, right = hyph[0].strip(), hyph[1].strip()
            left_arts = artists_of(left)
            l_art = any(sim(la, a) > 0.8 for la in left_arts for a in artists) or left.lower() in LABELS
            r_art = any(sim(right, a) > 0.8 for a in artists)
            if l_art and not r_art:
                titles.append(clean_title(right))
                artists.extend(left_arts)
            elif r_art and not l_art:
                titles.append(clean_title(left))
            else:
                titles += [clean_title(right), clean_title(left)]
                artists.extend(left_arts)

        if ":" in head:
            after_col = head.split(":", 1)[1].strip()
            if len(after_col) > 2:
                titles.append(clean_title(after_col))

        titles.append(clean_title(head))

    if not artists and pipe_hints:
        for seg in pipe_hints:
            artists.extend(artists_of(seg))

    t_out, seen_t = [], set()
    for t in titles:
        if t and len(t) > 1 and t.lower() not in seen_t:
            seen_t.add(t.lower())
            t_out.append(t)

    a_out, seen_a = [], set()
    for a in artists:
        if a and len(a) > 1 and a.lower() not in seen_a:
            seen_a.add(a.lower())
            a_out.append(a)

    return t_out[:5], a_out[:5]


# ── Sources ───────────────────────────────────────────────────────────────────

def http_json(url: str, retries: int = 2):
    for i in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=10) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code == 429:
                time.sleep(4 * (i + 1))
                continue
        except Exception:
            time.sleep(1.0 * (i + 1))
    return None


def lrc_last_ts(lrc: str) -> float:
    last = 0.0
    for line in lrc.splitlines():
        m = LRC_LINE.match(line.strip())
        if m:
            last = max(last, int(m.group(1)) * 60 + int(m.group(2)))
    return last


def _has_heavy_cjk(text: str) -> bool:
    return sum(1 for c in text if "\u4e00" <= c <= "\u9fff") > 15


def plausible(lrc: str, duration: float, loose: bool = False) -> bool:
    if not lrc or not TS_RE.search(lrc) or _has_heavy_cjk(lrc):
        return False
    last = lrc_last_ts(lrc)
    if last <= 0:
        return False
    if not duration:
        return last >= 20
    if loose:
        # shortcut: accepts MV intro/outro drift up to 65s or 35%, could drift if cut differs mid-song
        return min(25.0, duration * 0.3) <= last <= max(duration + 65.0, duration * 1.35)
    return last <= duration + 12.0


def from_lrclib(titles: list, artists: list, duration: float, loose: bool = False) -> tuple:
    """Returns (synced_lrc, plain_text, source_desc)."""
    best_plain = None

    # 1) exact /get
    for t in titles[:4]:
        for a in (artists[:3] or [""]):
            p = {"track_name": t}
            if a:
                p["artist_name"] = a
            if duration and not loose:
                p["duration"] = int(duration)
            d = http_json(f"{LRCLIB}/get?{urllib.parse.urlencode(p)}")
            if not d:
                continue
            if not best_plain and d.get("plainLyrics") and not _has_heavy_cjk(d["plainLyrics"]):
                best_plain = d["plainLyrics"]
            lrc = d.get("syncedLyrics")
            if lrc and plausible(lrc, duration, loose=loose):
                return lrc, best_plain, f"lrclib:get ({d.get('trackName')} - {d.get('artistName')})"

    # 2) fuzzy /search
    max_dd = 65.0 if loose else 12.0
    best, best_score = None, 0.0
    for t in titles[:3]:
        queries = []
        for a in artists[:2]:
            queries.append({"q": f"{t} {a}"})
        queries.append({"q": t})

        for q in queries:
            res = http_json(f"{LRCLIB}/search?{urllib.parse.urlencode(q)}") or []
            for r in res:
                ts = sim(t, clean_title(r.get("trackName") or r.get("name") or ""))
                r_art = r.get("artistName") or ""
                as_ = max([sim(a, r_art) for a in artists] + [sim(a, x) for a in artists for x in artists_of(r_art)] or [0.0])
                dd = abs((r.get("duration") or 0) - duration) if duration and r.get("duration") else 0.0

                if ts < 0.78:
                    continue

                if not best_plain and r.get("plainLyrics") and (as_ >= 0.65 or (not artists and ts >= 0.9)):
                    if not _has_heavy_cjk(r["plainLyrics"]):
                        best_plain = r["plainLyrics"]

                lrc = r.get("syncedLyrics")
                if not lrc:
                    continue

                # Allow MV duration drift up to 55s (or 95s in --loose) when both title & artist strongly match
                strong_pair = ts >= 0.88 and as_ >= 0.78
                allowed_dd = max(max_dd, 95.0 if loose else 55.0) if strong_pair else max_dd
                if duration and dd > allowed_dd:
                    continue

                # In loose mode, guard short/generic titles against wrong-artist matches
                if (loose or dd > 12.0) and not strong_pair:
                    words = len(t.split())
                    if artists and as_ < 0.55 and not (words >= 3 and ts >= 0.92):
                        continue
                    if not artists and (words < 2 or len(t) < 9 or ts < 0.9):
                        continue

                if not plausible(lrc, duration, loose=(loose or strong_pair)):
                    continue

                score = ts * 2.0 + as_ - dd / (60.0 if (loose or strong_pair) else 20.0)
                if score > best_score:
                    best, best_score = r, score

        if best and best_score >= 2.2:
            break

    min_score = 1.65 if loose else 1.8
    if best and best_score >= min_score:
        tag = "lrclib:loose" if (loose and duration and abs((best.get("duration") or 0) - duration) > 12) else "lrclib:search"
        return best["syncedLyrics"], best_plain, f"{tag} ({best.get('trackName')} - {best.get('artistName')})"

    return None, best_plain, None


def from_syncedlyrics(titles: list, artists: list, duration: float, loose: bool = False) -> tuple:
    global _SL_LAST
    if not syncedlyrics:
        return None, None

    terms = []
    for t in titles[:2]:
        for a in artists[:2]:
            terms.append(f"{t} {a}")
        if len(t.split()) >= 2 and len(t) >= 10:
            terms.append(t)

    seen = set()
    unique_terms = []
    for term in terms:
        if term.lower() not in seen:
            seen.add(term.lower())
            unique_terms.append(term)

    for term in unique_terms[:2]:
        # Serialize provider scraping so Musixmatch/NetEase don't 429 under ThreadPoolExecutor
        with _SL_LOCK:
            wait = 0.35 - (time.time() - _SL_LAST)
            if wait > 0:
                time.sleep(wait)
            try:
                lrc = syncedlyrics.search(
                    term, synced_only=True,
                    providers=["Musixmatch", "NetEase"],
                )
            except Exception:
                lrc = None
            _SL_LAST = time.time()

        if lrc and plausible(lrc, duration, loose=loose):
            min_fill = 0.30 if loose else 0.45
            if not duration or lrc_last_ts(lrc) >= duration * min_fill:
                return lrc, f"syncedlyrics ({term})"
    return None, None


# ── Public API for discord_bot.py & web_downloader.py ─────────────────────────

def is_non_lyric_track(title: str, duration: float = 0.0) -> bool:
    if duration and duration > 900:
        return True  # >15 min nonstop mashup/mix
    return bool(re.search(r"\b(instrumental|karaoke)\b", title or "", flags=re.I))


def find_best_lyrics(
    title: str,
    artist: str,
    mp3_path: str = "",
    loose: bool = False,
    use_fallback: bool = True,
) -> tuple:
    """
    Shared entrypoint returning (plain_lyrics, synced_lyrics).
    Reads duration/tags/folder from mp3_path when available.
    """
    tag_title, tag_artist, duration, folder, stem = "", "", 0.0, "", ""
    if mp3_path and os.path.exists(mp3_path):
        p = Path(mp3_path)
        stem = p.stem
        folder = p.parent.name
        tag_title, tag_artist, duration = read_meta(p)

    if is_non_lyric_track(title or tag_title or stem, duration):
        return None, None

    titles, artists = candidates(title or tag_title, artist or tag_artist, folder, stem or title)
    if tag_title and tag_title != title:
        t2, a2 = candidates(tag_title, tag_artist, folder, stem)
        titles = list(dict.fromkeys(titles + t2))[:5]
        artists = list(dict.fromkeys(artists + a2))[:5]

    if not titles:
        return None, None

    synced, plain, _ = from_lrclib(titles, artists, duration, loose=loose)
    if not synced and use_fallback:
        synced, _ = from_syncedlyrics(titles, artists, duration, loose=loose)
    return plain, synced


# ── File helpers ──────────────────────────────────────────────────────────────

def has_synced(mp3: Path) -> bool:
    lrc = mp3.with_suffix(".lrc")
    if lrc.exists() and TS_RE.search(lrc.read_text("utf-8", "replace")):
        return True
    if not MUTAGEN:
        return False
    try:
        t = ID3(mp3)
        return bool(t.getall("SYLT")) or any(TS_RE.search(u.text or "") for u in t.getall("USLT"))
    except Exception:
        return False


def read_meta(mp3: Path):
    title = artist = ""
    duration = 0.0
    if not MUTAGEN:
        return title, artist, duration
    try:
        t = ID3(mp3)
        title = str(t.get("TIT2", "") or "")
        artist = str(t.get("TPE1", "") or "")
    except Exception:
        pass
    try:
        duration = MP3(mp3).info.length
    except Exception:
        pass
    return title, artist, duration


def parse_lrc(lrc: str):
    out = []
    for line in lrc.splitlines():
        m = LRC_LINE.match(line.strip())
        if m and m.group(4).strip():
            frac = (m.group(3) or "0")[:3].ljust(3, "0")
            ms = (int(m.group(1)) * 60 + int(m.group(2))) * 1000 + int(frac)
            out.append((m.group(4).strip(), ms))
    return out


def write_lyrics(mp3: Path, lrc: str):
    mp3.with_suffix(".lrc").write_text(lrc, encoding="utf-8")
    if not MUTAGEN:
        return
    try:
        tags = ID3(mp3)
    except ID3NoHeaderError:
        tags = ID3()
    tags.delall("USLT")
    tags.add(USLT(encoding=Encoding.UTF8, lang="eng", desc="", text=lrc))
    parsed = parse_lrc(lrc)
    if parsed:
        tags.delall("SYLT")
        tags.add(SYLT(encoding=Encoding.UTF8, lang="eng", format=2, type=1, desc="", text=parsed))
    tags.save(mp3, v2_version=3)


# ── CLI ───────────────────────────────────────────────────────────────────────

def process(mp3: Path, root: Path, use_fallback: bool, loose: bool):
    title, artist, duration = read_meta(mp3)
    folder = mp3.parent.name if mp3.parent != root else ""
    if is_non_lyric_track(title or mp3.stem, duration):
        return {"file": str(mp3.relative_to(root)), "titles": [title or mp3.stem], "artists": [artist],
                "duration": int(duration), "source": None, "lrc": None, "skipped_reason": "instrumental_or_mix"}

    titles, artists = candidates(title, artist, folder, mp3.stem)
    lrc, _, src = from_lrclib(titles, artists, duration, loose=loose)
    if not lrc and use_fallback:
        lrc, src = from_syncedlyrics(titles, artists, duration, loose=loose)
    return {"file": str(mp3.relative_to(root)), "titles": titles, "artists": artists,
            "duration": int(duration), "source": src, "lrc": lrc}


def _self_test():
    t, a = candidates("", "T-Series", "T-Series",
                      "Lyrical： Aaoge Jab Tum ｜ Jab We Met ｜ Kareena Kapoor ｜ Ustad Rashid Khan")
    assert "Aaoge Jab Tum" in t and "Ustad Rashid Khan" in a, (t, a)
    t2, a2 = candidates('Ambarsariya Fukrey" Song By Sona Mohapatra', "", "T-Series", "")
    assert "Sona Mohapatra" in a2 and "Ambarsariya Fukrey" in t2, (t2, a2)
    t3, a3 = candidates("TALWIINDER - HASEEN (feat. SIMAR) ｜ NDS (VISUALIZER)", "Talwiinder", "", "")
    assert t3[0] == "HASEEN" and "Talwiinder" in a3, (t3, a3)
    assert is_non_lyric_track("THE HEIST (INSTRUMENTAL)", 117)
    assert sim("Theme", "Teen Titans Theme") < 0.75
    assert sim("Naina", "Tose Naina-Tum Jo Aaye") < 0.75
    assert not plausible("[00:10.00] 這是中文歌詞測試一二三四五六七八九十", 180, loose=True)
    print("self-test passed")


def main():
    ap = argparse.ArgumentParser(description="Fill missing synced lyrics")
    ap.add_argument("--music", default=os.getenv("MUSIC_FOLDER", "~/PSudofy/music"))
    ap.add_argument("--apply", action="store_true", help="write lyrics (default: dry run)")
    ap.add_argument("--loose", action="store_true", help="accept MV intro/outro duration drift")
    ap.add_argument("--no-fallback", action="store_true", help="lrclib only, skip syncedlyrics")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--log", default="synced_lyrics_report.json")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        _self_test()
        return

    root = Path(args.music).expanduser()
    all_mp3 = sorted(root.rglob("*.mp3"))
    todo = [p for p in all_mp3 if not has_synced(p)]
    mode = "LOOSE" if args.loose else "STRICT"
    print(f"Library: {len(all_mp3)} songs | synced: {len(all_mp3) - len(todo)} | missing synced: {len(todo)}")
    if args.limit:
        todo = todo[:args.limit]
    if not todo:
        print("All songs already have synced lyrics.")
        return
    use_fb = not args.no_fallback
    if use_fb and not syncedlyrics:
        print("(syncedlyrics not installed - lrclib only. `pip install syncedlyrics` for more sources)")
    print(f"Searching {len(todo)} songs ({mode}, {'APPLY' if args.apply else 'DRY RUN'})...\n")

    found, missing = [], []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(process, p, root, use_fb, args.loose): p for p in todo}
        for i, f in enumerate(as_completed(futs), 1):
            mp3 = futs[f]
            try:
                r = f.result()
            except Exception as e:
                r = {"file": str(mp3.relative_to(root)), "source": None, "lrc": None, "error": str(e)}
            if r["lrc"]:
                if args.apply:
                    try:
                        write_lyrics(mp3, r["lrc"])
                    except Exception as e:
                        r["error"] = f"write failed: {e}"
                found.append(r)
                print(f"[{i:4}/{len(todo)}] OK   {r['file'][:68]}  <- {r['source']}")
            else:
                missing.append(r)
                print(f"[{i:4}/{len(todo)}] --   {r['file'][:68]}")

    print(f"\nFound synced lyrics: {len(found)}/{len(todo)}  | still missing: {len(missing)}")
    by_src = {}
    for r in found:
        k = (r["source"] or "").split(" ")[0]
        by_src[k] = by_src.get(k, 0) + 1
    print("By source:", by_src)
    for r in found:
        r.pop("lrc", None)
    for r in missing:
        r.pop("lrc", None)
    with open(args.log, "w", encoding="utf-8") as fh:
        json.dump({"run_at": datetime.now().isoformat(), "mode": mode, "applied": args.apply,
                   "found": found, "missing": sorted(missing, key=lambda x: x["file"])},
                  fh, indent=1, ensure_ascii=False)
    print(f"Report: {args.log}")
    if not args.apply and found:
        print("Dry run - re-run with --apply to write them.")


if __name__ == "__main__":
    main()
