
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

import yt_dlp
import subprocess
import requests
import os
import re
import shutil
from concurrent.futures import ThreadPoolExecutor

app = FastAPI()

templates = Jinja2Templates(directory="templates")

DOWNLOAD_DIR = "/downloads"
ARCHIVE_FILE = os.path.join(DOWNLOAD_DIR, "archive.txt")

JELLYFIN_URL = "http://jellyfin:8096"
JELLYFIN_KEY = os.getenv("JELLYFIN_API_KEY")


def safe_filename(name):
    name = re.sub(r'[<>:"/\\|?*]', '', str(name))
    return name.strip().rstrip(".")


def get_archive():
    """Load previously downloaded video IDs."""

    if not os.path.exists(ARCHIVE_FILE):
        return set()

    with open(ARCHIVE_FILE, "r") as f:
        return {
            line.strip().split()[-1]
            for line in f
            if line.strip()
        }


def add_to_archive(video_id):
    """Mark a successfully processed video."""

    with open(ARCHIVE_FILE, "a") as f:
        f.write(f"youtube {video_id}\n")



def get_video_info(url):

    opts = {
        "quiet": True,
        "skip_download": True,
        "js_runtimes": {"deno": {}},
    }

    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def timestamp_to_seconds(timestamp):
    parts = [int(x) for x in timestamp.split(":")]

    if len(parts) == 2:
        return parts[0] * 60 + parts[1]

    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]

    raise ValueError(f"Invalid timestamp: {timestamp}")


def detect_chapters(info):
    duration = info.get("duration")
    raw_chapters = info.get("chapters")
    parsed = []

    # Only process native YouTube chapters
    if raw_chapters:
        print(f"Found {len(raw_chapters)} native chapters")
        for ch in raw_chapters:
            start = ch.get("start_time") or ch.get("start")
            end = ch.get("end_time") or ch.get("end")
            title = ch.get("title")
            if start is not None and title:
                parsed.append({
                    "start_time": float(start), 
                    "end_time": float(end) if end else None, 
                    "title": title.strip()
                })

    if len(parsed) < 2:
        print(f"CHAPTER DETECTION FAILED: found only {len(parsed)} native chapters")
        return None

    # Automatically assign missing end times using the next chapter's start or total duration
    for i, chapter in enumerate(parsed):
        if chapter.get("end_time") is None:
            if i + 1 < len(parsed):
                chapter["end_time"] = parsed[i + 1]["start_time"]
            else:
                chapter["end_time"] = duration

    # Filter out zero or negative duration chapters
    parsed = [
        c for c in parsed
        if c["end_time"] is not None and c["end_time"] > c["start_time"]
    ]

    if len(parsed) < 2:
        print(f"CHAPTER VALIDATION FAILED: valid chapters < 2")
        return None

    print(f"SUCCESS: detected {len(parsed)} native chapters")
    for chapter in parsed:
        print(f"{chapter['start_time']} - {chapter['end_time']}: {chapter['title']}")

    return parsed


def download_video(video_url, video_index, playlist_dir, is_playlist):

    info = get_video_info(video_url)

    video_id = info.get("id")

    if not video_id:
        raise Exception("Could not determine video ID.")

    # Check archive
    archive = get_archive()

    if video_id in archive:
        return f"Skipped (already downloaded): {info.get('title')}"

    uploader = safe_filename(info.get("uploader") or "Unknown")
    title = safe_filename(info.get("title") or "Unknown")

    chapters = detect_chapters(info)
    print("=" * 60)
    print("VIDEO:", info.get("title"))
    print("VIDEO ID:", video_id)
    print("DESCRIPTION AVAILABLE:", bool(info.get("description")))
    print("DESCRIPTION:")
    print(info.get("description") or "DESCRIPTION IS EMPTY")
    print("YT-DLP CHAPTERS:", info.get("chapters"))
    print("DETECTED CHAPTERS:", chapters)
    print("=" * 60)
    # --------------------------------------------------
    # NO CHAPTERS: DOWNLOAD AS OPUS
    # --------------------------------------------------

    if not chapters:
        print(f"No chapters found, downloading as single Opus: {title}")

        prefix = f"{video_index:02d} - " if is_playlist else ""

        download_opts = {
            # Prefer YouTube's native Opus stream so no re-encode is needed
            "format": "bestaudio[acodec=opus]/bestaudio/best",

            "outtmpl": os.path.join(
                playlist_dir,
                f"{prefix}%(title)s.%(ext)s"
            ),

            "js_runtimes": {"deno": {}},

            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "opus",
                },
                {
                    "key": "EmbedThumbnail",
                },
                {
                    "key": "FFmpegMetadata",
                },
            ],

            "writethumbnail": True,

            "postprocessor_args": {
                "ffmpeg": [
                    "-metadata", f"artist={uploader}"
                ]
            },

            "quiet": False,
        }

        with yt_dlp.YoutubeDL(download_opts) as ydl:
            ydl.download([video_url])

        add_to_archive(video_id)

        return f"Downloaded Opus: {title}"

    # --------------------------------------------------
    # CHAPTERS FOUND: DOWNLOAD AND SPLIT INTO OPUS
    # --------------------------------------------------

    print(f"Found {len(chapters)} chapters: {title}")

    # Each video gets its own directory
    video_dir = os.path.join(
        playlist_dir,
        f"{video_index:02d} - {title} [{video_id}]"
        if is_playlist
        else f"{title} [{video_id}]"
    )

    os.makedirs(video_dir, exist_ok=True)

    temp_dir = os.path.join(video_dir, "_temp")
    os.makedirs(temp_dir, exist_ok=True)

    try:

        # Download original audio.
        # YouTube serves Opus natively (format 251), so prefer it: chapters
        # can then be cut with a stream copy instead of a re-encode.
        download_opts = {
            "format": "bestaudio[acodec=opus]/bestaudio/best",

            "outtmpl": os.path.join(
                temp_dir,
                "full_audio.%(ext)s"
            ),

            "js_runtimes": {"deno": {}},

            "quiet": False,
        }

        with yt_dlp.YoutubeDL(download_opts) as ydl:
            dl_info = ydl.extract_info(video_url, download=True)

        source_is_opus = (dl_info.get("acodec") or "").startswith("opus")

        # Find downloaded audio file
        audio_files = [
            os.path.join(temp_dir, f)
            for f in os.listdir(temp_dir)
            if not f.endswith(".part")
        ]

        if not audio_files:
            raise Exception("Audio download produced no file.")

        audio_file = audio_files[0]

        # Split chapters into individual OPUS files
        jobs = []

        for i, chapter in enumerate(chapters, start=1):

            start = chapter["start_time"]
            end = chapter.get("end_time")

            if end is None:
                end = info.get("duration")

            if end is None or end <= start:
                raise Exception(
                    f"Invalid chapter duration: {chapter.get('title')}"
                )

            chapter_title = safe_filename(
                chapter.get("title") or f"Chapter {i}"
            )

            output_file = os.path.join(
                video_dir,
                f"{i:02d} - {chapter_title}.opus"
            )

            # Skip existing chapter files
            if os.path.exists(output_file):
                print(f"Already exists: {output_file}")
                continue

            if source_is_opus:
                # Lossless and near-instant: just copy the Opus packets
                codec_args = ["-c:a", "copy"]
            else:
                codec_args = [
                    "-c:a", "libopus",
                    "-b:a", "128k",
                    "-vbr", "on",
                    "-application", "audio",
                ]

            cmd = [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel", "error",

                # -ss BEFORE -i = fast input seek (jumps straight to the
                # chapter instead of decoding everything before it)
                "-ss", str(start),
                "-t", str(end - start),
                "-i", audio_file,

                "-map", "0:a:0",
                "-vn",
                *codec_args,

                "-metadata", f"title={chapter_title}",
                "-metadata", f"artist={uploader}",
                "-metadata", f"album={title}",
                "-metadata", f"track={i}/{len(chapters)}",

                output_file,
            ]

            jobs.append((cmd, output_file))

        def run_job(job):
            cmd, output_file = job
            subprocess.run(cmd, check=True)
            print(f"Created: {output_file}")

        # Copies are I/O-bound and quick; re-encodes use one core each
        workers = 4 if source_is_opus else (os.cpu_count() or 2)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            # list() re-raises the first ffmpeg failure, if any
            list(pool.map(run_job, jobs))

        # Mark video completed only after all chapters succeed
        add_to_archive(video_id)

        return f"Downloaded {len(chapters)} chapters as Opus: {title}"

    finally:
        # Always remove temporary audio
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)


def process_url(url):

    # --------------------------------------------------
    # DETECT PLAYLIST OR SINGLE VIDEO
    # --------------------------------------------------

    opts = {
        "quiet": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "js_runtimes": {"deno": {}},
        "ignoreerrors": True,
    }

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if not info:
        raise Exception("Could not retrieve video information.")

    entries = info.get("entries")
    is_playlist = entries is not None

    if is_playlist:

        playlist_title = safe_filename(
            info.get("title") or "YouTube Playlist"
        )

        playlist_uploader = safe_filename(
            info.get("uploader") or "Unknown"
        )

        playlist_dir = os.path.join(
            DOWNLOAD_DIR,
            playlist_uploader,
            playlist_title
        )

        print(f"Processing playlist: {playlist_title}")

        entries = list(entries)

        results = []

        successful = 0
        skipped = 0
        failed = 0

        # --------------------------------------------------
        # PROCESS EACH PLAYLIST VIDEO
        # --------------------------------------------------

        for index, entry in enumerate(entries, start=1):

            if not entry:
                failed += 1
                results.append(f"{index}: Unavailable video")
                continue

            video_url = (
                entry.get("webpage_url")
                or entry.get("url")
            )

            if not video_url:
                video_id = entry.get("id")

                if video_id:
                    video_url = (
                        f"https://www.youtube.com/watch?v={video_id}"
                    )
                else:
                    failed += 1
                    results.append(f"{index}: Missing video URL")
                    continue

            if not video_url.startswith(("http://", "https://")):
                video_url = (
                    f"https://www.youtube.com/watch?v={video_url}"
                )

            print(
                f"\nProcessing video {index}/{len(entries)}: "
                f"{entry.get('title')}"
            )

            try:

                result = download_video(
                    video_url,
                    index,
                    playlist_dir,
                    True,
                )

                results.append(f"{index}: {result}")

                if result.startswith("Skipped"):
                    skipped += 1
                else:
                    successful += 1

            except Exception as e:

                failed += 1

                error = (
                    f"{index}: FAILED - "
                    f"{entry.get('title', 'Unknown')}: {e}"
                )

                print(error)
                results.append(error)

        summary = (
            f"Playlist: {playlist_title}\n"
            f"Total videos: {len(entries)}\n"
            f"Successful: {successful}\n"
            f"Skipped: {skipped}\n"
            f"Failed: {failed}\n\n"
            + "\n".join(results)
        )

        return summary

    # --------------------------------------------------
    # SINGLE VIDEO
    # --------------------------------------------------

    uploader = safe_filename(
        info.get("uploader") or "Unknown"
    )

    playlist = safe_filename(
        info.get("playlist") or "YouTube"
    )

    playlist_dir = os.path.join(
        DOWNLOAD_DIR,
        uploader,
        playlist
    )

    os.makedirs(playlist_dir, exist_ok=True)

    result = download_video(
        url,
        1,
        playlist_dir,
        False,
    )

    return result


# --------------------------------------------------
# FASTAPI ROUTES
# --------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):

    return templates.TemplateResponse(
        request,
        "index.html",
        {"message": ""}
    )


@app.post("/", response_class=HTMLResponse)
async def download(request: Request, url: str = Form(...)):

    try:

        message = process_url(url)

        # Refresh Jellyfin once after processing
        requests.post(
            f"{JELLYFIN_URL}/Library/Refresh",
            headers={"X-Emby-Token": JELLYFIN_KEY},
            timeout=10,
        )

        message += "\n\nJellyfin library refreshed!"

    except Exception as e:

        message = f"Download failed: {e}"

    return templates.TemplateResponse(
        request,
        "index.html",
        {"message": message}
    )
