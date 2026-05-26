<div align="center">

```
██████╗ ███████╗██╗   ██╗██████╗  ██████╗ ███████╗██╗   ██╗
██╔══██╗██╔════╝██║   ██║██╔══██╗██╔═══██╗██╔════╝╚██╗ ██╔╝
██████╔╝███████╗██║   ██║██║  ██║██║   ██║█████╗   ╚████╔╝ 
██╔═══╝ ╚════██║██║   ██║██║  ██║██║   ██║██╔══╝    ╚██╔╝  
██║     ███████║╚██████╔╝██████╔╝╚██████╔╝██║        ██║   
╚═╝     ╚══════╝ ╚═════╝ ╚═════╝  ╚═════╝ ╚═╝        ╚═╝   
```

**Your Self-Hosted Music Network**

[![Python](https://img.shields.io/badge/Python-3.10+-3776AB?style=flat&logo=python&logoColor=white)](https://python.org)
[![spotDL](https://img.shields.io/badge/spotDL-latest-1DB954?style=flat&logo=spotify&logoColor=white)](https://github.com/spotDL/spotify-downloader)
[![Navidrome](https://img.shields.io/badge/Navidrome-latest-FF6600?style=flat)](https://navidrome.org)
[![Docker](https://img.shields.io/badge/Docker-required-2496ED?style=flat&logo=docker&logoColor=white)](https://docker.com)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

*Download from Spotify & YouTube Music. Stream everywhere via Navidrome + Symfonium.*

</div>

---

## ✨ What is PSudofy?

PSudofy is a **self-hosted music system** that lets you:

- 📥 Download entire Spotify playlists and YouTube Music playlists to your PC
- 🎵 Stream them to your phone (or any device) via [Navidrome](https://navidrome.org)
- 📱 Use [Symfonium](https://symfonium.app) (or any Subsonic-compatible app) as your player
- 🔁 Keep your library in sync — re-run the script and only new songs are downloaded

No subscription. No ads. Your music, your server.

---

## 🛠️ Requirements

| Tool | Purpose | Install |
|------|---------|---------|
| **Python 3.10+** | Run the downloader | [python.org](https://python.org) |
| **Docker Desktop** | Run Navidrome | [docker.com](https://docker.com) |
| **spotDL** | Download from Spotify | `pip install spotdl` |
| **yt-dlp** | Download from YouTube Music | `pip install yt-dlp` |
| **FFmpeg** | Audio conversion | [ffmpeg.org](https://ffmpeg.org) |
| **Rich** | Beautiful terminal UI | `pip install rich` |
| **spotapi** | Playlist pre-scan | `pip install spotapi` |

### Install all Python dependencies at once

```bash
pip install spotdl yt-dlp rich spotapi
```

---

## 🚀 Quick Start

### 1. Clone the repo

```bash
git clone https://github.com/yourusername/PSudofy.git
cd PSudofy
```

### 2. Start Navidrome (your music server)

```bash
docker compose up -d
```

Navidrome will be available at **http://localhost:4533**  
First visit → create an admin account.

### 3. Run the downloader

```bash
python downloader.py
```

Paste a Spotify or YouTube Music URL when prompted:

```
🎵  Paste a Spotify or YouTube Music URL: https://open.spotify.com/playlist/xxxxx
```

Songs are saved to `./music/` and Navidrome auto-scans when the download finishes.

---

## 📁 Project Structure

```
PSudofy/
├── downloader.py            # Main downloader script
├── docker-compose.yml       # Navidrome server config
├── music/                   # Downloaded music (gitignored)
│   └── Artist/
│       └── Album/
│           └── Song - Artist.mp3
├── data/                    # Navidrome database & config (gitignored)
├── downloaded_spotify.txt   # spotDL archive — tracks downloaded songs
└── downloaded_yt.txt        # yt-dlp archive — tracks downloaded songs
```

---

## 📱 Connect Symfonium (or any Subsonic app)

1. Open **Symfonium** → ☰ menu → **Add a media provider**
2. Select **Subsonic**
3. Enter your details:

   | Field | Value |
   |-------|-------|
   | Server URL | `http://<your-pc-local-ip>:4533` |
   | Username | *(your Navidrome username)* |
   | Password | *(your Navidrome password)* |

4. Tap **Test connection** → Save

> Find your PC's local IP: `ipconfig` → look for `192.168.x.x`

---

## ⚙️ How It Works

### Spotify Downloads (spotDL)
```
PSudofy → spotDL → Spotify API (metadata) + YouTube (audio) → MP3 → ./music/
```

- Uses `--archive` to track downloaded songs — re-running only fetches new songs
- Pre-fetches the playlist in parallel with spotDL so archive skips are shown by name
- 4 parallel download threads for speed

### YouTube Music Downloads (yt-dlp)
```
PSudofy → yt-dlp → YouTube Music → MP3 → ./music/
```

- 3 parallel workers with live per-song progress bars
- Auto-embeds thumbnail and metadata

### Auto Scan
After every download, PSudofy calls Navidrome's Subsonic API to trigger an immediate scan — new songs appear in Symfonium within seconds.

---

## 🎨 Terminal UI

```
✓  Found 104 songs

  ⚠️  Skipped (archive): Tum Hi Ho — Arijit Singh
  ⚠️  Skipped (archive): Kesariya — Arijit Singh
  ✅ Apna Bana Le — Arijit Singh
  ✅ Raataan Lambiyan — Jubin Nautiyal
  ...

  Overall Progress  12.4 songs/min ━━━━━━━━━━━━━━━━ 67/104 · 0:02:41

  ╭────────── ✨  Download Complete ───────────╮
  │    ✅  Downloaded    │   12                 │
  │    ⚠️   Skipped       │   91                 │
  │    ❌  Failed        │    1                 │
  │    🎵  Total Songs   │  104                 │
  │    ⏱️   Time Taken    │  3m 12s              │
  ╰────────────────────────────────────────────╯
```

---

## 🌐 Access From Anywhere (Optional)

By default, PSudofy + Navidrome only works on your **home Wi-Fi**. To stream from anywhere:

### Tailscale (Recommended — Free)
1. Install [Tailscale](https://tailscale.com) on your PC and phone
2. Sign in with the same account on both
3. Your PC gets a permanent private IP (`100.x.x.x`)
4. Use `http://100.x.x.x:4533` in Symfonium — works over mobile data anywhere

### Offline Listening
In Symfonium, long-press any playlist/album → **Download** → songs saved to phone storage → play without internet or PC.

---

## ❓ FAQ

**Q: Will it re-download songs I already have?**  
A: No. Both spotDL and yt-dlp use archive files (`downloaded_spotify.txt`, `downloaded_yt.txt`) to track what's been downloaded. Re-running only fetches new songs.

**Q: The download shows "Skipped (archive)" for most songs — is that normal?**  
A: Yes! Those songs are already in your library. Skipped = already downloaded.

**Q: A song failed with "YT-DLP download error" — what do I do?**  
A: Some songs can't be found on YouTube (spotDL searches YouTube for the audio). Just re-run — it won't re-download what already succeeded.

**Q: Do I need a Spotify Premium account?**  
A: No. spotDL only uses the Spotify API for metadata (song name, artist, album art). The actual audio comes from YouTube, which is free.

**Q: My music doesn't appear in Navidrome after downloading.**  
A: PSudofy triggers an auto-scan, but if it fails (Navidrome not running), open `http://localhost:4533` → Settings → Start Scan.


