<div align="center">

```
██████╗ ███████╗██╗   ██╗██████╗  ██████╗ ███████╗██╗   ██╗
██╔══██╗██╔════╝██║   ██║██╔══██╗██╔═══██╗██╔════╝╚██╗ ██╔╝
██████╔╝███████╗██║   ██║██║  ██║██║   ██║█████╗   ╚████╔╝ 
██╔═══╝ ╚════██║██║   ██║██║  ██║██║   ██║██╔══╝    ╚██╔╝  
██║     ███████║╚██████╔╝██████╔╝╚██████╔╝██║        ██║   
╚═╝     ╚══════╝ ╚═════╝ ╚═════╝  ╚═════╝ ╚═╝        ╚═╝   
```

**Your 24/7 Self-Hosted Music Server & Automated Discord Downloader**

[![Python](https://img.shields.io/badge/Python-3.10+-3776AB?style=flat&logo=python&logoColor=white)](https://python.org)
[![Discord.py](https://img.shields.io/badge/Discord.py-2.3+-5865F2?style=flat&logo=discord&logoColor=white)](https://github.com/Rapptz/discord.py)
[![yt--dlp](https://img.shields.io/badge/yt--dlp-latest-FF0000?style=flat&logo=youtube&logoColor=white)](https://github.com/yt-dlp/yt-dlp)
[![Navidrome](https://img.shields.io/badge/Navidrome-Subsonic-FF6600?style=flat)](https://navidrome.org)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED?style=flat&logo=docker&logoColor=white)](https://docker.com)

*Download songs, albums & playlists directly on your server using Discord. Stream everywhere in lossless quality with synced lyrics via Navidrome + Symfonium.*

</div>

---

## ✨ Features

- 🤖 **24/7 Discord Bot (`discord_bot.py`)**:
  - Request songs, albums, artists, or entire playlists with simple commands (`!dl <link or name>`).
  - **Zero device data**: Downloads happen entirely on your remote server's bandwidth.
  - **Instant Spotify Extraction**: Parses Spotify playlists, albums, and artist top tracks in `<0.5s` via Next.js metadata.
  - **Auto Playlist Creation**: Groups downloaded songs into playlists with identical names in Navidrome.
  - **Live Progress Reporting**: Realtime Discord progress bars and status updates.
  - **Synced & Plain Lyrics**: Fetches timestamped `.lrc` and plain lyrics from LRCLib and embeds them into ID3 tags.
  - **Deduplication & Smart Sync**: Skips existing songs in milliseconds by checking your local Navidrome SQLite database.

- 🎵 **Navidrome Music Server**:
  - Open-source Subsonic-compatible streaming server running via lightweight Docker.
  - Multi-user support, smart playlists, transcode on-the-fly, and instant library rescanning.

- 📱 **Seamless Client Ecosystem**:
  - Stream and cache for offline listening on **Symfonium** (Android), **Amperfy** / **play:Sub** (iOS), or any Subsonic client.

- 🛠️ **Power Utilities**:
  - `clean_titles.py`: Strips movie/bracket junk from track tags.
  - `smart_playlists.py`: Generates top-played and mood-based smart playlists.
  - `remove_duplicates.py`: Identifies and eliminates duplicate audio files.
  - `web_downloader.py`: Lightweight headless HTTP receiver for Android *HTTP Shortcuts* share-sheet integration.
  - `sync_libraries.py` / `sync.py`: Bidirectional sync and batch processing.

---

## 🤖 Discord Bot Command Reference

| Command | Usage | Description |
|---|---|---|
| `!dl <query or link>` | `!dl Kesariya Arijit Singh` | Download single song with lyrics and add to library |
| `!dl <playlist url>` | `!dl https://open.spotify.com/playlist/...` | Download full playlist and auto-create Navidrome playlist |
| `!dlnp <link>` | `!dlnp <spotify/yt url>` | Download playlist tracks **without** creating a Navidrome playlist |
| `!dl <url> --as "Name"` | `!dl <url> --as "Workout Hits"` | Save songs into a **custom playlist name** |
| `!cancel` | `!cancel` | Immediately terminate all active downloads, workers, and subprocesses |
| `!stats` | `!stats` | Display live library counters (songs, artists, albums, playlists) |
| `!scan` | `!scan` | Trigger instant Navidrome library rescan with live count confirmation |
| `!log [n]` | `!log 15` | View last `n` downloads history with lyrics status |
| `!failed` | `!failed` | List songs that failed to download with specific error reasons |
| `!nolyrics` | `!nolyrics` | List songs currently missing lyrics |
| `!retrylyrics` | `!retrylyrics` | Automatically retry fetching lyrics from LRCLib for missing tracks |
| `!help` | `!help` | Show categorized help and usage guide |

---

## 📁 Repository Structure

```
PSudofy/
├── discord_bot.py           # 24/7 Discord bot (Core download engine & library manager)
├── web_downloader.py        # Headless API receiver for Android HTTP Shortcuts
├── wrapped_generator.py     # Spotify-Wrapped style cards & audio personality engine
├── docker-compose.yml       # Navidrome streaming server configuration
├── requirements.txt         # Python dependencies
├── .env.example             # Configuration template
├── .gitignore               # Comprehensive exclusion rules
├── systemd/                 # Linux service definitions
│   ├── psudofy-discord.service
│   └── psudofy-api.service
├── scripts/
│   └── fix_ext_filenames.py # Audits & cleans up legacy .{ext}.mp3 files + DB updater
├── backup_library.py        # Automated library & database backup manager
├── clean_titles.py          # ID3 metadata title sanitizer
├── smart_playlists.py       # Smart playlist generator (Last.fm + ListenBrainz)
├── remove_duplicates.py     # Audio library duplicate cleaner
├── sync.py                  # CLI sync and remote uploader
├── sync_libraries.py        # Library synchronization tool
└── fetch_recommendations.py # Scrobble and recommendation tool
```

---

## 🚀 Quick Start & Installation

### 1. Clone & Install Dependencies

```bash
git clone https://github.com/Adarsh4our/PSudofy.git
cd PSudofy

# Create virtual environment (recommended)
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

pip install -r requirements.txt
```

### 2. Configure Environment

Copy `.env.example` to `.env` and fill in your details:

```bash
cp .env.example .env
nano .env
```

```env
DISCORD_BOT_TOKEN=your_bot_token_here
NAVIDROME_URL=http://localhost:4533
NAVI_USER=your_navidrome_user
NAVI_PASS=your_navidrome_password
MUSIC_FOLDER=./music
DB_PATH=./data/navidrome.db
```

### 3. Start Navidrome Server

```bash
docker compose up -d
```

Navidrome is now live at **http://localhost:4533** (or `http://<server-ip>:4533`).  
Create your initial admin user upon first visit.

### 4. Run the Discord Bot

```bash
python discord_bot.py
```

To run 24/7 as a background systemd daemon on Linux:

```bash
sudo cp systemd/psudofy-discord.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now psudofy-discord
```

---

## 📱 Connecting Your Phone (Symfonium / Subsonic)

1. Install **[Symfonium](https://symfonium.app)** (Android) or **play:Sub / Amperfy** (iOS).
2. Go to **Settings → Add Media Provider → Subsonic**.
3. Fill in:
   - **URL**: `http://<your-server-ip>:4533`
   - **Username**: *(your Navidrome username)*
   - **Password**: *(your Navidrome password)*
4. Tap **Save & Sync**.

---

## 🛠️ Maintenance & Utilities

- **Backup Library & Database**:
  ```bash
  python backup_library.py
  ```
  Archives the SQLite database and music index to prevent data loss.

- **Sanitize Legacy `.{ext}.mp3` Filenames**:
  ```bash
  # Preview files that will be renamed:
  python scripts/fix_ext_filenames.py --dry-run

  # Apply safe file renames and update SQLite database paths:
  python scripts/fix_ext_filenames.py --apply

  # Generate an executable bash script for your Linux cloud server:
  python scripts/fix_ext_filenames.py --server-script
  ```

---

## 🛡️ License

This project is open-source under the [MIT License](LICENSE).

