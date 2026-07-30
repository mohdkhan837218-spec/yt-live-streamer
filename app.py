"""
YouTube 24/7 Live Streamer — app.py (Advanced Edition)
=======================================================
Flask + Telegram Bot + FFmpeg with:
  - Multiple video sources (URL / PC Upload / Google Drive / Dropbox / YouTube via yt-dlp)
  - Advanced stream settings (platform, quality, fps, audio, auto-reconnect)
  - Professional dashboard UI
"""

import os
import re
import signal
import subprocess
import threading
import uuid
import asyncio
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import psutil
from flask import Flask, jsonify, render_template_string, request

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

# ─── Upload Directory ──────────────────────────────────────────────────────────
UPLOAD_DIR = Path("/tmp/yt_streamer_uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB

# ─── Platform RTMP Endpoints ───────────────────────────────────────────────────
PLATFORM_RTMP = {
    "youtube":  "rtmp://a.rtmp.youtube.com/live2/{}",
    "facebook": "rtmps://live-api-s.facebook.com:443/rtmp/{}",
    "twitch":   "rtmp://live.twitch.tv/app/{}",
    "custom":   "{}",  # user provides full custom RTMP URL
}

# ─── Quality Presets ───────────────────────────────────────────────────────────
QUALITY_PRESETS = {
    "480p": {
        "vb": "1500k", "maxrate": "1500k",
        "bufsize": "3000k", "scale": "854:480",
    },
    "720p": {
        "vb": "2500k", "maxrate": "2500k",
        "bufsize": "5000k", "scale": "1280:720",
    },
    "1080p": {
        "vb": "4000k", "maxrate": "4000k",
        "bufsize": "8000k", "scale": "1920:1080",
    },
    "1080p_hq": {
        "vb": "6000k", "maxrate": "6000k",
        "bufsize": "12000k", "scale": "1920:1080",
    },
    "4k": {
        "vb": "8000k", "maxrate": "8000k",
        "bufsize": "16000k", "scale": "3840:2160",
    },
}


# ═══════════════════════════════════════════════════════════════════════════════
# Stream State
# ═══════════════════════════════════════════════════════════════════════════════

_state_lock = threading.Lock()


class StreamState:
    process: Optional[subprocess.Popen] = None
    video_url: str = ""
    stream_key: str = ""
    platform: str = "youtube"
    quality: str = "1080p"
    fps: int = 30
    audio_bitrate: str = "128k"
    auto_reconnect: bool = False
    started_at: Optional[datetime] = None
    is_active: bool = False
    uploaded_file: Optional[Path] = None   # track uploaded file for cleanup


stream_state = StreamState()


# ═══════════════════════════════════════════════════════════════════════════════
# URL Resolvers
# ═══════════════════════════════════════════════════════════════════════════════

def resolve_gdrive_url(url: str) -> tuple[str, str]:
    """Convert Google Drive share URL to direct download URL."""
    # Patterns:
    # https://drive.google.com/file/d/FILE_ID/view
    # https://drive.google.com/open?id=FILE_ID
    match = re.search(r"/d/([a-zA-Z0-9_-]+)", url)
    if not match:
        match = re.search(r"[?&]id=([a-zA-Z0-9_-]+)", url)
    if match:
        file_id = match.group(1)
        direct = f"https://drive.google.com/uc?export=download&id={file_id}&confirm=t"
        return direct, ""
    return "", "Google Drive: could not extract file ID from URL."


def resolve_dropbox_url(url: str) -> tuple[str, str]:
    """Convert Dropbox share URL to direct download URL."""
    direct = re.sub(r"[?&]dl=0", "", url)
    if "?" in direct:
        direct += "&dl=1"
    else:
        direct += "?dl=1"
    return direct, ""


def resolve_youtube_url(url: str) -> tuple[str, str]:
    """Use yt-dlp to extract a direct stream URL from YouTube."""
    try:
        result = subprocess.run(
            [
                "yt-dlp",
                "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
                "--get-url",
                url,
            ],
            capture_output=True,
            text=True,
            timeout=45,
        )
        if result.returncode == 0:
            urls = result.stdout.strip().splitlines()
            # Return the first URL (video); yt-dlp may return 2 lines for separate A/V
            return urls[0], ""
        return "", f"yt-dlp error: {result.stderr.strip()[:300]}"
    except FileNotFoundError:
        return "", "yt-dlp is not installed in this container."
    except subprocess.TimeoutExpired:
        return "", "yt-dlp timed out while fetching YouTube URL."
    except Exception as exc:
        return "", str(exc)


def resolve_video_url(raw_url: str, source_type: str) -> tuple[str, str]:
    """Resolve any video source type to a final FFmpeg-compatible URL."""
    if source_type == "gdrive":
        return resolve_gdrive_url(raw_url)
    if source_type == "dropbox":
        return resolve_dropbox_url(raw_url)
    if source_type == "youtube":
        return resolve_youtube_url(raw_url)
    # direct / uploaded file path
    return raw_url, ""


# ═══════════════════════════════════════════════════════════════════════════════
# FFmpeg Command Builder
# ═══════════════════════════════════════════════════════════════════════════════

def build_rtmp_url(platform: str, key: str) -> str:
    template = PLATFORM_RTMP.get(platform, PLATFORM_RTMP["youtube"])
    return template.format(key)


def build_ffmpeg_command(
    video_url: str,
    stream_key: str,
    platform: str = "youtube",
    quality: str = "1080p",
    fps: int = 30,
    audio_bitrate: str = "128k",
) -> list[str]:
    q = QUALITY_PRESETS.get(quality, QUALITY_PRESETS["1080p"])
    rtmp = build_rtmp_url(platform, stream_key)
    gop = fps * 2  # keyframe interval = 2 seconds

    return [
        "ffmpeg",
        "-re",
        "-stream_loop", "-1",
        "-i", video_url,
        # Video
        "-vf", f"scale={q['scale']}:force_original_aspect_ratio=decrease,pad={q['scale']}:(ow-iw)/2:(oh-ih)/2",
        "-c:v", "libx264",
        "-preset", "superfast",
        "-b:v", q["vb"],
        "-maxrate", q["maxrate"],
        "-bufsize", q["bufsize"],
        "-pix_fmt", "yuv420p",
        "-r", str(fps),
        "-g", str(gop),
        # Audio
        "-c:a", "aac",
        "-b:a", audio_bitrate,
        "-ar", "44100",
        # Output
        "-f", "flv",
        rtmp,
    ]


# ═══════════════════════════════════════════════════════════════════════════════
# Process Management
# ═══════════════════════════════════════════════════════════════════════════════

def _monitor_process():
    """Watches the FFmpeg process. Auto-reconnects if enabled."""
    with _state_lock:
        proc = stream_state.process

    if proc is None:
        return

    proc.wait()

    with _state_lock:
        if stream_state.process is not proc:
            return  # A different process is now active; ignore
        should_reconnect = stream_state.auto_reconnect and stream_state.is_active
        video_url    = stream_state.video_url
        stream_key   = stream_state.stream_key
        platform     = stream_state.platform
        quality      = stream_state.quality
        fps          = stream_state.fps
        audio_bitrate = stream_state.audio_bitrate

    if should_reconnect:
        logger.warning("FFmpeg exited unexpectedly. Auto-reconnecting in 5s…")
        threading.Event().wait(5)
        success, msg = _launch_ffmpeg(video_url, stream_key, platform, quality, fps, audio_bitrate)
        if success:
            logger.info("Auto-reconnect successful.")
        else:
            logger.error("Auto-reconnect failed: %s", msg)
            with _state_lock:
                stream_state.is_active = False
                stream_state.process = None
                stream_state.started_at = None
    else:
        logger.warning("FFmpeg process exited. Stream marked offline.")
        with _state_lock:
            stream_state.process = None
            stream_state.is_active = False
            stream_state.started_at = None


def _launch_ffmpeg(
    video_url: str,
    stream_key: str,
    platform: str,
    quality: str,
    fps: int,
    audio_bitrate: str,
) -> tuple[bool, str]:
    """Internal: spawn FFmpeg and set state. NOT lock-protected — caller manages."""
    cmd = build_ffmpeg_command(video_url, stream_key, platform, quality, fps, audio_bitrate)
    logger.info("Launching FFmpeg: %s", " ".join(cmd[:8]) + " …")
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
        stream_state.process     = proc
        stream_state.video_url   = video_url
        stream_state.stream_key  = stream_key
        stream_state.platform    = platform
        stream_state.quality     = quality
        stream_state.fps         = fps
        stream_state.audio_bitrate = audio_bitrate
        stream_state.started_at  = datetime.now(timezone.utc)
        stream_state.is_active   = True

    watcher = threading.Thread(target=_monitor_process, daemon=True)
    watcher.start()
    logger.info("FFmpeg started (PID=%s)", proc.pid)
    return True, f"Stream started (PID={proc.pid})."


def start_stream(
    video_url: str,
    stream_key: str,
    platform: str = "youtube",
    quality: str = "1080p",
    fps: int = 30,
    audio_bitrate: str = "128k",
    auto_reconnect: bool = False,
) -> tuple[bool, str]:
    with _state_lock:
        if stream_state.is_active and stream_state.process:
            return False, "A stream is already running. Stop it first."
        stream_state.auto_reconnect = auto_reconnect

    return _launch_ffmpeg(video_url, stream_key, platform, quality, fps, audio_bitrate)


def stop_stream() -> tuple[bool, str]:
    with _state_lock:
        if not stream_state.is_active or stream_state.process is None:
            return False, "No active stream to stop."
        proc = stream_state.process
        stream_state.auto_reconnect = False  # prevent auto-restart on manual stop

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

    # Clean up uploaded file if used
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
            logger.info("Cleaned up uploaded file: %s", uploaded)
        except Exception:
            pass

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


@app.route("/start", methods=["POST"])
def route_start():
    data          = request.get_json(silent=True) or request.form
    raw_url       = (data.get("url")          or "").strip()
    stream_key    = (data.get("key")          or "").strip()
    source_type   = (data.get("source_type")  or "direct").strip()
    platform      = (data.get("platform")     or "youtube").strip()
    quality       = (data.get("quality")      or "1080p").strip()
    fps           = int(data.get("fps", 30))
    audio_bitrate = (data.get("audio_bitrate") or "128k").strip()
    auto_reconnect = str(data.get("auto_reconnect", "false")).lower() == "true"

    if not raw_url:
        return jsonify({"success": False, "message": "Video URL/path is required."}), 400
    if not stream_key:
        return jsonify({"success": False, "message": "Stream key is required."}), 400
    if platform not in PLATFORM_RTMP:
        return jsonify({"success": False, "message": f"Unknown platform: {platform}"}), 400
    if quality not in QUALITY_PRESETS:
        quality = "1080p"

    # Resolve URL based on source type
    if source_type != "upload":
        video_url, err = resolve_video_url(raw_url, source_type)
        if err:
            return jsonify({"success": False, "message": err}), 400
    else:
        video_url = raw_url  # Already resolved path from upload endpoint

    success, message = start_stream(
        video_url, stream_key, platform, quality, fps, audio_bitrate, auto_reconnect
    )
    return jsonify({"success": success, "message": message}), 200 if success else 409


@app.route("/stop", methods=["POST"])
def route_stop():
    success, message = stop_stream()
    return jsonify({"success": success, "message": message})


@app.route("/api/status")
def route_status():
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
        **stats,
    })


@app.route("/upload", methods=["POST"])
def route_upload():
    """Upload a video file from the user's PC."""
    if "file" not in request.files:
        return jsonify({"success": False, "message": "No file part in request."}), 400

    f = request.files["file"]
    if f.filename == "":
        return jsonify({"success": False, "message": "No file selected."}), 400

    # Check content length
    f.seek(0, 2)
    size = f.tell()
    f.seek(0)
    if size > MAX_UPLOAD_BYTES:
        return jsonify({"success": False, "message": f"File too large (max 2 GB)."}), 413

    ext = Path(f.filename).suffix.lower()
    allowed = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".webm", ".m4v", ".ts"}
    if ext not in allowed:
        return jsonify({"success": False, "message": f"File type '{ext}' not allowed."}), 400

    unique_name = f"{uuid.uuid4().hex}{ext}"
    save_path   = UPLOAD_DIR / unique_name
    f.save(save_path)

    with _state_lock:
        stream_state.uploaded_file = save_path

    logger.info("File uploaded: %s (%d bytes)", save_path, size)
    return jsonify({
        "success":   True,
        "message":   f"File uploaded: {f.filename}",
        "file_path": str(save_path),
        "file_name": f.filename,
        "size_mb":   round(size / (1024**2), 1),
    })


@app.route("/resolve-url", methods=["POST"])
def route_resolve_url():
    """Resolve a GDrive / Dropbox / YouTube URL to a direct download link."""
    data        = request.get_json(silent=True) or {}
    raw_url     = (data.get("url")         or "").strip()
    source_type = (data.get("source_type") or "").strip()

    if not raw_url:
        return jsonify({"success": False, "message": "URL is required."}), 400

    resolved, err = resolve_video_url(raw_url, source_type)
    if err:
        return jsonify({"success": False, "message": err}), 400

    return jsonify({"success": True, "resolved_url": resolved})


# ═══════════════════════════════════════════════════════════════════════════════
# Dashboard HTML — Professional Dark UI
# ═══════════════════════════════════════════════════════════════════════════════

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en" class="dark">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>YouTube 24/7 Live Streamer — Pro Dashboard</title>
  <meta name="description" content="Professional 24/7 YouTube live streaming control panel powered by FFmpeg. Upload videos, connect cloud storage, and stream to any platform."/>
  <script src="https://cdn.tailwindcss.com"></script>
  <link rel="preconnect" href="https://fonts.googleapis.com"/>
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&display=swap" rel="stylesheet"/>
  <script>
    tailwind.config = {
      darkMode: 'class',
      theme: { extend: { fontFamily: { sans: ['Inter','sans-serif'] } } }
    };
  </script>
  <style>
    :root{--bg:#080d1c;--card:rgba(255,255,255,0.04);--border:rgba(255,255,255,0.08);--text:#e2e8f0;--muted:#64748b}
    *{box-sizing:border-box}
    body{background:linear-gradient(135deg,#060b18 0%,#0b1630 50%,#07121f 100%);min-height:100vh;color:var(--text)}
    .glass{background:var(--card);backdrop-filter:blur(20px);border:1px solid var(--border)}
    .glass-strong{background:rgba(255,255,255,0.06);backdrop-filter:blur(24px);border:1px solid rgba(255,255,255,0.12)}
    .glow-blue{box-shadow:0 0 40px rgba(56,189,248,0.12),0 0 80px rgba(56,189,248,0.04)}
    .glow-green{box-shadow:0 0 30px rgba(34,197,94,0.2)}
    /* Status dot */
    .dot-live{width:12px;height:12px;border-radius:50%;background:#22c55e;box-shadow:0 0 12px #22c55e;animation:pulse 1.5s infinite}
    .dot-offline{width:12px;height:12px;border-radius:50%;background:#ef4444;box-shadow:0 0 8px #ef4444}
    /* Tabs */
    .src-tab{padding:8px 16px;border-radius:10px;font-size:.8rem;font-weight:600;cursor:pointer;transition:all .2s;color:#64748b;border:1px solid transparent;display:flex;align-items:center;gap:6px;white-space:nowrap}
    .src-tab:hover{color:#94a3b8;background:rgba(255,255,255,0.04)}
    .src-tab.active{background:linear-gradient(135deg,rgba(56,189,248,0.15),rgba(139,92,246,0.15));border-color:rgba(56,189,248,0.3);color:#38bdf8}
    /* Platform buttons */
    .plat-btn{padding:10px 18px;border-radius:12px;font-size:.8rem;font-weight:700;cursor:pointer;transition:all .2s;border:1px solid var(--border);color:#64748b;display:flex;align-items:center;gap:8px;flex:1;justify-content:center}
    .plat-btn:hover{border-color:rgba(255,255,255,0.2);color:#94a3b8}
    .plat-btn.active{border-color:rgba(56,189,248,0.5);color:#38bdf8;background:rgba(56,189,248,0.08)}
    /* Quality cards */
    .q-card{padding:10px 14px;border-radius:12px;cursor:pointer;transition:all .2s;border:1px solid var(--border);text-align:center;flex:1}
    .q-card:hover{border-color:rgba(255,255,255,0.2)}
    .q-card.active{border-color:rgba(139,92,246,0.5);background:rgba(139,92,246,0.08)}
    .q-card .label{font-size:.75rem;font-weight:700;color:#a78bfa}
    .q-card .sub{font-size:.65rem;color:#475569;margin-top:2px}
    /* Inputs */
    .inp{width:100%;background:rgba(255,255,255,0.04);border:1px solid var(--border);color:var(--text);border-radius:12px;padding:12px 16px;font-size:.875rem;transition:all .2s;outline:none}
    .inp:focus{border-color:rgba(56,189,248,0.5);box-shadow:0 0 0 3px rgba(56,189,248,0.1);background:rgba(255,255,255,0.06)}
    .inp::placeholder{color:#334155}
    /* Buttons */
    .btn-start{background:linear-gradient(135deg,#059669,#047857);border:none;color:#fff;padding:14px 24px;border-radius:14px;font-weight:700;font-size:.9rem;cursor:pointer;transition:all .2s;display:flex;align-items:center;justify-content:center;gap:8px;width:100%}
    .btn-start:hover:not(:disabled){background:linear-gradient(135deg,#10b981,#059669);transform:translateY(-2px);box-shadow:0 12px 30px rgba(5,150,105,.4)}
    .btn-stop{background:linear-gradient(135deg,#dc2626,#b91c1c);border:none;color:#fff;padding:14px 24px;border-radius:14px;font-weight:700;font-size:.9rem;cursor:pointer;transition:all .2s;display:flex;align-items:center;justify-content:center;gap:8px;width:100%}
    .btn-stop:hover:not(:disabled){background:linear-gradient(135deg,#ef4444,#dc2626);transform:translateY(-2px);box-shadow:0 12px 30px rgba(220,38,38,.4)}
    button:disabled{opacity:.45;cursor:not-allowed;transform:none!important}
    /* Progress bars */
    .pbar{height:5px;border-radius:3px;background:rgba(255,255,255,0.08);overflow:hidden}
    .pbar-fill{height:100%;border-radius:3px;transition:width .6s ease}
    /* Toggle switch */
    .toggle{position:relative;width:44px;height:24px;flex-shrink:0}
    .toggle input{opacity:0;width:0;height:0}
    .toggle-slider{position:absolute;cursor:pointer;inset:0;background:rgba(255,255,255,0.1);border-radius:24px;transition:.3s;border:1px solid var(--border)}
    .toggle-slider:before{content:"";position:absolute;width:18px;height:18px;left:2px;top:2px;background:#64748b;border-radius:50%;transition:.3s}
    input:checked + .toggle-slider{background:rgba(56,189,248,0.2);border-color:rgba(56,189,248,0.4)}
    input:checked + .toggle-slider:before{transform:translateX(20px);background:#38bdf8}
    /* Upload zone */
    .upload-zone{border:2px dashed rgba(56,189,248,0.25);border-radius:16px;padding:36px 20px;text-align:center;cursor:pointer;transition:all .3s;background:rgba(56,189,248,0.02)}
    .upload-zone:hover,.upload-zone.drag-over{border-color:rgba(56,189,248,0.5);background:rgba(56,189,248,0.06)}
    /* Toast */
    .toast{position:fixed;bottom:24px;right:24px;z-index:100;min-width:300px;animation:slideUp .3s ease}
    @keyframes slideUp{from{opacity:0;transform:translateY(16px)}to{opacity:1;transform:translateY(0)}}
    @keyframes fadeIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
    .fade-in{animation:fadeIn .4s ease}
    /* Grid background */
    body::before{content:"";position:fixed;inset:0;opacity:.04;background-image:radial-gradient(circle,#4f9cf8 1px,transparent 1px);background-size:36px 36px;pointer-events:none}
    /* Section label */
    .sec-label{font-size:.7rem;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:#334155;margin-bottom:12px}
    /* Badge */
    .badge{display:inline-flex;align-items:center;gap:6px;padding:4px 12px;border-radius:20px;font-size:.72rem;font-weight:600}
  </style>
</head>
<body class="font-sans antialiased">

<div class="min-h-screen p-4 lg:p-8 max-w-6xl mx-auto">

  <!-- Header -->
  <header class="text-center mb-8 fade-in">
    <div class="inline-flex items-center gap-4 mb-2">
      <span class="text-5xl">🎥</span>
      <div class="text-left">
        <h1 class="text-4xl lg:text-5xl font-black bg-gradient-to-r from-sky-400 via-blue-400 to-violet-500 bg-clip-text text-transparent leading-tight">24/7 Live Streamer</h1>
        <p class="text-slate-500 text-sm font-medium">Pro Dashboard · Powered by FFmpeg</p>
      </div>
    </div>
  </header>

  <div class="grid grid-cols-1 lg:grid-cols-3 gap-5">

    <!-- LEFT COLUMN -->
    <div class="lg:col-span-2 space-y-5">

      <!-- Status Card -->
      <div class="glass rounded-2xl p-5 glow-blue fade-in">
        <div class="flex flex-wrap items-center justify-between gap-4">
          <div>
            <div class="sec-label">Stream Status</div>
            <div class="flex items-center gap-3">
              <span id="status-dot" class="dot-offline"></span>
              <span id="status-text" class="text-2xl font-black text-red-400">🔴 Offline</span>
            </div>
            <div class="flex flex-wrap gap-4 mt-2">
              <span class="text-slate-500 text-sm" id="uptime-text">Uptime: N/A</span>
              <span class="text-slate-600 text-sm font-mono" id="pid-text"></span>
            </div>
          </div>
          <div class="flex flex-wrap gap-2" id="active-badges"></div>
        </div>
      </div>

      <!-- Metrics -->
      <div class="grid grid-cols-2 gap-4 fade-in">
        <div class="glass rounded-2xl p-4">
          <div class="flex justify-between items-center mb-3">
            <span class="text-slate-400 text-sm font-semibold">⚡ CPU</span>
            <span id="cpu-pct" class="text-sky-400 font-black text-lg">0%</span>
          </div>
          <div class="pbar"><div id="cpu-bar" class="pbar-fill bg-gradient-to-r from-sky-500 to-blue-400" style="width:0%"></div></div>
        </div>
        <div class="glass rounded-2xl p-4">
          <div class="flex justify-between items-center mb-3">
            <span class="text-slate-400 text-sm font-semibold">🧠 RAM</span>
            <span id="ram-pct" class="text-violet-400 font-black text-lg">0%</span>
          </div>
          <div class="pbar"><div id="ram-bar" class="pbar-fill bg-gradient-to-r from-violet-500 to-purple-400" style="width:0%"></div></div>
          <p id="ram-detail" class="text-slate-600 text-xs mt-2">— MB / — MB</p>
        </div>
      </div>

      <!-- VIDEO SOURCE -->
      <div class="glass rounded-2xl p-5 fade-in">
        <div class="sec-label">📹 Video Source</div>
        <!-- Source Tabs -->
        <div class="flex flex-wrap gap-2 mb-5 overflow-x-auto pb-1">
          <button class="src-tab active" onclick="switchTab('direct')"   id="tab-direct">   🔗 Direct URL</button>
          <button class="src-tab"        onclick="switchTab('upload')"   id="tab-upload">   📁 Upload PC</button>
          <button class="src-tab"        onclick="switchTab('gdrive')"   id="tab-gdrive">   ☁️ Google Drive</button>
          <button class="src-tab"        onclick="switchTab('dropbox')"  id="tab-dropbox">  📦 Dropbox</button>
          <button class="src-tab"        onclick="switchTab('youtube')"  id="tab-youtube">  🎬 YouTube</button>
        </div>

        <!-- Tab Panels -->
        <div id="panel-direct">
          <label class="block text-slate-400 text-xs font-semibold mb-2">Direct video link (MP4 / MKV / HLS / DASH)</label>
          <input id="url-direct" class="inp" type="url" placeholder="https://cdn.example.com/video.mp4"/>
        </div>

        <div id="panel-upload" style="display:none">
          <div class="upload-zone" id="upload-zone" onclick="document.getElementById('file-input').click()"
               ondragover="ev.preventDefault();this.classList.add('drag-over')"
               ondragleave="this.classList.remove('drag-over')"
               ondrop="handleDrop(event)">
            <div class="text-4xl mb-3">📁</div>
            <p class="text-slate-400 font-semibold text-sm">Drag & drop or click to browse</p>
            <p class="text-slate-600 text-xs mt-1">MP4, MKV, AVI, MOV, FLV, WebM · Max 2 GB</p>
            <div id="upload-status" class="mt-3 text-sky-400 text-sm font-semibold hidden"></div>
          </div>
          <input id="file-input" type="file" accept="video/*,.mkv,.flv,.ts" class="hidden" onchange="handleFileSelect(this)"/>
          <input id="url-upload" type="hidden"/>
        </div>

        <div id="panel-gdrive" style="display:none">
          <label class="block text-slate-400 text-xs font-semibold mb-2">Google Drive share link</label>
          <div class="flex gap-2">
            <input id="url-gdrive" class="inp" type="url" placeholder="https://drive.google.com/file/d/FILE_ID/view"/>
            <button onclick="resolveURL('gdrive')" class="px-4 py-3 glass rounded-xl text-sky-400 text-sm font-semibold whitespace-nowrap hover:bg-white/5 transition-all">Resolve →</button>
          </div>
          <p id="gdrive-resolved" class="text-emerald-400 text-xs mt-2 hidden"></p>
        </div>

        <div id="panel-dropbox" style="display:none">
          <label class="block text-slate-400 text-xs font-semibold mb-2">Dropbox share link</label>
          <div class="flex gap-2">
            <input id="url-dropbox" class="inp" type="url" placeholder="https://www.dropbox.com/s/xxx/video.mp4?dl=0"/>
            <button onclick="resolveURL('dropbox')" class="px-4 py-3 glass rounded-xl text-sky-400 text-sm font-semibold whitespace-nowrap hover:bg-white/5 transition-all">Resolve →</button>
          </div>
          <p id="dropbox-resolved" class="text-emerald-400 text-xs mt-2 hidden"></p>
        </div>

        <div id="panel-youtube" style="display:none">
          <label class="block text-slate-400 text-xs font-semibold mb-2">YouTube video URL (yt-dlp will extract stream)</label>
          <div class="flex gap-2">
            <input id="url-youtube" class="inp" type="url" placeholder="https://www.youtube.com/watch?v=VIDEO_ID"/>
            <button onclick="resolveURL('youtube')" class="px-4 py-3 glass rounded-xl text-sky-400 text-sm font-semibold whitespace-nowrap hover:bg-white/5 transition-all">Extract →</button>
          </div>
          <p id="youtube-resolved" class="text-slate-500 text-xs mt-2">⚠️ May take ~30 seconds to resolve via yt-dlp</p>
        </div>
      </div>

      <!-- STREAM SETTINGS -->
      <div class="glass rounded-2xl p-5 fade-in">
        <div class="sec-label">🎛️ Stream Settings</div>

        <!-- Platform -->
        <div class="mb-5">
          <label class="block text-slate-400 text-xs font-semibold mb-3">Streaming Platform</label>
          <div class="flex flex-wrap gap-2">
            <button class="plat-btn active" onclick="setPlatform('youtube')"  id="plat-youtube">  📺 YouTube</button>
            <button class="plat-btn"        onclick="setPlatform('facebook')" id="plat-facebook"> 📘 Facebook</button>
            <button class="plat-btn"        onclick="setPlatform('twitch')"   id="plat-twitch">   🟣 Twitch</button>
            <button class="plat-btn"        onclick="setPlatform('custom')"   id="plat-custom">   🔧 Custom RTMP</button>
          </div>
        </div>

        <!-- Stream Key / Custom RTMP -->
        <div class="mb-5">
          <label class="block text-slate-400 text-xs font-semibold mb-2" id="key-label">🔑 Stream Key</label>
          <input id="stream-key" class="inp font-mono" type="password" placeholder="xxxx-xxxx-xxxx-xxxx-xxxx"/>
          <div id="custom-rtmp-wrap" class="mt-3 hidden">
            <label class="block text-slate-400 text-xs font-semibold mb-2">Custom RTMP URL (include key)</label>
            <input id="custom-rtmp" class="inp font-mono" type="text" placeholder="rtmp://your-server.com/live/YOUR_STREAM_KEY"/>
          </div>
        </div>

        <!-- Quality -->
        <div class="mb-5">
          <label class="block text-slate-400 text-xs font-semibold mb-3">Video Quality</label>
          <div class="flex flex-wrap gap-2">
            <div class="q-card active" onclick="setQuality('480p')"     id="q-480p">     <div class="label">480p ⭐</div>  <div class="sub">1.5 Mbps · Stable</div></div>
            <div class="q-card" onclick="setQuality('720p')"     id="q-720p">     <div class="label">720p</div>    <div class="sub">2.5 Mbps</div></div>
            <div class="q-card" onclick="setQuality('1080p')"   id="q-1080p">   <div class="label">1080p</div>   <div class="sub">4 Mbps</div></div>
            <div class="q-card" onclick="setQuality('1080p_hq')" id="q-1080p_hq"> <div class="label">1080p HQ</div><div class="sub">6 Mbps</div></div>
            <div class="q-card" onclick="setQuality('4k')"       id="q-4k">       <div class="label">4K Ultra</div><div class="sub">8 Mbps</div></div>
          </div>
        </div>

        <!-- FPS + Audio -->
        <div class="grid grid-cols-2 gap-4 mb-5">
          <div>
            <label class="block text-slate-400 text-xs font-semibold mb-2">Frame Rate</label>
            <div class="flex gap-2">
              <button onclick="setFPS(30)"  id="fps-30"  class="plat-btn active text-sm flex-1">30 FPS</button>
              <button onclick="setFPS(60)"  id="fps-60"  class="plat-btn text-sm flex-1">60 FPS</button>
            </div>
          </div>
          <div>
            <label class="block text-slate-400 text-xs font-semibold mb-2">Audio Quality</label>
            <select id="audio-bitrate" class="inp" style="padding:10px 14px">
              <option value="128k">128k — Standard</option>
              <option value="192k">192k — High</option>
              <option value="320k">320k — Lossless</option>
            </select>
          </div>
        </div>

        <!-- Auto-reconnect -->
        <div class="flex items-center justify-between glass rounded-xl p-4">
          <div>
            <p class="text-slate-300 text-sm font-semibold">🔄 Auto-Reconnect</p>
            <p class="text-slate-600 text-xs mt-0.5">Restart stream automatically if FFmpeg crashes</p>
          </div>
          <label class="toggle">
            <input type="checkbox" id="auto-reconnect"/>
            <span class="toggle-slider"></span>
          </label>
        </div>
      </div>

      <!-- Action Buttons -->
      <div class="grid grid-cols-2 gap-4 fade-in">
        <button id="btn-start" onclick="startStream()" class="btn-start">
          <span>▶️</span> Start 24/7 Stream
        </button>
        <button id="btn-stop" onclick="stopStream()" class="btn-stop">
          <span>⏹️</span> Stop Stream
        </button>
      </div>
    </div>

    <!-- RIGHT COLUMN -->
    <div class="space-y-5">

      <!-- Active Stream Info -->
      <div class="glass rounded-2xl p-5 fade-in" id="stream-info-card" style="display:none">
        <div class="sec-label">📡 Active Stream</div>
        <div class="space-y-3 text-sm">
          <div class="flex justify-between"><span class="text-slate-500">Platform</span><span id="info-platform" class="text-slate-300 font-semibold capitalize">—</span></div>
          <div class="flex justify-between"><span class="text-slate-500">Quality</span><span id="info-quality"   class="text-slate-300 font-semibold">—</span></div>
          <div class="flex justify-between"><span class="text-slate-500">FPS</span>    <span id="info-fps"      class="text-slate-300 font-semibold">—</span></div>
          <div class="flex justify-between"><span class="text-slate-500">Auto-Reconnect</span><span id="info-ar" class="font-semibold">—</span></div>
        </div>
      </div>

      <!-- Telegram Bot -->
      <div class="glass rounded-2xl p-5 fade-in">
        <div class="sec-label">🤖 Telegram Bot Commands</div>
        <div class="space-y-2 text-xs font-mono">
          <div class="glass rounded-lg p-2.5"><span class="text-sky-400">/start</span><span class="text-slate-600"> — Help</span></div>
          <div class="glass rounded-lg p-2.5"><span class="text-sky-400">/stream &lt;url&gt; &lt;key&gt;</span><span class="text-slate-600"> — Start</span></div>
          <div class="glass rounded-lg p-2.5"><span class="text-sky-400">/stop</span><span class="text-slate-600"> — Stop stream</span></div>
          <div class="glass rounded-lg p-2.5"><span class="text-sky-400">/status</span><span class="text-slate-600"> — Status & metrics</span></div>
        </div>
        <p class="text-slate-700 text-xs mt-3">Set <code class="text-slate-500">TELEGRAM_BOT_TOKEN</code> in Render env vars to activate.</p>
      </div>

      <!-- Tips -->
      <div class="glass rounded-2xl p-5 fade-in">
        <div class="sec-label">💡 Quick Tips</div>
        <ul class="text-slate-500 text-xs space-y-2">
          <li>🔗 <strong class="text-slate-400">Direct URL</strong> — most stable for 24/7</li>
          <li>☁️ <strong class="text-slate-400">Google Drive</strong> — good for large files</li>
          <li>📁 <strong class="text-slate-400">PC Upload</strong> — temporary (lost on restart)</li>
          <li>🎬 <strong class="text-slate-400">YouTube</strong> — requires yt-dlp, ~30s delay</li>
          <li>🔄 <strong class="text-slate-400">Auto-Reconnect</strong> — for 24/7 reliability</li>
          <li>🔑 Keep stream key secret — never share it</li>
        </ul>
      </div>
    </div>
  </div>
</div>

<!-- Toast -->
<div id="toast" class="toast hidden">
  <div id="toast-inner" class="glass-strong rounded-2xl px-5 py-4 text-sm font-semibold shadow-2xl"></div>
</div>

<script>
// ─── State ────────────────────────────────────────────────────────────────────
let activeTab      = 'direct';
let activePlatform = 'youtube';
let activeQuality  = '480p';
let activeFPS      = 30;
let resolvedURLs   = {};  // tab -> resolved URL

// ─── Tab Switching ─────────────────────────────────────────────────────────────
function switchTab(tab) {
  ['direct','upload','gdrive','dropbox','youtube'].forEach(t => {
    document.getElementById('panel-' + t).style.display = t === tab ? '' : 'none';
    document.getElementById('tab-' + t).classList.toggle('active', t === tab);
  });
  activeTab = tab;
}

// ─── Platform Selection ────────────────────────────────────────────────────────
function setPlatform(p) {
  ['youtube','facebook','twitch','custom'].forEach(x => {
    document.getElementById('plat-' + x).classList.toggle('active', x === p);
  });
  activePlatform = p;
  const customWrap = document.getElementById('custom-rtmp-wrap');
  const keyLabel   = document.getElementById('key-label');
  if (p === 'custom') {
    customWrap.classList.remove('hidden');
    keyLabel.textContent = '🔑 Custom RTMP (enter in field below)';
  } else {
    customWrap.classList.add('hidden');
    keyLabel.textContent = '🔑 Stream Key';
  }
}

// ─── Quality Selection ─────────────────────────────────────────────────────────
function setQuality(q) {
  ['480p','720p','1080p','1080p_hq','4k'].forEach(x => {
    document.getElementById('q-' + x).classList.toggle('active', x === q);
  });
  activeQuality = q;
}

// ─── FPS Selection ─────────────────────────────────────────────────────────────
function setFPS(fps) {
  [30, 60].forEach(f => {
    document.getElementById('fps-' + f).classList.toggle('active', f === fps);
  });
  activeFPS = fps;
}

// ─── URL Resolver ──────────────────────────────────────────────────────────────
async function resolveURL(sourceType) {
  const inputId = 'url-' + sourceType;
  const statusId = sourceType + '-resolved';
  const url = document.getElementById(inputId).value.trim();
  if (!url) { showToast('Please enter a URL first.', 'error'); return; }

  const statusEl = document.getElementById(statusId);
  statusEl.textContent = '⏳ Resolving…';
  statusEl.className = 'text-slate-400 text-xs mt-2';
  statusEl.classList.remove('hidden');

  try {
    const res  = await fetch('/resolve-url', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ url, source_type: sourceType }),
    });
    const data = await res.json();
    if (data.success) {
      resolvedURLs[sourceType] = data.resolved_url;
      statusEl.textContent = '✅ Resolved! Ready to stream.';
      statusEl.className = 'text-emerald-400 text-xs mt-2';
      showToast('URL resolved successfully!', 'success');
    } else {
      statusEl.textContent = '❌ ' + data.message;
      statusEl.className = 'text-red-400 text-xs mt-2';
      showToast(data.message, 'error');
    }
  } catch(e) {
    statusEl.textContent = '❌ Network error.';
    statusEl.className = 'text-red-400 text-xs mt-2';
  }
}

// ─── File Upload ───────────────────────────────────────────────────────────────
function handleDrop(ev) {
  ev.preventDefault();
  document.getElementById('upload-zone').classList.remove('drag-over');
  const file = ev.dataTransfer.files[0];
  if (file) uploadFile(file);
}
function handleFileSelect(input) {
  if (input.files[0]) uploadFile(input.files[0]);
}
async function uploadFile(file) {
  const status = document.getElementById('upload-status');
  status.textContent = `⏳ Uploading ${file.name} (${(file.size/1024/1024).toFixed(1)} MB)…`;
  status.classList.remove('hidden');

  const formData = new FormData();
  formData.append('file', file);

  try {
    const res  = await fetch('/upload', { method: 'POST', body: formData });
    const data = await res.json();
    if (data.success) {
      resolvedURLs['upload'] = data.file_path;
      document.getElementById('url-upload').value = data.file_path;
      status.textContent = `✅ ${data.file_name} (${data.size_mb} MB) — Ready!`;
      showToast(`File uploaded: ${data.file_name}`, 'success');
    } else {
      status.textContent = '❌ ' + data.message;
      showToast(data.message, 'error');
    }
  } catch(e) {
    status.textContent = '❌ Upload failed. Check network.';
    showToast('Upload failed.', 'error');
  }
}

// ─── Get Active Video URL ──────────────────────────────────────────────────────
function getVideoURL() {
  if (activeTab === 'direct')  return document.getElementById('url-direct').value.trim();
  if (activeTab === 'upload')  return resolvedURLs['upload'] || '';
  if (activeTab === 'gdrive')  return resolvedURLs['gdrive']  || document.getElementById('url-gdrive').value.trim();
  if (activeTab === 'dropbox') return resolvedURLs['dropbox'] || document.getElementById('url-dropbox').value.trim();
  if (activeTab === 'youtube') return resolvedURLs['youtube'] || document.getElementById('url-youtube').value.trim();
  return '';
}

// ─── Start Stream ──────────────────────────────────────────────────────────────
async function startStream() {
  const url = getVideoURL();
  let key   = document.getElementById('stream-key').value.trim();
  if (activePlatform === 'custom') {
    key = document.getElementById('custom-rtmp').value.trim();
  }

  if (!url) { showToast('Please provide a video source.', 'error'); return; }
  if (!key) { showToast('Please enter your stream key.', 'error'); return; }

  setLoading('btn-start', true);
  try {
    const res  = await fetch('/start', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({
        url,
        key,
        source_type:    activeTab,
        platform:       activePlatform,
        quality:        activeQuality,
        fps:            activeFPS,
        audio_bitrate:  document.getElementById('audio-bitrate').value,
        auto_reconnect: document.getElementById('auto-reconnect').checked,
      }),
    });
    const data = await res.json();
    showToast(data.message, data.success ? 'success' : 'error');
  } catch(e) {
    showToast('Network error. Please try again.', 'error');
  } finally {
    setLoading('btn-start', false);
  }
}

// ─── Stop Stream ───────────────────────────────────────────────────────────────
async function stopStream() {
  setLoading('btn-stop', true);
  try {
    const res  = await fetch('/stop', { method: 'POST' });
    const data = await res.json();
    showToast(data.message, data.success ? 'success' : 'error');
  } catch(e) {
    showToast('Network error. Please try again.', 'error');
  } finally {
    setLoading('btn-stop', false);
  }
}

// ─── Poll Status ───────────────────────────────────────────────────────────────
const QUALITY_LABELS = {'720p':'720p','1080p':'1080p HD','1080p_hq':'1080p HQ','4k':'4K Ultra'};
const PLATFORM_LABELS = {'youtube':'📺 YouTube','facebook':'📘 Facebook','twitch':'🟣 Twitch','custom':'🔧 Custom'};

async function pollStatus() {
  try {
    const res  = await fetch('/api/status');
    const d    = await res.json();
    const dot  = document.getElementById('status-dot');
    const txt  = document.getElementById('status-text');
    const up   = document.getElementById('uptime-text');
    const pid  = document.getElementById('pid-text');
    const card = document.getElementById('stream-info-card');
    const bads = document.getElementById('active-badges');

    if (d.is_active) {
      dot.className  = 'dot-live';
      txt.className  = 'text-2xl font-black text-emerald-400';
      txt.textContent = '🟢 Live Streaming';
      up.textContent  = 'Uptime: ' + d.uptime;
      pid.textContent = d.pid ? 'PID: ' + d.pid : '';
      card.style.display = '';
      document.getElementById('info-platform').textContent = d.platform || '—';
      document.getElementById('info-quality').textContent  = QUALITY_LABELS[d.quality] || d.quality || '—';
      document.getElementById('info-fps').textContent      = (d.fps || '—') + ' FPS';
      const arEl = document.getElementById('info-ar');
      arEl.textContent = d.auto_reconnect ? '✅ ON' : '❌ OFF';
      arEl.className   = d.auto_reconnect ? 'font-semibold text-emerald-400' : 'font-semibold text-slate-600';
      bads.innerHTML  = `<span class="badge bg-emerald-400/10 border border-emerald-400/30 text-emerald-400">${QUALITY_LABELS[d.quality]||d.quality}</span>
                         <span class="badge bg-sky-400/10 border border-sky-400/30 text-sky-400">${d.fps||30} FPS</span>`;
    } else {
      dot.className  = 'dot-offline';
      txt.className  = 'text-2xl font-black text-red-400';
      txt.textContent = '🔴 Offline';
      up.textContent  = 'Uptime: N/A';
      pid.textContent = '';
      card.style.display = 'none';
      bads.innerHTML  = '';
    }

    const cpu = d.cpu_percent || 0;
    const ram = d.ram_percent || 0;
    document.getElementById('cpu-pct').textContent = cpu.toFixed(1) + '%';
    document.getElementById('cpu-bar').style.width = cpu + '%';
    document.getElementById('ram-pct').textContent = ram.toFixed(1) + '%';
    document.getElementById('ram-bar').style.width = ram + '%';
    document.getElementById('ram-detail').textContent = (d.ram_used_mb||0) + ' MB / ' + (d.ram_total_mb||0) + ' MB';
  } catch(e) {
    console.warn('Status poll error:', e);
  }
}

// ─── Helpers ───────────────────────────────────────────────────────────────────
function setLoading(id, on) { document.getElementById(id).disabled = on; }

let toastTimer;
function showToast(msg, type='info') {
  const t  = document.getElementById('toast');
  const ti = document.getElementById('toast-inner');
  const color = {success:'text-emerald-400',error:'text-red-400',info:'text-sky-400'}[type];
  const icon  = {success:'✅',error:'❌',info:'ℹ️'}[type];
  ti.className = `glass-strong rounded-2xl px-5 py-4 text-sm font-semibold shadow-2xl ${color}`;
  ti.textContent = icon + '  ' + msg;
  t.classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add('hidden'), 4500);
}

// ─── Init ──────────────────────────────────────────────────────────────────────
pollStatus();
setInterval(pollStatus, 3000);
</script>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════════════════════
# Telegram Bot
# ═══════════════════════════════════════════════════════════════════════════════

HELP_TEXT = """
🎥 *YouTube 24/7 Live Streamer Bot* — Pro Edition

*Commands:*
/start — Show this help
/stream `<url>` `<key>` — Start with default settings (1080p, YouTube)
/stop — Stop active stream
/status — Show stats & metrics

*Example:*
```
/stream https://cdn.example.com/video.mp4 xxxx-xxxx-xxxx-xxxx
```
Use the web dashboard for advanced options (platform, quality, FPS, cloud sources).
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
        fps      = stream_state.fps
        auto_r   = stream_state.auto_reconnect

    stats  = get_system_stats()
    uptime = get_uptime()
    status_line = (
        f"🟢 *Live* (PID: `{pid}`)\n"
        f"⏱ Uptime: `{uptime}`\n"
        f"📺 Platform: `{platform}` | 🎬 Quality: `{quality}` | 🎞 FPS: `{fps}`\n"
        f"🔄 Auto-Reconnect: `{'ON' if auto_r else 'OFF'}`"
    ) if active else "🔴 *Offline*"

    await update.message.reply_text(
        f"📊 *Stream Status*\n\n{status_line}\n\n"
        f"⚡ CPU: `{stats['cpu_percent']:.1f}%` | "
        f"🧠 RAM: `{stats['ram_percent']:.1f}%` ({stats['ram_used_mb']} MB / {stats['ram_total_mb']} MB)",
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


# ═══════════════════════════════════════════════════════════════════════════════
# Entry Point
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if bot_token:
        threading.Thread(target=run_telegram_bot, args=(bot_token,), daemon=True, name="TelegramBot").start()
        logger.info("Telegram bot thread started.")
    else:
        logger.warning("TELEGRAM_BOT_TOKEN not set — Telegram bot disabled.")

    logger.info("Starting Flask on 0.0.0.0:7860…")
    app.run(host="0.0.0.0", port=7860, debug=False, use_reloader=False, threaded=True)
