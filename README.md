# Jellyfin Homelab + YouTube Music Downloader

A small homelab setup that runs a [Jellyfin](https://jellyfin.org/) media server alongside a tiny web app that downloads music from YouTube straight into the Jellyfin library.

Paste a YouTube video or playlist URL into the web page, and the downloader:

1. Fetches the video(s) with [yt-dlp](https://github.com/yt-dlp/yt-dlp).
2. Uses the video's native YouTube **chapters** to split it into individual tracks.
3. Encodes each track to **Opus** (128 kbps VBR) with `ffmpeg`, tagging title and artist.
4. Saves the tracks into the shared music folder, organized by uploader / playlist / video.
5. Triggers a **Jellyfin library refresh** through the API so the new music shows up right away.

## Architecture

```
            ┌────────────────────────┐
 Browser ──▶│ yt-downloader :8080    │  FastAPI + yt-dlp + ffmpeg
            │                        │
            └──────────┬─────────────┘
                       │ writes .opus files       POST /Library/Refresh
                       ▼                                   │
              D:/Jellyfin/Music  ◀───── reads ──── ┌───────▼────────┐
                                                   │ jellyfin :8096 │
                                                   └────────────────┘
```

Both services run with Docker Compose and share the same music volume.

## Project structure

```
.
├── Downloader/
│   ├── app.py               # FastAPI app: download, split by chapter, encode, refresh Jellyfin
│   ├── templates/index.html # Single-page form for pasting a URL
│   ├── Dockerfile           # Python 3.12 + ffmpeg + Deno (JS runtime for yt-dlp)
│   ├── docker-compose.yml   # Jellyfin + downloader services
│   ├── requirements.txt
│   └── .env.example         # Template for the Jellyfin API key
└── config/config/           # Reference Jellyfin server config (XML), no secrets
```

## Requirements

- Docker and Docker Compose
- A folder layout for Jellyfin data. The compose file uses Windows paths under `D:/Jellyfin/` — change them to suit your machine:
  - `D:/Jellyfin/config` → Jellyfin config
  - `D:/Jellyfin/cache` → Jellyfin cache
  - `D:/Jellyfin/Music` → music library (shared with the downloader)

## Setup

1. **Clone the repo**

   ```bash
   git clone https://github.com/EdAmado/jellyfin.git
   cd jellyfin/Downloader
   ```

2. **Start Jellyfin first** and finish the setup wizard at <http://localhost:8096>. Add a *Music* library pointing to `/music`.

   ```bash
   docker compose up -d jellyfin
   ```

3. **Create an API key** in Jellyfin: *Dashboard → API Keys → +*.

4. **Configure the downloader**

   ```bash
   cp .env.example .env
   ```

   Then put your key in `.env`:

   ```
   JELLYFIN_API_KEY=your-jellyfin-api-key-here
   ```

5. **Start everything**

   ```bash
   docker compose up -d --build
   ```

6. Open <http://localhost:8080>, paste a YouTube URL and hit **Download**.

## How downloads are organized

```
Music/
└── <Uploader>/
    └── <Playlist or "YouTube">/
        └── 01 - <Video title> [<video id>]/
            ├── 01 - <Chapter 1>.opus
            ├── 02 - <Chapter 2>.opus
            └── ...
```

- An `archive.txt` file in the music folder records processed video IDs, so re-submitting a playlist only downloads new videos.
- Existing chapter files are skipped.
- Videos **without at least two native chapters are rejected** on purpose — the downloader is built for mixes/albums that should be split into tracks.

## Notes

- The downloader has no authentication. Keep port `8080` on your LAN only and don't expose it to the internet.
- `yt-dlp` is unpinned in `requirements.txt` so rebuilding the image picks up the latest release (YouTube changes often). Rebuild with `docker compose build --no-cache yt-downloader` if downloads start failing.
- Jellyfin's runtime data (database, users, logs, device IDs) and the `.env` file are excluded from git via `.gitignore`.
- Only download content you have the right to download.
