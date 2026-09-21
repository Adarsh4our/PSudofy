# -*- coding: utf-8 -*-
import sqlite3
import os
import json
import urllib.request

# 1. Navidrome Top Played Tracks
db_path = os.path.expanduser("~/PSudofy/data/navidrome.db")
if os.path.exists(db_path):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    top_tracks = cur.execute("""
        SELECT mf.title, mf.artist, a.play_count, a.play_date, mf.duration
        FROM annotation a
        JOIN media_file mf ON a.item_id = mf.id
        WHERE a.item_type = 'media_file' AND a.play_count > 0
        ORDER BY a.play_count DESC
        LIMIT 10
    """).fetchall()
    print("--- Navidrome Top 10 Tracks ---")
    total_minutes = 0
    for t, art, count, date, dur in top_tracks:
        print(f"[{count:2} plays] {t} — {art}")
    
    total_time_row = cur.execute("""
        SELECT SUM(mf.duration * a.play_count)
        FROM annotation a
        JOIN media_file mf ON a.item_id = mf.id
        WHERE a.item_type = 'media_file' AND a.play_count > 0
    """).fetchone()
    total_sec = total_time_row[0] or 0
    print(f"Total Listening Time: {int(total_sec // 3600)}h {int((total_sec % 3600) // 60)}m ({int(total_sec // 60)} minutes)")

    top_artists = cur.execute("""
        SELECT mf.artist, SUM(a.play_count) as total_plays
        FROM annotation a
        JOIN media_file mf ON a.item_id = mf.id
        WHERE a.item_type = 'media_file' AND a.play_count > 0
        GROUP BY mf.artist
        ORDER BY total_plays DESC
        LIMIT 5
    """).fetchall()
    print("\n--- Navidrome Top 5 Artists ---")
    for art, count in top_artists:
        print(f"[{count:2} plays] {art}")
    conn.close()

# 2. ListenBrainz Listening Stats
lb_user = "adarsh4our"
try:
    url = f"https://api.listenbrainz.org/1/stats/user/{lb_user}/artists?range=all_time"
    req = urllib.request.Request(url, headers={"User-Agent": "PSudofy-Stats/1.0"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        artists = data.get("payload", {}).get("artists", [])[:5]
        print(f"\n--- ListenBrainz Top Artists for {lb_user} ---")
        for a in artists:
            print(f"[{a.get('listen_count')} listens] {a.get('artist_name')}")
except Exception as e:
    print("ListenBrainz API error:", e)
