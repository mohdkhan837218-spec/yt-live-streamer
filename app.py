"""
YouTube 24/7 Live Streamer — app.py (Ultimate Pro Edition with Full CORS & Zero-Lag Stream)
========================================================================================
Flask + Telegram Bot + FFmpeg + Live Upload Progress + Live Video Preview + YouTube SEO Engine:
  - Full CORS Support (Allow all origins for Hugging Face Static & Cloud embeds)
  - Multiple video sources (Direct URL / PC Upload / Google Drive / Dropbox / YouTube via yt-dlp)
  - Real-Time PC Upload Progress Bar & Video Preview Window
  - Real-Time FFmpeg Health & Speed Stats Monitor (FPS, Bitrate, Speed, Health Badge)
  - Ultra-Fast Zero Lag Presets (360p 500Kbps / 480p 900Kbps)
  - YouTube Live SEO & Metadata Suite (Title, Description, Tags Generator, Thumbnail Upload)
"""

import os
import re
import signal
import subprocess
import threading
import uuid
import asyncio
import logging
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import psutil
import requests
from flask import Flask, jsonify, render_template_string, request, make_response

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("yt-streamer")

# ─── Flask App ─────────────────────────────────────────────────────────────────
app = Flask(__name__)

# ─── CORS Support ──────────────────────────────────────────────────────────────
@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return response

# ─── Upload Directories ────────────────────────────────────────────────────────
UPLOAD_DIR = Path("/tmp/yt_streamer_uploads")
THUMB_DIR  = Path("/tmp/yt_streamer_thumbnails")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
THUMB_DIR.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB

# ─── Platform RTMP Endpoints ───────────────────────────────────────────────────
PLATFORM_RTMP = {
    "youtube":  "rtmp://a.rtmp.youtube.com/live2/{}",
    "facebook": "rtmps://live-api-s.facebook.com:443/rtmp/{}",
    "twitch":   "rtmp://live.twitch.tv/app/{}",
    "custom":   "{}",
}

# ─── Quality Presets (Tuned for Zero Lag) ──────────────────────────────────────
QUALITY_PRESETS = {
    "360p":     {"vb": "500k",  "maxrate": "500k",  "bufsize": "1000k", "scale": "640:360"},
    "480p":     {"vb": "900k",  "maxrate": "900k",  "bufsize": "1800k", "scale": "854:480"},
    "720p":     {"vb": "1800k", "maxrate": "1800k", "bufsize": "3600k", "scale": "1280:720"},
    "1080p":    {"vb": "3500k", "maxrate": "3500k", "bufsize": "7000k", "scale": "1920:1080"},
    "1080p_hq": {"vb": "5000k", "maxrate": "5000k", "bufsize": "10000k", "scale": "1920:1080"},
    "4k":       {"vb": "7500k", "maxrate": "7500k", "bufsize": "15000k", "scale": "3840:2160"},
}


# ═══════════════════════════════════════════════════════════════════════════════
# Stream State & Stats
# ═══════════════════════════════════════════════════════════════════════════════

_state_lock = threading.Lock()


class StreamState:
    process: Optional[subprocess.Popen] = None
    video_url: str = ""
    stream_key: str = ""
    platform: str = "youtube"
    destinations: list[str] = []
    active_platforms: list[str] = ["youtube"]
    quality: str = "360p"
    fps: int = 30
    audio_bitrate: str = "128k"
    auto_reconnect: bool = True
    started_at: Optional[datetime] = None
    is_active: bool = False
    uploaded_file: Optional[Path] = None

    attached_source_name: str = ""
    attached_source_type: str = ""
    attached_size_mb: float = 0.0
    attached_status: str = "none"

    title: str = "24/7 Non-Stop Live Stream 🔴"
    description: str = "Welcome to our 24/7 non-stop continuous live stream! Enjoy watching."
    tags: str = "live, 24/7, streaming, youtube live, stream"
    category_id: str = "24"
    thumbnail_path: Optional[str] = None


stream_state = StreamState()

ffmpeg_live_stats: dict = {
    "frame": 0,
    "fps": 0.0,
    "bitrate": "0kbits/s",
    "speed": "0x",
    "size_kb": 0,
    "time": "00:00:00",
    "health": "offline",
}


# ═══════════════════════════════════════════════════════════════════════════════
# Robust Universal Cloud URL Resolvers (Google Drive, Dropbox, YouTube, CDN)
# ═══════════════════════════════════════════════════════════════════════════════

def resolve_gdrive_url(url: str) -> tuple[str, str]:
    match = re.search(r"/d/([a-zA-Z0-9_-]+)", url) or re.search(r"[?&]id=([a-zA-Z0-9_-]+)", url)
    if not match:
        return "", "Google Drive: Invalid URL format. Could not extract file ID."

    file_id = match.group(1)

    # 1. Try yt-dlp for Google Drive file
    try:
        gdrive_view_url = f"https://drive.google.com/file/d/{file_id}/view"
        res = subprocess.run(
            ["yt-dlp", "-g", "--no-warnings", gdrive_view_url],
            capture_output=True,
            text=True,
            timeout=25,
        )
        if res.returncode == 0 and res.stdout.strip():
            direct = res.stdout.strip().splitlines()[0]
            logger.info("Google Drive resolved via yt-dlp: %s", direct[:60])
            return direct, ""
    except Exception as exc:
        logger.debug("gdrive yt-dlp attempt failed: %s", exc)

    # 2. Try requests session to follow confirmation tokens
    try:
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        })
        base_url = "https://docs.google.com/uc?export=download"
        r = session.get(base_url, params={"id": file_id}, stream=True, timeout=15)

        for key, value in r.cookies.items():
            if key.startswith("download_warning"):
                r = session.get(base_url, params={"id": file_id, "confirm": value}, stream=True, timeout=15)
                logger.info("Google Drive resolved via confirm cookie: %s", r.url[:60])
                return r.url, ""

        if "drive.google.com" not in r.url and "googleusercontent.com" in r.url:
            return r.url, ""

    except Exception as exc:
        logger.debug("gdrive requests attempt failed: %s", exc)

    # 3. Direct export fallback
    return f"https://drive.google.com/uc?export=download&id={file_id}&confirm=t", ""


def resolve_dropbox_url(url: str) -> tuple[str, str]:
    direct = re.sub(r"[?&]dl=0", "", url)
    if "?" in direct:
        direct += "&dl=1"
    else:
        direct += "?dl=1"
    return direct, ""


def resolve_youtube_url(url: str) -> tuple[str, str]:
    try:
        result = subprocess.run(
            [
                "yt-dlp",
                "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best/b",
                "--no-warnings",
                "--no-call-home",
                "--get-url",
                url,
            ],
            capture_output=True,
            text=True,
            timeout=35,
        )
        # Extract direct stream URLs from stdout
        stdout_lines = result.stdout.strip().splitlines()
        urls = [line.strip() for line in stdout_lines if line.strip().startswith("http") or "googlevideo.com" in line or "manifest" in line]
        if urls:
            return urls[0], ""

        # If stdout has any line, return it
        if result.stdout.strip():
            for line in stdout_lines:
                if "http" in line:
                    return line.strip(), ""

        # Filter out WARNING lines from stderr
        err_lines = [line for line in result.stderr.strip().splitlines() if not line.startswith("WARNING:")]
        clean_err = "\n".join(err_lines[:5]).strip() or result.stderr.strip()[:300] or "Could not extract video stream."
        return "", f"YouTube URL Error: {clean_err}"
    except FileNotFoundError:
        return "", "yt-dlp is not installed."
    except subprocess.TimeoutExpired:
        return "", "yt-dlp timed out fetching YouTube media stream."
    except Exception as exc:
        return "", str(exc)


def resolve_video_url(raw_url: str, source_type: str) -> tuple[str, str]:
    raw_url = raw_url.strip()
    if not raw_url:
        return "", "Empty URL provided."

    # Auto-detect source if needed
    if "drive.google.com" in raw_url or "docs.google.com" in raw_url or source_type == "gdrive":
        return resolve_gdrive_url(raw_url)
    if "dropbox.com" in raw_url or source_type == "dropbox":
        return resolve_dropbox_url(raw_url)
    if "youtube.com" in raw_url or "youtu.be" in raw_url or source_type == "youtube":
        return resolve_youtube_url(raw_url)

    # Universal yt-dlp check for other cloud video platforms (Vimeo, Dailymotion, Facebook, Twitch clips, etc.)
    if source_type in {"direct", "cloud", ""}:
        try:
            res = subprocess.run(
                ["yt-dlp", "-g", "--no-warnings", raw_url],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if res.returncode == 0 and res.stdout.strip():
                extracted = res.stdout.strip().splitlines()[0]
                logger.info("Universal yt-dlp resolved URL: %s", extracted[:60])
                return extracted, ""
        except Exception:
            pass

def auto_extract_video_metadata(target_url_or_path: str, source_type: str, file_name: str = "") -> dict:
    """Extracts Title, Description, Tags, and Thumbnail frame automatically from any video source."""
    extracted_title = ""
    extracted_desc = ""
    extracted_tags = []
    extracted_thumb_path = ""

    # 1. Try yt-dlp metadata extraction for YouTube / Cloud links
    try:
        cmd = ["yt-dlp", "-J", "--no-warnings", target_url_or_path]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if res.returncode == 0 and res.stdout.strip():
            info = json.loads(res.stdout)
            extracted_title = info.get("title", "")
            extracted_desc  = info.get("description", "")
            extracted_tags  = info.get("tags", [])
            thumb_url       = info.get("thumbnail", "")

            if thumb_url:
                t_res = requests.get(thumb_url, timeout=10)
                if t_res.status_code == 200:
                    thumb_file = THUMB_DIR / f"auto_thumb_{uuid.uuid4().hex[:8]}.jpg"
                    with open(thumb_file, "wb") as f:
                        f.write(t_res.content)
                    extracted_thumb_path = str(thumb_file)
    except Exception as exc:
        logger.debug("yt-dlp auto metadata extraction failed: %s", exc)

    # 2. Clean Title & SEO generation fallback
    if not extracted_title:
        base_name = file_name or Path(target_url_or_path.split("?")[0].split("/")[-1]).stem
        clean_name = base_name.replace("_", " ").replace("-", " ")
        clean_name = re.sub(r"[^\w\s]", "", clean_name).strip()
        if not clean_name:
            clean_name = "24/7 Live Stream Video"
        extracted_title = f"🔴 {clean_name.title()} | 24/7 Non-Stop Live Stream (60FPS)"

    if not extracted_desc:
        extracted_desc = (
            f"Welcome to our official 24/7 live stream featuring {extracted_title}!\n\n"
            "🔔 Subscribe and turn on notifications to stay updated.\n\n"
            "#LiveStream #247Stream #YouTubeLive #60FPS #NonStop"
        )

    if not extracted_tags:
        words = [w.lower() for w in re.findall(r"\b\w{4,}\b", extracted_title) if w.lower() not in {"live","stream","non","stop","video"}]
        base_tags = ["live", "24/7", "live stream", "youtube live", "60fps", "non stop"]
        extracted_tags = list(dict.fromkeys(base_tags + words[:8]))

    tags_str = ", ".join(extracted_tags) if isinstance(extracted_tags, list) else str(extracted_tags)

    # 3. Automatic Thumbnail Frame Extraction via FFmpeg
    if not extracted_thumb_path:
        try:
            thumb_file = THUMB_DIR / f"auto_frame_{uuid.uuid4().hex[:8]}.jpg"
            ff_cmd = [
                "ffmpeg", "-y",
                "-user_agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "-ss", "00:00:05",
                "-i", target_url_or_path,
                "-vframes", "1",
                "-q:v", "2",
                str(thumb_file)
            ]
            ff_res = subprocess.run(ff_cmd, capture_output=True, timeout=12)
            if ff_res.returncode == 0 and thumb_file.exists():
                extracted_thumb_path = str(thumb_file)
        except Exception as exc:
            logger.debug("FFmpeg frame extraction failed: %s", exc)

    return {
        "title":          extracted_title,
        "description":    extracted_desc,
        "tags":           tags_str,
        "thumbnail_path": extracted_thumb_path,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# FFmpeg Command & Monitoring
# ═══════════════════════════════════════════════════════════════════════════════

def build_rtmp_url(platform: str, key: str) -> str:
    template = PLATFORM_RTMP.get(platform, PLATFORM_RTMP["youtube"])
    return template.format(key)


def build_ffmpeg_command(
    video_url: str,
    destinations: list[str],
    quality: str = "360p",
    fps: int = 30,
    audio_bitrate: str = "128k",
) -> list[str]:
    q = QUALITY_PRESETS.get(quality, QUALITY_PRESETS["360p"])
    gop = fps * 2

    if not destinations:
        destinations = [build_rtmp_url("youtube", "default")]

    if len(destinations) == 1:
        output_args = ["-f", "flv", destinations[0]]
    else:
        # Simultaneous multi-platform streaming via FFmpeg tee muxer
        tee_str = "|".join([f"[f=flv]{url}" for url in destinations])
        output_args = ["-f", "tee", tee_str]

    return [
        "ffmpeg",
        "-user_agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "-headers", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36\r\n",
        "-re",
        "-stream_loop", "-1",
        "-i", video_url,
        "-vf", f"scale={q['scale']}:force_original_aspect_ratio=decrease,pad={q['scale']}:(ow-iw)/2:(oh-ih)/2",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-tune", "zerolatency",
        "-subq", "1",
        "-me_method", "dia",
        "-sc_threshold", "0",
        "-b:v", q["vb"],
        "-maxrate", q["maxrate"],
        "-bufsize", q["bufsize"],
        "-pix_fmt", "yuv420p",
        "-r", str(fps),
        "-g", str(gop),
        "-c:a", "aac",
        "-b:a", audio_bitrate,
        "-ar", "44100",
        "-flvflags", "no_duration_filesize",
        "-max_delay", "500000",
        "-rtmp_buffer", "1000",
        *output_args,
    ]


def _read_ffmpeg_stderr(proc: subprocess.Popen) -> None:
    global ffmpeg_live_stats
    stat_pattern = re.compile(
        r"frame=\s*(?P<frame>\d+).*?fps=\s*(?P<fps>[\d.]+).*?"
        r"size=\s*(?P<size>[\d]+)kB.*?time=(?P<time>[\d:.]+).*?"
        r"bitrate=\s*(?P<bitrate>[\d.]+kbits/s).*?speed=\s*(?P<speed>[\d.]+x)"
    )
    try:
        for raw_line in proc.stderr:
            line = raw_line.decode("utf-8", errors="replace").strip()
            m = stat_pattern.search(line)
            if m:
                speed_str = m.group("speed").rstrip("x")
                try:
                    speed_val = float(speed_str)
                except ValueError:
                    speed_val = 1.0

                if speed_val >= 0.92:
                    health = "good"
                elif speed_val >= 0.70:
                    health = "warning"
                else:
                    health = "poor"

                ffmpeg_live_stats = {
                    "frame":   int(m.group("frame")),
                    "fps":     float(m.group("fps")),
                    "bitrate": m.group("bitrate"),
                    "speed":   m.group("speed"),
                    "size_kb": int(m.group("size")),
                    "time":    m.group("time"),
                    "health":  health,
                }
    except Exception as exc:
        logger.debug("Stderr reader error: %s", exc)
    finally:
        ffmpeg_live_stats["health"] = "offline"


def _monitor_process():
    with _state_lock:
        proc = stream_state.process

    if proc is None:
        return

    proc.wait()

    with _state_lock:
        if stream_state.process is not proc:
            return
        should_reconnect = stream_state.auto_reconnect and stream_state.is_active
        video_url    = stream_state.video_url
        stream_key   = stream_state.stream_key
        platform     = stream_state.platform
        quality      = stream_state.quality
        fps          = stream_state.fps
        audio_bitrate = stream_state.audio_bitrate

def _launch_ffmpeg(
    video_url: str,
    destinations: list[str],
    active_platforms: list[str],
    quality: str,
    fps: int,
    audio_bitrate: str,
) -> tuple[bool, str]:
    cmd = build_ffmpeg_command(video_url, destinations, quality, fps, audio_bitrate)
    logger.info("Launching Multi-Stream FFmpeg across %d platforms...", len(destinations))
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            preexec_fn=os.setsid if hasattr(os, "setsid") else None,
        )
    except FileNotFoundError:
        return False, "FFmpeg not found. Ensure it is installed."
    except Exception as exc:
        logger.exception("Failed to launch FFmpeg")
        return False, f"Error launching FFmpeg: {exc}"

    with _state_lock:
        stream_state.process          = proc
        stream_state.video_url        = video_url
        stream_state.destinations     = destinations
        stream_state.active_platforms = active_platforms
        stream_state.quality          = quality
        stream_state.fps              = fps
        stream_state.audio_bitrate    = audio_bitrate
        stream_state.started_at       = datetime.now(timezone.utc)
        stream_state.is_active        = True

    threading.Thread(target=_read_ffmpeg_stderr, args=(proc,), daemon=True).start()
    threading.Thread(target=_monitor_process, daemon=True).start()

    plat_names = ", ".join([p.upper() for p in active_platforms])
    logger.info("FFmpeg started (PID=%s) for platforms: %s", proc.pid, plat_names)
    return True, f"Multi-Stream LIVE on: {plat_names} (PID={proc.pid})."


def start_stream(
    video_url: str,
    destinations: list[str],
    active_platforms: list[str],
    quality: str = "360p",
    fps: int = 30,
    audio_bitrate: str = "128k",
    auto_reconnect: bool = True,
) -> tuple[bool, str]:
    with _state_lock:
        if stream_state.is_active and stream_state.process:
            return False, "A stream is already running. Stop it first."
        stream_state.auto_reconnect = auto_reconnect

    return _launch_ffmpeg(video_url, destinations, active_platforms, quality, fps, audio_bitrate)


def stop_stream() -> tuple[bool, str]:
    with _state_lock:
        if not stream_state.is_active or stream_state.process is None:
            return False, "No active stream to stop."
        proc = stream_state.process
        stream_state.auto_reconnect = False

    logger.info("Stopping FFmpeg (PID=%s)…", proc.pid)
    try:
        if hasattr(os, "killpg"):
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        else:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if hasattr(os, "killpg"):
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            else:
                proc.kill()
            proc.wait()
    except (ProcessLookupError, PermissionError):
        pass

    with _state_lock:
        uploaded = stream_state.uploaded_file
        stream_state.process      = None
        stream_state.is_active    = False
        stream_state.started_at   = None
        stream_state.video_url    = ""
        stream_state.stream_key   = ""
        stream_state.uploaded_file = None

    if uploaded and uploaded.exists():
        try:
            uploaded.unlink()
        except Exception:
            pass

    ffmpeg_live_stats["health"] = "offline"
    return True, "Stream stopped."


def get_uptime() -> str:
    with _state_lock:
        started = stream_state.started_at
    if started is None:
        return "N/A"
    delta = datetime.now(timezone.utc) - started
    total = int(delta.total_seconds())
    h, r  = divmod(total, 3600)
    m, s  = divmod(r, 60)
    return f"{h:02d}h {m:02d}m {s:02d}s"


def get_system_stats() -> dict:
    return {
        "cpu_percent":  psutil.cpu_percent(interval=0.1),
        "ram_percent":  psutil.virtual_memory().percent,
        "ram_used_mb":  round(psutil.virtual_memory().used  / (1024**2), 1),
        "ram_total_mb": round(psutil.virtual_memory().total / (1024**2), 1),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Flask Routes
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)


@app.route("/start", methods=["POST", "OPTIONS"])
def route_start():
    if request.method == "OPTIONS":
        return make_response("", 200)

    data          = request.get_json(silent=True) or request.form
    raw_url       = (data.get("url")          or "").strip()
    source_type   = (data.get("source_type")  or "direct").strip()
    quality       = (data.get("quality")      or "360p").strip()
    fps           = int(data.get("fps", 30))
    audio_bitrate = (data.get("audio_bitrate") or "128k").strip()
    auto_reconnect = str(data.get("auto_reconnect", "true")).lower() == "true"

    yt_key      = (data.get("yt_key")      or "").strip()
    fb_key      = (data.get("fb_key")      or "").strip()
    tw_key      = (data.get("tw_key")      or "").strip()
    custom_rtmp = (data.get("custom_rtmp") or "").strip()
    single_key  = (data.get("key")          or "").strip()
    single_plat = (data.get("platform")     or "youtube").strip()

    destinations = []
    active_platforms = []

    if yt_key:
        destinations.append(build_rtmp_url("youtube", yt_key))
        active_platforms.append("youtube")
    if fb_key:
        destinations.append(build_rtmp_url("facebook", fb_key))
        active_platforms.append("facebook")
    if tw_key:
        destinations.append(build_rtmp_url("twitch", tw_key))
        active_platforms.append("twitch")
    if custom_rtmp:
        destinations.append(custom_rtmp)
        active_platforms.append("custom")

    if not destinations and single_key:
        destinations.append(build_rtmp_url(single_plat, single_key))
        active_platforms.append(single_plat)

    if not raw_url:
        return jsonify({"success": False, "message": "Video URL/path is required."}), 400
    if not destinations:
        return jsonify({"success": False, "message": "At least one stream key is required."}), 400

    if quality not in QUALITY_PRESETS:
        quality = "360p"

    if source_type != "upload":
        video_url, err = resolve_video_url(raw_url, source_type)
        if err:
            return jsonify({"success": False, "message": err}), 400
    else:
        video_url = raw_url

    success, message = start_stream(
        video_url, destinations, active_platforms, quality, fps, audio_bitrate, auto_reconnect
    )
    return jsonify({"success": success, "message": message}), 200 if success else 409


@app.route("/stop", methods=["POST", "OPTIONS"])
def route_stop():
    if request.method == "OPTIONS":
        return make_response("", 200)
    success, message = stop_stream()
    return jsonify({"success": success, "message": message})


@app.route("/api/status", methods=["GET", "OPTIONS"])
def route_status():
    if request.method == "OPTIONS":
        return make_response("", 200)

    with _state_lock:
        active   = stream_state.is_active
        pid      = stream_state.process.pid if stream_state.process else None
        url      = stream_state.video_url
        platform = stream_state.platform
        quality  = stream_state.quality
        fps      = stream_state.fps
        auto_r   = stream_state.auto_reconnect
        key_raw  = stream_state.stream_key
        key_m    = ("*" * 6 + key_raw[-4:]) if len(key_raw) > 4 else "****"

        attached_name   = stream_state.attached_source_name
        attached_type   = stream_state.attached_source_type
        attached_size   = stream_state.attached_size_mb
        attached_status = stream_state.attached_status

        title     = stream_state.title
        desc      = stream_state.description
        tags      = stream_state.tags
        thumb_url = f"/thumbnail/{Path(stream_state.thumbnail_path).name}" if stream_state.thumbnail_path else None

    stats = get_system_stats()
    return jsonify({
        "is_active":         active,
        "pid":               pid,
        "uptime":            get_uptime(),
        "video_url":         url if active else "",
        "platform":          platform,
        "quality":           quality,
        "fps":               fps,
        "auto_reconnect":    auto_r,
        "stream_key_masked": key_m if active else "",
        "attached_source": {
            "name": attached_name,
            "type": attached_type,
            "size_mb": attached_size,
            "status": attached_status,
        },
        "seo": {
            "title": title,
            "description": desc,
            "tags": tags,
            "thumbnail_url": thumb_url,
        },
        "live_stats":        ffmpeg_live_stats,
        **stats,
    })


@app.route("/upload", methods=["POST", "OPTIONS"])
def route_upload():
    if request.method == "OPTIONS":
        return make_response("", 200)

    if "file" not in request.files:
        return jsonify({"success": False, "message": "No file part."}), 400

    f = request.files["file"]
    if f.filename == "":
        return jsonify({"success": False, "message": "No file selected."}), 400

    f.seek(0, 2)
    size = f.tell()
    f.seek(0)
    if size > MAX_UPLOAD_BYTES:
        return jsonify({"success": False, "message": "File exceeds 2 GB limit."}), 413

    ext = Path(f.filename).suffix.lower()
    allowed = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".webm", ".m4v", ".ts"}
    if ext not in allowed:
        return jsonify({"success": False, "message": f"Format {ext} not supported."}), 400

    unique_name = f"{uuid.uuid4().hex}{ext}"
    save_path   = UPLOAD_DIR / unique_name
    f.save(save_path)

    size_mb = round(size / (1024**2), 1)

    # Auto-extract SEO Title, Description, Tags, and Thumbnail Frame from uploaded file
    seo_meta = auto_extract_video_metadata(str(save_path), "upload", f.filename)

    with _state_lock:
        stream_state.uploaded_file = save_path
        stream_state.attached_source_name = f.filename
        stream_state.attached_source_type = "PC Local Upload"
        stream_state.attached_size_mb = size_mb
        stream_state.attached_status = "verified"

        stream_state.title = seo_meta["title"]
        stream_state.description = seo_meta["description"]
        stream_state.tags = seo_meta["tags"]
        if seo_meta["thumbnail_path"]:
            stream_state.thumbnail_path = seo_meta["thumbnail_path"]

    thumb_url = f"/thumbnail/{Path(seo_meta['thumbnail_path']).name}" if seo_meta["thumbnail_path"] else None

    return jsonify({
        "success":       True,
        "message":       f"File uploaded & SEO auto-generated: {f.filename}",
        "file_path":     str(save_path),
        "file_name":     f.filename,
        "size_mb":       size_mb,
        "preview_url":   f"/uploaded-video/{unique_name}",
        "seo": {
            "title":          seo_meta["title"],
            "description":    seo_meta["description"],
            "tags":           seo_meta["tags"],
            "thumbnail_url":  thumb_url,
        }
    })


@app.route("/uploaded-video/<filename>")
def route_get_uploaded_video(filename):
    from flask import send_from_directory
    return send_from_directory(UPLOAD_DIR, filename)


@app.route("/upload-thumbnail", methods=["POST", "OPTIONS"])
def route_upload_thumbnail():
    if request.method == "OPTIONS":
        return make_response("", 200)

    if "file" not in request.files:
        return jsonify({"success": False, "message": "No image file provided."}), 400

    f = request.files["file"]
    if f.filename == "":
        return jsonify({"success": False, "message": "No file selected."}), 400

    ext = Path(f.filename).suffix.lower()
    if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
        return jsonify({"success": False, "message": "Thumbnail must be JPG, PNG, or WEBP."}), 400

    unique_name = f"thumb_{uuid.uuid4().hex}{ext}"
    save_path   = THUMB_DIR / unique_name
    f.save(save_path)

    with _state_lock:
        stream_state.thumbnail_path = str(save_path)

    return jsonify({
        "success": True,
        "message": "Thumbnail uploaded successfully!",
        "thumbnail_url": f"/thumbnail/{unique_name}",
    })


@app.route("/thumbnail/<filename>")
def route_get_thumbnail(filename):
    from flask import send_from_directory
    return send_from_directory(THUMB_DIR, filename)


@app.route("/save-seo", methods=["POST", "OPTIONS"])
def route_save_seo():
    if request.method == "OPTIONS":
        return make_response("", 200)

    data = request.get_json(silent=True) or request.form
    title = (data.get("title") or "").strip()
    desc  = (data.get("description") or "").strip()
    tags  = (data.get("tags") or "").strip()

    with _state_lock:
        if title: stream_state.title = title
        if desc:  stream_state.description = desc
        if tags:  stream_state.tags = tags

    return jsonify({
        "success": True,
        "message": "SEO metadata updated!",
        "title": stream_state.title,
        "description": stream_state.description,
        "tags": stream_state.tags,
    })


@app.route("/resolve-url", methods=["POST", "OPTIONS"])
def route_resolve_url():
    if request.method == "OPTIONS":
        return make_response("", 200)

    data        = request.get_json(silent=True) or {}
    raw_url     = (data.get("url")         or "").strip()
    source_type = (data.get("source_type") or "").strip()

    if not raw_url:
        return jsonify({"success": False, "message": "URL required."}), 400

    resolved, err = resolve_video_url(raw_url, source_type)
    if err:
        with _state_lock:
            stream_state.attached_status = "failed"
        return jsonify({"success": False, "message": err}), 400

    labels = {
        "gdrive": "Google Drive Video",
        "dropbox": "Dropbox Cloud Video",
        "youtube": "YouTube Video Stream",
        "direct": "Direct CDN Link",
    }
    st_label = labels.get(source_type, "Cloud Video URL")

    # Auto-extract SEO Title, Description, Tags, and Thumbnail Frame
    seo_meta = auto_extract_video_metadata(resolved, source_type, raw_url[:30])

    with _state_lock:
        stream_state.attached_source_name = raw_url[:40] + "..." if len(raw_url) > 40 else raw_url
        stream_state.attached_source_type = st_label
        stream_state.attached_size_mb = 0.0
        stream_state.attached_status = "verified"

        stream_state.title = seo_meta["title"]
        stream_state.description = seo_meta["description"]
        stream_state.tags = seo_meta["tags"]
        if seo_meta["thumbnail_path"]:
            stream_state.thumbnail_path = seo_meta["thumbnail_path"]

    thumb_url = f"/thumbnail/{Path(seo_meta['thumbnail_path']).name}" if seo_meta["thumbnail_path"] else None

    return jsonify({
        "success": True,
        "resolved_url": resolved,
        "source_label": st_label,
        "seo": {
            "title":          seo_meta["title"],
            "description":    seo_meta["description"],
            "tags":           seo_meta["tags"],
            "thumbnail_url":  thumb_url,
        }
    })


# ═══════════════════════════════════════════════════════════════════════════════
# Dashboard HTML — Ultimate Dark UI with Live Upload Tracker & Player
# ═══════════════════════════════════════════════════════════════════════════════

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en" class="dark">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>YouTube 24/7 Live Streamer — Pro Dashboard</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <link rel="preconnect" href="https://fonts.googleapis.com"/>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&display=swap" rel="stylesheet"/>
  <script>
    tailwind.config = {
      darkMode: 'class',
      theme: { extend: { fontFamily: { sans: ['Inter','sans-serif'] } } }
    };
  </script>
  <style>
    :root{--bg:#060b18;--card:rgba(255,255,255,0.04);--border:rgba(255,255,255,0.08);--text:#e2e8f0}
    *{box-sizing:border-box}
    body{background:linear-gradient(135deg,#060b18 0%,#0b1630 50%,#07121f 100%);min-height:100vh;color:var(--text)}
    .glass{background:var(--card);backdrop-filter:blur(20px);border:1px solid var(--border)}
    .glass-strong{background:rgba(255,255,255,0.07);backdrop-filter:blur(24px);border:1px solid rgba(255,255,255,0.14)}
    .glow-blue{box-shadow:0 0 40px rgba(56,189,248,0.12),0 0 80px rgba(56,189,248,0.04)}

    .health-good{background:rgba(34,197,94,0.15);border:1px solid rgba(34,197,94,0.4);color:#4ade80}
    .health-warning{background:rgba(234,179,8,0.15);border:1px solid rgba(234,179,8,0.4);color:#facc15}
    .health-poor{background:rgba(239,68,68,0.15);border:1px solid rgba(239,68,68,0.4);color:#f87171}
    .health-offline{background:rgba(100,116,139,0.15);border:1px solid rgba(100,116,139,0.4);color:#94a3b8}

    .dot-good{width:12px;height:12px;border-radius:50%;background:#22c55e;box-shadow:0 0 12px #22c55e;animation:pulse 1.5s infinite}
    .dot-warning{width:12px;height:12px;border-radius:50%;background:#eab308;box-shadow:0 0 12px #eab308;animation:pulse 1.5s infinite}
    .dot-poor{width:12px;height:12px;border-radius:50%;background:#ef4444;box-shadow:0 0 12px #ef4444;animation:pulse 1s infinite}
    .dot-offline{width:12px;height:12px;border-radius:50%;background:#64748b}

    .src-tab{padding:8px 16px;border-radius:10px;font-size:.8rem;font-weight:600;cursor:pointer;transition:all .2s;color:#64748b;border:1px solid transparent;display:flex;align-items:center;gap:6px;white-space:nowrap}
    .src-tab:hover{color:#94a3b8;background:rgba(255,255,255,0.04)}
    .src-tab.active{background:linear-gradient(135deg,rgba(56,189,248,0.15),rgba(139,92,246,0.15));border-color:rgba(56,189,248,0.3);color:#38bdf8}

    .plat-btn{padding:10px 16px;border-radius:12px;font-size:.8rem;font-weight:700;cursor:pointer;transition:all .2s;border:1px solid var(--border);color:#64748b;display:flex;align-items:center;gap:8px;flex:1;justify-content:center}
    .plat-btn.active{border-color:rgba(56,189,248,0.5);color:#38bdf8;background:rgba(56,189,248,0.08)}
    
    .q-card{padding:10px 14px;border-radius:12px;cursor:pointer;transition:all .2s;border:1px solid var(--border);text-align:center;flex:1}
    .q-card.active{border-color:rgba(139,92,246,0.5);background:rgba(139,92,246,0.1)}
    .q-card .label{font-size:.75rem;font-weight:700;color:#a78bfa}
    .q-card .sub{font-size:.65rem;color:#475569;margin-top:2px}

    .inp{width:100%;background:rgba(255,255,255,0.04);border:1px solid var(--border);color:var(--text);border-radius:12px;padding:12px 16px;font-size:.875rem;transition:all .2s;outline:none}
    .inp:focus{border-color:rgba(56,189,248,0.5);box-shadow:0 0 0 3px rgba(56,189,248,0.1);background:rgba(255,255,255,0.06)}

    .btn-start{background:linear-gradient(135deg,#059669,#047857);border:none;color:#fff;padding:14px 24px;border-radius:14px;font-weight:700;font-size:.9rem;cursor:pointer;transition:all .2s;display:flex;align-items:center;justify-content:center;gap:8px;width:100%}
    .btn-start:hover:not(:disabled){background:linear-gradient(135deg,#10b981,#059669);transform:translateY(-2px);box-shadow:0 12px 30px rgba(5,150,105,.4)}
    .btn-stop{background:linear-gradient(135deg,#dc2626,#b91c1c);border:none;color:#fff;padding:14px 24px;border-radius:14px;font-weight:700;font-size:.9rem;cursor:pointer;transition:all .2s;display:flex;align-items:center;justify-content:center;gap:8px;width:100%}
    .btn-stop:hover:not(:disabled){background:linear-gradient(135deg,#ef4444,#dc2626);transform:translateY(-2px);box-shadow:0 12px 30px rgba(220,38,38,.4)}
    button:disabled{opacity:.45;cursor:not-allowed;transform:none!important}

    .pbar{height:8px;border-radius:4px;background:rgba(255,255,255,0.08);overflow:hidden}
    .pbar-fill{height:100%;border-radius:4px;transition:width .3s ease}

    .toggle{position:relative;width:44px;height:24px;flex-shrink:0}
    .toggle input{opacity:0;width:0;height:0}
    .toggle-slider{position:absolute;cursor:pointer;inset:0;background:rgba(255,255,255,0.1);border-radius:24px;transition:.3s;border:1px solid var(--border)}
    .toggle-slider:before{content:"";position:absolute;width:18px;height:18px;left:2px;top:2px;background:#64748b;border-radius:50%;transition:.3s}
    input:checked + .toggle-slider{background:rgba(56,189,248,0.2);border-color:rgba(56,189,248,0.4)}
    input:checked + .toggle-slider:before{transform:translateX(20px);background:#38bdf8}

    .upload-zone{border:2px dashed rgba(56,189,248,0.25);border-radius:16px;padding:24px 20px;text-align:center;cursor:pointer;transition:all .3s;background:rgba(56,189,248,0.02)}
    .upload-zone:hover{border-color:rgba(56,189,248,0.5);background:rgba(56,189,248,0.06)}

    .toast{position:fixed;bottom:24px;right:24px;z-index:100;min-width:320px;animation:slideUp .3s ease}
    @keyframes slideUp{from{opacity:0;transform:translateY(16px)}to{opacity:1;transform:translateY(0)}}
    .sec-label{font-size:.72rem;font-weight:800;letter-spacing:.12em;text-transform:uppercase;color:#475569;margin-bottom:12px}
  </style>
</head>
<body class="font-sans antialiased p-4 lg:p-8">

<div class="max-w-6xl mx-auto space-y-6">

  <!-- Header -->
  <header class="text-center mb-6">
    <div class="inline-flex items-center gap-4">
      <span class="text-5xl">🎥</span>
      <div class="text-left">
        <h1 class="text-3xl lg:text-4xl font-black bg-gradient-to-r from-sky-400 via-blue-400 to-violet-500 bg-clip-text text-transparent">24/7 Live Streamer Pro</h1>
        <p class="text-slate-500 text-xs font-semibold">Real-Time Upload Progress Tracker & Live Stream Health Monitor</p>
      </div>
    </div>
  </header>

  <!-- LIVE HEALTH MONITOR PANEL -->
  <div class="glass rounded-2xl p-5 glow-blue">
    <div class="flex flex-wrap items-center justify-between gap-4 mb-4">
      <div>
        <div class="sec-label">📡 Real-Time Stream & Health Monitor</div>
        <div class="flex items-center gap-3">
          <span id="status-dot" class="dot-offline"></span>
          <span id="status-text" class="text-2xl font-black text-slate-400">Offline</span>
          <span id="health-badge" class="px-3 py-1 rounded-full text-xs font-bold health-offline">STATIONARY</span>
        </div>
      </div>
      <div class="text-right">
        <p id="uptime-text" class="text-slate-400 text-sm font-bold">Uptime: N/A</p>
        <p id="pid-text" class="text-slate-600 text-xs font-mono"></p>
      </div>
    </div>

    <!-- Live FFmpeg Real-Time Metrics -->
    <div class="grid grid-cols-2 md:grid-cols-4 gap-3">
      <div class="glass rounded-xl p-3 text-center">
        <p class="text-slate-500 text-xs font-bold">ENCODING FPS</p>
        <p id="stat-fps" class="text-2xl font-black text-sky-400 mt-1">0.0</p>
      </div>
      <div class="glass rounded-xl p-3 text-center">
        <p class="text-slate-500 text-xs font-bold">BITRATE</p>
        <p id="stat-bitrate" class="text-2xl font-black text-emerald-400 mt-1">0 kbits/s</p>
      </div>
      <div class="glass rounded-xl p-3 text-center">
        <p class="text-slate-500 text-xs font-bold">STREAM SPEED</p>
        <p id="stat-speed" class="text-2xl font-black text-violet-400 mt-1">0.0x</p>
      </div>
      <div class="glass rounded-xl p-3 text-center">
        <p class="text-slate-500 text-xs font-bold">TOTAL FRAMES</p>
        <p id="stat-frames" class="text-2xl font-black text-amber-400 mt-1">0</p>
      </div>
    </div>

    <!-- CPU & RAM -->
    <div class="grid grid-cols-2 gap-4 mt-4">
      <div>
        <div class="flex justify-between text-xs font-semibold mb-1">
          <span class="text-slate-400">⚡ Server CPU</span>
          <span id="cpu-pct" class="text-sky-400">0%</span>
        </div>
        <div class="pbar"><div id="cpu-bar" class="pbar-fill bg-sky-400" style="width:0%"></div></div>
      </div>
      <div>
        <div class="flex justify-between text-xs font-semibold mb-1">
          <span class="text-slate-400">🧠 Server RAM</span>
          <span id="ram-pct" class="text-violet-400">0%</span>
        </div>
        <div class="pbar"><div id="ram-bar" class="pbar-fill bg-violet-400" style="width:0%"></div></div>
      </div>
    </div>
  </div>

  <!-- MAIN TWO-COLUMN LAYOUT -->
  <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">

    <!-- LEFT COLUMN: CONTROLS & SOURCES -->
    <div class="lg:col-span-2 space-y-6">

      <!-- LIVE VIDEO PREVIEW & PLAYER WINDOW -->
      <div id="video-preview-card" class="glass rounded-2xl p-5">
        <div class="sec-label flex justify-between items-center">
          <span>📺 Live Video Preview & Player</span>
          <span id="preview-badge" class="px-2.5 py-1 rounded-full text-[10px] font-bold bg-slate-800 text-slate-400">NO VIDEO LOADED</span>
        </div>
        <div class="relative w-full aspect-video rounded-xl bg-black border border-slate-800 flex items-center justify-center overflow-hidden">
          <video id="video-preview-player" class="w-full h-full object-contain hidden" controls autoplay loop muted playsinline></video>
          <div id="video-preview-placeholder" class="text-center p-6">
            <span class="text-4xl block mb-2">🎬</span>
            <p class="text-slate-400 text-xs font-bold">Video Preview Window</p>
            <p class="text-slate-600 text-[11px] mt-1">Upload a video or attach a cloud link to preview live</p>
          </div>
        </div>
      </div>

      <!-- VIDEO SOURCE SELECTION -->
      <div class="glass rounded-2xl p-5">
        <div class="sec-label">📹 Video Source & Live Upload Progress</div>
        <div class="flex flex-wrap gap-2 mb-4 overflow-x-auto pb-1">
          <button class="src-tab active" onclick="switchTab('direct')"   id="tab-direct">   🔗 Direct URL</button>
          <button class="src-tab"        onclick="switchTab('upload')"   id="tab-upload">   📁 Upload PC</button>
          <button class="src-tab"        onclick="switchTab('gdrive')"   id="tab-gdrive">   ☁️ Google Drive</button>
          <button class="src-tab"        onclick="switchTab('dropbox')"  id="tab-dropbox">  📦 Dropbox</button>
          <button class="src-tab"        onclick="switchTab('youtube')"  id="tab-youtube">  🎬 YouTube</button>
        </div>

        <div id="panel-direct">
          <label class="block text-slate-400 text-xs font-semibold mb-2">Direct Video File / Stream URL</label>
          <div class="flex gap-2">
            <input id="url-direct" class="inp" type="url" placeholder="https://cdn.example.com/video.mp4"/>
            <button onclick="verifyCloudSource('direct')" class="px-4 py-2 glass rounded-xl text-sky-400 font-bold text-xs">Verify →</button>
          </div>
        </div>

        <div id="panel-upload" style="display:none">
          <div class="upload-zone" onclick="document.getElementById('file-input').click()">
            <div class="text-3xl mb-1">📁</div>
            <p class="text-slate-300 font-bold text-sm">Click to select MP4/MKV video file from PC</p>
            <p class="text-slate-500 text-xs mt-1">Supports up to 2 GB video files</p>
          </div>

          <!-- Real-Time Upload Progress Widget -->
          <div id="upload-progress-box" class="mt-4 glass rounded-xl p-4 hidden">
            <div class="flex justify-between items-center text-xs font-bold mb-2">
              <span id="upload-file-name" class="text-sky-400 truncate max-w-[200px]">uploading.mp4</span>
              <span id="upload-pct" class="text-amber-400 text-sm font-black">0%</span>
            </div>
            <div class="pbar mb-2"><div id="upload-bar" class="pbar-fill bg-amber-400" style="width:0%"></div></div>
            <div class="flex justify-between text-xs text-slate-500 font-mono">
              <span id="upload-mb">0 MB / 0 MB</span>
              <span id="upload-speed">0 MB/s</span>
            </div>
            <div id="upload-complete-banner" class="mt-3 p-2.5 bg-emerald-500/20 border border-emerald-500/40 rounded-lg text-emerald-400 text-xs font-bold text-center hidden">
              🎉 UPLOAD COMPLETE & VERIFIED! Video is 100% uploaded and ready to stream.
            </div>
          </div>

          <input id="file-input" type="file" accept="video/*,.mkv,.flv,.ts" class="hidden" onchange="handleFileSelect(this)"/>
          <input id="url-upload" type="hidden"/>
        </div>

        <div id="panel-gdrive" style="display:none">
          <label class="block text-slate-400 text-xs font-semibold mb-2">Google Drive Shareable Link</label>
          <div class="flex gap-2">
            <input id="url-gdrive" class="inp" type="url" placeholder="https://drive.google.com/file/d/FILE_ID/view"/>
            <button onclick="resolveURL('gdrive')" class="px-4 py-2 glass rounded-xl text-sky-400 font-bold text-xs">Verify & Attach →</button>
          </div>
        </div>

        <div id="panel-dropbox" style="display:none">
          <label class="block text-slate-400 text-xs font-semibold mb-2">Dropbox Link</label>
          <div class="flex gap-2">
            <input id="url-dropbox" class="inp" type="url" placeholder="https://www.dropbox.com/s/xxx/video.mp4?dl=0"/>
            <button onclick="resolveURL('dropbox')" class="px-4 py-2 glass rounded-xl text-sky-400 font-bold text-xs">Verify & Attach →</button>
          </div>
        </div>

        <div id="panel-youtube" style="display:none">
          <label class="block text-slate-400 text-xs font-semibold mb-2">YouTube Video URL</label>
          <div class="flex gap-2">
            <input id="url-youtube" class="inp" type="url" placeholder="https://www.youtube.com/watch?v=VIDEO_ID"/>
            <button onclick="resolveURL('youtube')" class="px-4 py-2 glass rounded-xl text-sky-400 font-bold text-xs">Extract Stream →</button>
          </div>
        </div>

        <!-- ATTACHED VIDEO MEDIA BADGE -->
        <div id="attached-media-card" class="mt-4 glass rounded-xl p-3 border border-sky-500/30 flex items-center justify-between">
          <div class="flex items-center gap-3">
            <span class="text-2xl">📌</span>
            <div>
              <p class="text-xs font-bold text-slate-400">ACTIVE VIDEO SOURCE</p>
              <p id="attached-name" class="text-sm font-black text-sky-300">No video attached yet</p>
              <p id="attached-type" class="text-xs text-slate-500">Select a source above to attach</p>
            </div>
          </div>
          <div>
            <span id="attached-badge" class="px-3 py-1 rounded-full text-xs font-bold bg-slate-800 text-slate-400 border border-slate-700">NOT VERIFIED</span>
          </div>
        </div>

      </div>

      <!-- STREAM CONFIG & MULTI-PLATFORM SETUP -->
      <div class="glass rounded-2xl p-5">
        <div class="sec-label">🎛️ Multi-Platform Streaming Setup</div>

        <!-- Mode Toggle -->
        <div class="flex items-center justify-between glass rounded-xl p-3 mb-4">
          <div>
            <p class="text-slate-200 text-xs font-bold">✨ Multi-Platform Restream Mode</p>
            <p class="text-slate-500 text-xs">Stream simultaneously to YouTube, Facebook, Twitch & Custom RTMP</p>
          </div>
          <label class="toggle">
            <input type="checkbox" id="multi-stream-toggle" onchange="toggleMultiStream(this)"/>
            <span class="toggle-slider"></span>
          </label>
        </div>

        <!-- Single Platform Selector -->
        <div id="single-platform-wrap" class="mb-4">
          <label class="block text-slate-400 text-xs font-semibold mb-2">Target Platform</label>
          <div class="flex flex-wrap gap-2">
            <button class="plat-btn active" onclick="setPlatform('youtube')"  id="plat-youtube">  📺 YouTube</button>
            <button class="plat-btn"        onclick="setPlatform('facebook')" id="plat-facebook"> 📘 Facebook</button>
            <button class="plat-btn"        onclick="setPlatform('twitch')"   id="plat-twitch">   🟣 Twitch</button>
            <button class="plat-btn"        onclick="setPlatform('custom')"   id="plat-custom">   🔧 Custom RTMP</button>
          </div>
          <div class="mt-3">
            <label class="block text-slate-400 text-xs font-semibold mb-1">🔑 Stream Key</label>
            <input id="stream-key" class="inp font-mono" type="password" placeholder="xxxx-xxxx-xxxx-xxxx"/>
          </div>
        </div>

        <!-- Multi-Platform Stream Keys -->
        <div id="multi-platform-wrap" class="space-y-3 mb-4 hidden">
          <div>
            <label class="block text-slate-300 text-xs font-bold mb-1">📺 YouTube Live Stream Key</label>
            <input id="yt-key" class="inp font-mono text-xs" type="password" placeholder="xxxx-xxxx-xxxx-xxxx"/>
          </div>
          <div>
            <label class="block text-slate-300 text-xs font-bold mb-1">📘 Facebook Live Stream Key</label>
            <input id="fb-key" class="inp font-mono text-xs" type="password" placeholder="FB-12345-xxxx-xxxx"/>
          </div>
          <div>
            <label class="block text-slate-300 text-xs font-bold mb-1">🟣 Twitch Stream Key</label>
            <input id="tw-key" class="inp font-mono text-xs" type="password" placeholder="live_xxxx_xxxx"/>
          </div>
          <div>
            <label class="block text-slate-300 text-xs font-bold mb-1">🔧 Custom RTMP Server URL</label>
            <input id="custom-rtmp" class="inp font-mono text-xs" type="text" placeholder="rtmp://server.com/live/YOUR_KEY"/>
          </div>
        </div>

        <!-- Quality Preset -->
        <div class="mb-4">
          <label class="block text-slate-400 text-xs font-semibold mb-2">Quality Preset (Choose 360p or 480p for Zero Lag)</label>
          <div class="grid grid-cols-2 md:grid-cols-6 gap-2">
            <div class="q-card active" onclick="setQuality('360p')"     id="q-360p">     <div class="label">360p ⚡</div>  <div class="sub">500 Kbps · Zero Lag</div></div>
            <div class="q-card"        onclick="setQuality('480p')"     id="q-480p">     <div class="label">480p ⭐</div>  <div class="sub">900 Kbps · Smooth</div></div>
            <div class="q-card"        onclick="setQuality('720p')"     id="q-720p">     <div class="label">720p</div>    <div class="sub">1.8 Mbps</div></div>
            <div class="q-card"        onclick="setQuality('1080p')"   id="q-1080p">   <div class="label">1080p</div>   <div class="sub">3.5 Mbps</div></div>
            <div class="q-card"        onclick="setQuality('1080p_hq')" id="q-1080p_hq"> <div class="label">1080p HQ</div><div class="sub">5 Mbps</div></div>
            <div class="q-card"        onclick="setQuality('4k')"       id="q-4k">       <div class="label">4K Ultra</div><div class="sub">7.5 Mbps</div></div>
          </div>
        </div>

        <!-- Auto Reconnect -->
        <div class="flex items-center justify-between glass rounded-xl p-3">
          <div>
            <p class="text-slate-200 text-xs font-bold">🔄 Auto-Reconnect 24/7</p>
            <p class="text-slate-500 text-xs">Automatically restarts stream if connection drops</p>
          </div>
          <label class="toggle">
            <input type="checkbox" id="auto-reconnect" checked/>
            <span class="toggle-slider"></span>
          </label>
        </div>
      </div>

      <!-- STREAM ACTIONS -->
      <div class="grid grid-cols-2 gap-4">
        <button id="btn-start" onclick="startStream()" class="btn-start">▶️ Start 24/7 Stream</button>
        <button id="btn-stop"  onclick="stopStream()"  class="btn-stop">⏹️ Stop Stream</button>
      </div>

    </div>

    <!-- RIGHT COLUMN: YOUTUBE LIVE SEO & THUMBNAIL SUITE -->
    <div class="space-y-6">

      <!-- THUMBNAIL UPLOADER & PREVIEW -->
      <div class="glass rounded-2xl p-5">
        <div class="sec-label">🖼️ Stream Thumbnail Manager</div>
        <div class="text-center mb-3">
          <div id="thumb-preview-box" class="w-full h-36 rounded-xl border border-slate-700 bg-slate-900/60 flex items-center justify-center overflow-hidden">
            <span id="thumb-placeholder" class="text-slate-600 text-xs">No Thumbnail Uploaded</span>
            <img id="thumb-img" class="w-full h-full object-cover hidden" alt="Thumbnail Preview"/>
          </div>
        </div>
        <button onclick="document.getElementById('thumb-input').click()" class="w-full py-2 glass rounded-xl text-sky-400 font-bold text-xs hover:bg-white/5">
          📤 Upload Custom Stream Thumbnail (JPG/PNG)
        </button>
        <input id="thumb-input" type="file" accept="image/*" class="hidden" onchange="uploadThumbnail(this)"/>
      </div>

      <!-- YOUTUBE LIVE SEO & METADATA SUITE -->
      <div class="glass rounded-2xl p-5">
        <div class="sec-label">🚀 YouTube Live SEO Suite</div>

        <!-- SEO AI Auto-Generator -->
        <div class="mb-4">
          <button onclick="generateSEO()" class="w-full py-2 bg-gradient-to-r from-violet-600 to-indigo-600 text-white font-bold rounded-xl text-xs hover:opacity-90 transition-all">
            ✨ Auto-Generate High-Ranking SEO Metadata
          </button>
        </div>

        <!-- Stream Title -->
        <div class="mb-3">
          <label class="block text-slate-400 text-xs font-bold mb-1">Live Stream Title</label>
          <input id="seo-title" class="inp text-xs" type="text" placeholder="24/7 Non-Stop Live Stream 🔴"/>
        </div>

        <!-- Stream Description -->
        <div class="mb-3">
          <label class="block text-slate-400 text-xs font-bold mb-1">Description & Socials</label>
          <textarea id="seo-desc" class="inp text-xs h-24" placeholder="Welcome to our continuous 24/7 stream..."></textarea>
        </div>

        <!-- Tags -->
        <div class="mb-4">
          <label class="block text-slate-400 text-xs font-bold mb-1">SEO Tags (comma separated)</label>
          <input id="seo-tags" class="inp text-xs font-mono" type="text" placeholder="live, 24/7, streaming, gaming, music"/>
        </div>

        <!-- Action Buttons -->
        <div class="space-y-2">
          <button onclick="saveSEO()" class="w-full py-2.5 bg-sky-500 text-white font-bold rounded-xl text-xs hover:bg-sky-400">
            💾 Save SEO Settings
          </button>
          <button onclick="openYouTubeStudio()" class="w-full py-2 glass text-amber-400 font-bold rounded-xl text-xs hover:bg-white/5">
            🎬 Open YouTube Live Studio →
          </button>
        </div>
      </div>

    </div>

  </div>
</div>

<!-- Toast -->
<div id="toast" class="toast hidden">
  <div id="toast-inner" class="glass-strong rounded-2xl px-5 py-3 text-xs font-bold"></div>
</div>

<script>
let activeTab      = 'direct';
let activePlatform = 'youtube';
let activeQuality  = '360p';
let resolvedURLs   = {};

function switchTab(tab) {
  ['direct','upload','gdrive','dropbox','youtube'].forEach(t => {
    document.getElementById('panel-' + t).style.display = t === tab ? '' : 'none';
    document.getElementById('tab-' + t).classList.toggle('active', t === tab);
  });
  activeTab = tab;
}

function setPlatform(p) {
  ['youtube','facebook','twitch','custom'].forEach(x => {
    document.getElementById('plat-' + x).classList.toggle('active', x === p);
  });
  activePlatform = p;
  document.getElementById('custom-rtmp-wrap').classList.toggle('hidden', p !== 'custom');
}

function setQuality(q) {
  ['360p','480p','720p','1080p','1080p_hq','4k'].forEach(x => {
    document.getElementById('q-' + x).classList.toggle('active', x === q);
  });
  activeQuality = q;
}

async function verifyCloudSource(sourceType) {
  const url = document.getElementById('url-' + sourceType).value.trim();
  if (!url) return showToast('Please enter a URL first.', 'error');
  resolveURL(sourceType);
}

async function resolveURL(sourceType) {
  const url = document.getElementById('url-' + sourceType).value.trim();
  if (!url) return showToast('Please enter a URL.', 'error');

  showToast('⏳ Verifying cloud link & attaching video...', 'info');

  try {
    const res  = await fetch('/resolve-url', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ url, source_type: sourceType }),
    });
    const data = await res.json();
    if (data.success) {
      resolvedURLs[sourceType] = data.resolved_url;

      // Load preview player for direct URL
      if (sourceType === 'direct' || sourceType === 'gdrive' || sourceType === 'dropbox') {
        const player = document.getElementById('video-preview-player');
        const placeholder = document.getElementById('video-preview-placeholder');
        const badge = document.getElementById('preview-badge');
        player.src = data.resolved_url;
        player.classList.remove('hidden');
        placeholder.classList.add('hidden');
        badge.className = 'px-2.5 py-1 rounded-full text-[10px] font-bold bg-emerald-500/20 text-emerald-400 border border-emerald-500/30';
        badge.textContent = '🟢 PREVIEW READY (' + data.source_label + ')';
      }

      showToast('✅ Cloud video verified & attached!', 'success');
      pollStatus();
    } else {
      showToast(data.message, 'error');
    }
  } catch(e) {
    showToast('Network error while verifying URL.', 'error');
  }
}

function handleFileSelect(input) {
  if (!input.files[0]) return;
  const file = input.files[0];

  const box        = document.getElementById('upload-progress-box');
  const nameEl     = document.getElementById('upload-file-name');
  const pctEl      = document.getElementById('upload-pct');
  const barEl      = document.getElementById('upload-bar');
  const mbEl       = document.getElementById('upload-mb');
  const spdEl      = document.getElementById('upload-speed');
  const banner     = document.getElementById('upload-complete-banner');
  const startBtn   = document.getElementById('btn-start');

  box.classList.remove('hidden');
  banner.classList.add('hidden');
  nameEl.textContent = file.name;
  pctEl.textContent  = '0%';
  pctEl.className    = 'text-amber-400 text-sm font-black';
  barEl.className    = 'pbar-fill bg-amber-400';
  barEl.style.width  = '0%';

  startBtn.disabled  = true;
  startBtn.textContent = '⏳ Uploading Video... Please Wait';

  const formData = new FormData();
  formData.append('file', file);

  const xhr = new XMLHttpRequest();
  let startTime = Date.now();

  xhr.upload.onprogress = function(e) {
    if (e.lengthComputable) {
      const pct = Math.round((e.loaded / e.total) * 100);
      const loadedMB = (e.loaded / (1024*1024)).toFixed(1);
      const totalMB  = (e.total / (1024*1024)).toFixed(1);
      const elapsedSec = (Date.now() - startTime) / 1000;
      const speedMBs   = elapsedSec > 0 ? ((e.loaded / (1024*1024)) / elapsedSec).toFixed(1) : '0';

      pctEl.textContent  = pct + '% UPLOADING';
      barEl.style.width  = pct + '%';
      mbEl.textContent   = `${loadedMB} MB / ${totalMB} MB`;
      spdEl.textContent  = `${speedMBs} MB/s`;
    }
  };

  xhr.onload = function() {
    if (xhr.status === 200) {
      const data = JSON.parse(xhr.responseText);
      if (data.success) {
        resolvedURLs['upload'] = data.file_path;
        document.getElementById('url-upload').value = data.file_path;

        pctEl.textContent  = '100% COMPLETE 🎉';
        pctEl.className    = 'text-emerald-400 text-sm font-black';
        barEl.className    = 'pbar-fill bg-emerald-400';
        barEl.style.width  = '100%';
        banner.classList.remove('hidden');

        startBtn.disabled  = false;
        startBtn.textContent = '▶️ Start 24/7 Stream';

        if (data.preview_url) {
          const player = document.getElementById('video-preview-player');
          const placeholder = document.getElementById('video-preview-placeholder');
          const badge = document.getElementById('preview-badge');

          player.src = data.preview_url;
          player.classList.remove('hidden');
          placeholder.classList.add('hidden');
          badge.className = 'px-2.5 py-1 rounded-full text-[10px] font-bold bg-emerald-500/20 text-emerald-400 border border-emerald-500/30';
          badge.textContent = '🟢 PREVIEW READY (' + data.file_name + ')';
        }

        showToast(`🎉 ${data.file_name} uploaded & ready to stream!`, 'success');
        pollStatus();
      } else {
        startBtn.disabled = false;
        startBtn.textContent = '▶️ Start 24/7 Stream';
        showToast(data.message, 'error');
      }
    } else {
      startBtn.disabled = false;
      startBtn.textContent = '▶️ Start 24/7 Stream';
      showToast('Upload failed.', 'error');
    }
  };

  xhr.onerror = function() {
    startBtn.disabled = false;
    startBtn.textContent = '▶️ Start 24/7 Stream';
    showToast('Upload network error.', 'error');
  };

  xhr.open('POST', '/upload', true);
  xhr.send(formData);
}

async function uploadThumbnail(input) {
  if (!input.files[0]) return;
  const formData = new FormData();
  formData.append('file', input.files[0]);

  try {
    const res = await fetch('/upload-thumbnail', { method: 'POST', body: formData });
    const data = await res.json();
    if (data.success) {
      document.getElementById('thumb-img').src = data.thumbnail_url;
      document.getElementById('thumb-img').classList.remove('hidden');
      document.getElementById('thumb-placeholder').classList.add('hidden');
      showToast('Thumbnail uploaded successfully!', 'success');
    } else {
      showToast(data.message, 'error');
    }
  } catch(e) {
    showToast('Failed to upload thumbnail.', 'error');
  }
}

function generateSEO() {
  document.getElementById('seo-title').value = "🔴 24/7 Non-Stop Live Stream (HQ 60FPS) | Continuous Stream";
  document.getElementById('seo-desc').value  = "Welcome to our official 24/7 non-stop continuous live stream!\n\n🔔 Subscribe & Turn on notifications to stay updated.\n\n#Live #247 #Stream #YouTubeLive #NonStop";
  document.getElementById('seo-tags').value  = "live, 24/7, live stream, 24/7 stream, non stop, youtube live, 60fps, continuous live";
  showToast('High-ranking SEO Metadata generated!', 'success');
}

async function saveSEO() {
  const title = document.getElementById('seo-title').value;
  const desc  = document.getElementById('seo-desc').value;
  const tags  = document.getElementById('seo-tags').value;

  try {
    const res = await fetch('/save-seo', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ title, description: desc, tags }),
    });
    const data = await res.json();
    if (data.success) {
      showToast('SEO Settings saved!', 'success');
    }
  } catch(e) {
    showToast('Error saving SEO.', 'error');
  }
}

function openYouTubeStudio() {
  window.open('https://studio.youtube.com/channel/UC/livestreaming', '_blank');
}

function getVideoURL() {
  if (activeTab === 'direct')  return document.getElementById('url-direct').value.trim();
  if (activeTab === 'upload')  return resolvedURLs['upload'] || '';
  if (activeTab === 'gdrive')  return resolvedURLs['gdrive']  || document.getElementById('url-gdrive').value.trim();
  if (activeTab === 'dropbox') return resolvedURLs['dropbox'] || document.getElementById('url-dropbox').value.trim();
  if (activeTab === 'youtube') return resolvedURLs['youtube'] || document.getElementById('url-youtube').value.trim();
  return '';
}

function toggleMultiStream(cb) {
  document.getElementById('multi-platform-wrap').classList.toggle('hidden', !cb.checked);
  document.getElementById('single-platform-wrap').classList.toggle('hidden', cb.checked);
}

async function startStream() {
  const url = getVideoURL();
  const isMulti = document.getElementById('multi-stream-toggle').checked;

  if (!url) return showToast('Please select or enter a video source.', 'error');

  let payload = {
    url, source_type: activeTab,
    quality: activeQuality, fps: 30, audio_bitrate: '128k',
    auto_reconnect: document.getElementById('auto-reconnect').checked,
  };

  if (isMulti) {
    payload.yt_key = document.getElementById('yt-key').value.trim();
    payload.fb_key = document.getElementById('fb-key').value.trim();
    payload.tw_key = document.getElementById('tw-key').value.trim();
    payload.custom_rtmp = document.getElementById('custom-rtmp').value.trim();
  } else {
    let key = document.getElementById('stream-key').value.trim();
    if (activePlatform === 'custom') key = document.getElementById('custom-rtmp').value.trim();
    payload.key = key;
    payload.platform = activePlatform;
  }

  try {
    const res  = await fetch('/start', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    showToast(data.message, data.success ? 'success' : 'error');
  } catch(e) {
    showToast('Network error.', 'error');
  }
}

async function stopStream() {
  try {
    const res = await fetch('/stop', { method: 'POST' });
    const data = await res.json();
    showToast(data.message, data.success ? 'success' : 'error');
  } catch(e) {
    showToast('Network error.', 'error');
  }
}

async function pollStatus() {
  try {
    const res  = await fetch('/api/status');
    const d    = await res.json();

    const dot  = document.getElementById('status-dot');
    const txt  = document.getElementById('status-text');
    const hB   = document.getElementById('health-badge');
    const stats = d.live_stats || {};

    if (d.is_active) {
      txt.textContent = 'Streaming Live';
      txt.className = 'text-2xl font-black text-emerald-400';
      document.getElementById('uptime-text').textContent = 'Uptime: ' + d.uptime;
      document.getElementById('pid-text').textContent    = 'PID: ' + (d.pid || '');

      const health = stats.health || 'good';
      if (health === 'good') {
        dot.className = 'dot-good';
        hB.className  = 'px-3 py-1 rounded-full text-xs font-bold health-good';
        hB.textContent = '🟢 HEALTH EXCELLENT (1.0x)';
      } else if (health === 'warning') {
        dot.className = 'dot-warning';
        hB.className  = 'px-3 py-1 rounded-full text-xs font-bold health-warning';
        hB.textContent = '🟡 SLIGHT LAG / BUFFERING';
      } else {
        dot.className = 'dot-poor';
        hB.className  = 'px-3 py-1 rounded-full text-xs font-bold health-poor';
        hB.textContent = '🔴 POOR SPEED / NETWORK LAG';
      }

      document.getElementById('stat-fps').textContent     = (stats.fps || 0).toFixed(1);
      document.getElementById('stat-bitrate').textContent = stats.bitrate || '0 kbits/s';
      document.getElementById('stat-speed').textContent   = stats.speed || '0.0x';
      document.getElementById('stat-frames').textContent  = stats.frame || 0;
    } else {
      dot.className = 'dot-offline';
      txt.className = 'text-2xl font-black text-slate-400';
      txt.textContent = 'Offline';
      hB.className  = 'px-3 py-1 rounded-full text-xs font-bold health-offline';
      hB.textContent = 'STATIONARY';
      document.getElementById('uptime-text').textContent = 'Uptime: N/A';
      document.getElementById('pid-text').textContent    = '';

      document.getElementById('stat-fps').textContent     = '0.0';
      document.getElementById('stat-bitrate').textContent = '0 kbits/s';
      document.getElementById('stat-speed').textContent   = '0.0x';
      document.getElementById('stat-frames').textContent  = '0';
    }

    document.getElementById('cpu-pct').textContent = (d.cpu_percent || 0).toFixed(1) + '%';
    document.getElementById('cpu-bar').style.width = (d.cpu_percent || 0) + '%';
    document.getElementById('ram-pct').textContent = (d.ram_percent || 0).toFixed(1) + '%';
    document.getElementById('ram-bar').style.width = (d.ram_percent || 0) + '%';

    if (d.attached_source && d.attached_source.name) {
      document.getElementById('attached-name').textContent = d.attached_source.name;
      document.getElementById('attached-type').textContent = d.attached_source.type + (d.attached_source.size_mb ? ` (${d.attached_source.size_mb} MB)` : '');
      const badge = document.getElementById('attached-badge');
      if (d.attached_source.status === 'verified') {
        badge.className = 'px-3 py-1 rounded-full text-xs font-bold bg-emerald-500/20 text-emerald-400 border border-emerald-500/30';
        badge.textContent = '🟢 VERIFIED & READY';
      } else {
        badge.className = 'px-3 py-1 rounded-full text-xs font-bold bg-amber-500/20 text-amber-400 border border-amber-500/30';
        badge.textContent = '🟡 ATTACHING...';
      }
    }

    if (d.seo) {
      if (!document.getElementById('seo-title').value) document.getElementById('seo-title').value = d.seo.title || '';
      if (!document.getElementById('seo-desc').value)  document.getElementById('seo-desc').value  = d.seo.description || '';
      if (!document.getElementById('seo-tags').value)  document.getElementById('seo-tags').value  = d.seo.tags || '';
      if (d.seo.thumbnail_url && document.getElementById('thumb-img').classList.contains('hidden')) {
        document.getElementById('thumb-img').src = d.seo.thumbnail_url;
        document.getElementById('thumb-img').classList.remove('hidden');
        document.getElementById('thumb-placeholder').classList.add('hidden');
      }
    }
  } catch(e) {}
}

function showToast(msg, type='info') {
  const t = document.getElementById('toast');
  const ti = document.getElementById('toast-inner');
  ti.className = `glass-strong rounded-2xl px-5 py-3 text-xs font-bold ${type==='error'?'text-red-400':type==='success'?'text-emerald-400':'text-sky-400'}`;
  ti.textContent = msg;
  t.classList.remove('hidden');
  setTimeout(() => t.classList.add('hidden'), 4000);
}

pollStatus();
setInterval(pollStatus, 2000);
</script>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════════════════════
# Telegram Bot
# ═══════════════════════════════════════════════════════════════════════════════

HELP_TEXT = """
🎥 *YouTube 24/7 Live Streamer Bot* — Pro Edition

*Commands:*
/start — Help
/stream `<url>` `<key>` — Start streaming
/stop — Stop active stream
/status — Show health & stats
"""


async def tg_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")


async def tg_stream(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text("❌ Usage: `/stream <video_url> <stream_key>`", parse_mode="Markdown")
        return
    await update.message.reply_text("⏳ Launching stream…")
    success, msg = start_stream(args[0].strip(), args[1].strip())
    await update.message.reply_text(("✅ " if success else "❌ ") + msg)


async def tg_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    success, msg = stop_stream()
    await update.message.reply_text(("✅ " if success else "❌ ") + msg)


async def tg_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with _state_lock:
        active   = stream_state.is_active
        pid      = stream_state.process.pid if stream_state.process else None
        platform = stream_state.platform
        quality  = stream_state.quality

    stats  = get_system_stats()
    uptime = get_uptime()
    l_stats = ffmpeg_live_stats

    status_line = (
        f"🟢 *Live* (PID: `{pid}`)\n"
        f"⏱ Uptime: `{uptime}`\n"
        f"📺 Platform: `{platform}` | 🎬 Quality: `{quality}`\n"
        f"⚡ FPS: `{l_stats.get('fps', 0)}` | 📶 Bitrate: `{l_stats.get('bitrate', '0k')}` | 🚀 Speed: `{l_stats.get('speed', '1.0x')}`"
    ) if active else "🔴 *Offline*"

    await update.message.reply_text(
        f"📊 *Stream Status*\n\n{status_line}\n\n"
        f"⚡ CPU: `{stats['cpu_percent']:.1f}%` | "
        f"🧠 RAM: `{stats['ram_percent']:.1f}%`",
        parse_mode="Markdown",
    )


def run_telegram_bot(token: str):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("start",  tg_start))
    application.add_handler(CommandHandler("stream", tg_stream))
    application.add_handler(CommandHandler("stop",   tg_stop))
    application.add_handler(CommandHandler("status", tg_status))
    logger.info("Telegram bot polling…")
    application.run_polling(allowed_updates=Update.ALL_TYPES, close_loop=False)


def _keep_alive_thread():
    """Background thread: pings local status endpoint every 2 minutes to keep server awake 24/7."""
    import time
    time.sleep(15)
    while True:
        try:
            port = os.environ.get("PORT", "5000")
            requests.get(f"http://127.0.0.1:{port}/api/status", timeout=5)
        except Exception:
            pass
        time.sleep(120)


# ═══════════════════════════════════════════════════════════════════════════════
# Entry Point
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if bot_token:
        threading.Thread(target=run_telegram_bot, args=(bot_token,), daemon=True, name="TelegramBot").start()

    # Start 24/7 Keep-Alive Thread
    threading.Thread(target=_keep_alive_thread, daemon=True, name="KeepAlive").start()

    port = int(os.environ.get("PORT", 5000))
    logger.info("Starting Flask on 0.0.0.0:%d…", port)
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False, threaded=True)
