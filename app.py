"""
YouTube 24/7 Live Streamer — app.py
====================================
A Flask web application + Telegram Bot that manages a continuous FFmpeg
live-stream to YouTube. Designed for deployment on Hugging Face Spaces
using the Docker SDK (port 7860).

Architecture
------------
  ┌─────────────────────────────────────────────────────┐
  │  Main Thread: Flask (port 7860)                     │
  │  Background Thread: python-telegram-bot asyncio loop│
  │  Background Process: ffmpeg subprocess              │
  └─────────────────────────────────────────────────────┘

Environment Variables (set in HF Space Secrets)
------------------------------------------------
  TELEGRAM_BOT_TOKEN   — Required for Telegram bot functionality.
  ALLOWED_CHAT_ID      — (Optional) Restrict bot to a specific Telegram chat.
"""

import os
import signal
import subprocess
import threading
import time
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import psutil
from flask import Flask, jsonify, render_template_string, request

# ─── Telegram Bot imports (python-telegram-bot v20+) ─────────────────────────
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# ─── Logging Setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("yt-streamer")

# ─── Flask App ─────────────────────────────────────────────────────────────────
app = Flask(__name__)

# ─── Global Streaming State ────────────────────────────────────────────────────
_state_lock = threading.Lock()

class StreamState:
    """Thread-safe container for all streaming state."""
    process: Optional[subprocess.Popen] = None
    video_url: str = ""
    stream_key: str = ""
    started_at: Optional[datetime] = None
    is_active: bool = False

stream_state = StreamState()


# ═══════════════════════════════════════════════════════════════════════════════
# FFmpeg Process Management
# ═══════════════════════════════════════════════════════════════════════════════

def build_ffmpeg_command(video_url: str, stream_key: str) -> list[str]:
    """Construct the FFmpeg command for YouTube RTMP streaming."""
    rtmp_url = f"rtmp://a.rtmp.youtube.com/live2/{stream_key}"
    return [
        "ffmpeg",
        "-re",
        "-stream_loop", "-1",
        "-i", video_url,
        "-c:v", "libx264",
        "-preset", "superfast",
        "-b:v", "2500k",
        "-maxrate", "2500k",
        "-bufsize", "5000k",
        "-pix_fmt", "yuv420p",
        "-g", "60",
        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "44100",
        "-f", "flv",
        rtmp_url,
    ]


def _monitor_process():
    """Background watcher: marks stream inactive when ffmpeg exits unexpectedly."""
    with _state_lock:
        proc = stream_state.process

    if proc is None:
        return

    proc.wait()  # blocks until ffmpeg exits

    with _state_lock:
        # Only clear state if this is still the tracked process
        if stream_state.process is proc:
            logger.warning("FFmpeg process exited (PID=%s). Stream marked offline.", proc.pid)
            stream_state.process = None
            stream_state.is_active = False
            stream_state.started_at = None


def start_stream(video_url: str, stream_key: str) -> tuple[bool, str]:
    """
    Launch an FFmpeg subprocess for continuous YouTube live streaming.

    Returns (success: bool, message: str).
    """
    with _state_lock:
        if stream_state.is_active and stream_state.process:
            return False, "A stream is already running. Stop it first."

    cmd = build_ffmpeg_command(video_url, stream_key)
    logger.info("Starting FFmpeg: %s", " ".join(cmd[:6]) + " ...")

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,  # capture stderr for debugging
            preexec_fn=os.setsid if hasattr(os, "setsid") else None,
        )
    except FileNotFoundError:
        return False, "FFmpeg not found. Ensure it is installed in the container."
    except Exception as exc:
        logger.exception("Failed to launch FFmpeg")
        return False, f"Error launching FFmpeg: {exc}"

    with _state_lock:
        stream_state.process = proc
        stream_state.video_url = video_url
        stream_state.stream_key = stream_key
        stream_state.started_at = datetime.now(timezone.utc)
        stream_state.is_active = True

    # Start a daemon thread to watch for unexpected process exit
    watcher = threading.Thread(target=_monitor_process, daemon=True)
    watcher.start()

    logger.info("FFmpeg started successfully (PID=%s)", proc.pid)
    return True, f"Stream started (PID={proc.pid})."


def stop_stream() -> tuple[bool, str]:
    """
    Terminate the active FFmpeg streaming process.

    Returns (success: bool, message: str).
    """
    with _state_lock:
        if not stream_state.is_active or stream_state.process is None:
            return False, "No active stream to stop."

        proc = stream_state.process

    logger.info("Stopping FFmpeg (PID=%s)…", proc.pid)
    try:
        # Kill the entire process group to ensure child processes are also terminated
        if hasattr(os, "killpg"):
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        else:
            proc.terminate()

        # Give it 5 seconds to exit gracefully, then force-kill
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if hasattr(os, "killpg"):
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            else:
                proc.kill()
            proc.wait()
    except (ProcessLookupError, PermissionError):
        pass  # Process already gone

    with _state_lock:
        stream_state.process = None
        stream_state.is_active = False
        stream_state.started_at = None
        stream_state.video_url = ""
        stream_state.stream_key = ""

    logger.info("Stream stopped successfully.")
    return True, "Stream stopped."


def get_uptime() -> str:
    """Return a human-readable uptime string if the stream is active."""
    with _state_lock:
        started = stream_state.started_at

    if started is None:
        return "N/A"

    delta = datetime.now(timezone.utc) - started
    total_seconds = int(delta.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}h {minutes:02d}m {seconds:02d}s"


def get_system_stats() -> dict:
    """Return current CPU and RAM usage as a dict."""
    return {
        "cpu_percent": psutil.cpu_percent(interval=0.1),
        "ram_percent": psutil.virtual_memory().percent,
        "ram_used_mb": round(psutil.virtual_memory().used / (1024 ** 2), 1),
        "ram_total_mb": round(psutil.virtual_memory().total / (1024 ** 2), 1),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Flask Routes
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    """Serve the main Web Dashboard."""
    return render_template_string(DASHBOARD_HTML)


@app.route("/start", methods=["POST"])
def route_start():
    """POST /start — Launch FFmpeg streaming process."""
    data = request.get_json(silent=True) or request.form
    video_url = (data.get("url") or "").strip()
    stream_key = (data.get("key") or "").strip()

    if not video_url:
        return jsonify({"success": False, "message": "Video URL is required."}), 400
    if not stream_key:
        return jsonify({"success": False, "message": "Stream key is required."}), 400

    success, message = start_stream(video_url, stream_key)
    status_code = 200 if success else 409
    return jsonify({"success": success, "message": message}), status_code


@app.route("/stop", methods=["POST"])
def route_stop():
    """POST /stop — Terminate the active FFmpeg process."""
    success, message = stop_stream()
    return jsonify({"success": success, "message": message}), 200


@app.route("/api/status")
def route_status():
    """GET /api/status — Returns current streaming status + system metrics."""
    with _state_lock:
        active = stream_state.is_active
        pid = stream_state.process.pid if stream_state.process else None
        url = stream_state.video_url
        key_masked = ("*" * 6 + stream_state.stream_key[-4:]) if len(stream_state.stream_key) > 4 else "****"

    stats = get_system_stats()
    return jsonify({
        "is_active": active,
        "pid": pid,
        "uptime": get_uptime(),
        "video_url": url if active else "",
        "stream_key_masked": key_masked if active else "",
        **stats,
    })


# ═══════════════════════════════════════════════════════════════════════════════
# Dashboard HTML (Tailwind CSS via CDN — dark mode, glassmorphism)
# ═══════════════════════════════════════════════════════════════════════════════

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en" class="dark">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>YouTube 24/7 Live Streamer</title>
  <meta name="description" content="Control panel for 24/7 automated YouTube live streaming via FFmpeg on Hugging Face Spaces." />
  <script src="https://cdn.tailwindcss.com"></script>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap" rel="stylesheet" />
  <script>
    tailwind.config = {
      darkMode: 'class',
      theme: {
        extend: {
          fontFamily: { sans: ['Inter', 'sans-serif'] },
          colors: {
            brand: { 50:'#f0f7ff', 100:'#e0effe', 200:'#b9dffd', 300:'#7cc4fc', 400:'#36a9f8', 500:'#0c8ee8', 600:'#0270c6', 700:'#0259a1', 800:'#064b85', 600:'#093f6e' },
          },
          animation: {
            'pulse-slow': 'pulse 3s cubic-bezier(0.4,0,0.6,1) infinite',
            'fade-in': 'fadeIn 0.4s ease-in-out',
          },
          keyframes: {
            fadeIn: { '0%': { opacity: 0, transform: 'translateY(8px)' }, '100%': { opacity: 1, transform: 'translateY(0)' } },
          },
        },
      },
    };
  </script>
  <style>
    body { background: linear-gradient(135deg, #0a0f1e 0%, #0d1b3e 50%, #0a1628 100%); min-height: 100vh; }
    .glass { background: rgba(255,255,255,0.04); backdrop-filter: blur(16px); border: 1px solid rgba(255,255,255,0.08); }
    .glow-blue { box-shadow: 0 0 30px rgba(56,189,248,0.15), 0 0 60px rgba(56,189,248,0.05); }
    .status-dot { width:12px; height:12px; border-radius:50%; display:inline-block; }
    .dot-live { background:#22c55e; box-shadow:0 0 10px #22c55e, 0 0 20px rgba(34,197,94,0.5); animation: pulse 1.5s infinite; }
    .dot-offline { background:#ef4444; box-shadow:0 0 8px #ef4444; }
    .input-field { background:rgba(255,255,255,0.05); border:1px solid rgba(255,255,255,0.12); color:#e2e8f0; transition:all 0.2s; }
    .input-field:focus { outline:none; border-color:rgba(56,189,248,0.6); box-shadow:0 0 0 3px rgba(56,189,248,0.1); background:rgba(255,255,255,0.07); }
    .input-field::placeholder { color: rgba(148,163,184,0.6); }
    .btn-start { background: linear-gradient(135deg, #059669 0%, #047857 100%); transition: all 0.2s; }
    .btn-start:hover:not(:disabled) { background: linear-gradient(135deg, #10b981 0%, #059669 100%); transform:translateY(-1px); box-shadow:0 8px 25px rgba(5,150,105,0.4); }
    .btn-stop { background: linear-gradient(135deg, #dc2626 0%, #b91c1c 100%); transition: all 0.2s; }
    .btn-stop:hover:not(:disabled) { background: linear-gradient(135deg, #ef4444 0%, #dc2626 100%); transform:translateY(-1px); box-shadow:0 8px 25px rgba(220,38,38,0.4); }
    button:disabled { opacity:0.5; cursor:not-allowed; transform:none !important; }
    .metric-card { background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.06); }
    .progress-bar { height:6px; border-radius:3px; background:rgba(255,255,255,0.1); overflow:hidden; }
    .progress-fill { height:100%; border-radius:3px; transition: width 0.5s ease; }
    .toast { position:fixed; bottom:24px; right:24px; z-index:100; min-width:280px; animation:fadeIn 0.3s ease; }
  </style>
</head>
<body class="font-sans text-slate-100 antialiased">

  <!-- Background grid pattern -->
  <div class="fixed inset-0 opacity-5 pointer-events-none" style="background-image:radial-gradient(circle,#4f9cf8 1px,transparent 1px);background-size:40px 40px;"></div>

  <div class="relative min-h-screen p-4 md:p-8">
    <!-- Header -->
    <header class="text-center mb-10 animate-fade-in">
      <div class="inline-flex items-center gap-3 mb-3">
        <span class="text-4xl">🎥</span>
        <h1 class="text-4xl md:text-5xl font-extrabold bg-gradient-to-r from-sky-400 via-blue-400 to-indigo-400 bg-clip-text text-transparent">
          YouTube 24/7 Streamer
        </h1>
      </div>
      <p class="text-slate-400 text-lg">Powered by FFmpeg · Hugging Face Spaces</p>
    </header>

    <div class="max-w-4xl mx-auto space-y-6">

      <!-- Status Card -->
      <div class="glass rounded-2xl p-6 glow-blue animate-fade-in">
        <div class="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4">
          <div>
            <p class="text-xs font-semibold uppercase tracking-widest text-slate-500 mb-2">Stream Status</p>
            <div class="flex items-center gap-3">
              <span id="status-dot" class="status-dot dot-offline"></span>
              <span id="status-text" class="text-2xl font-bold text-red-400">🔴 Offline</span>
            </div>
            <p class="text-slate-500 mt-1 text-sm" id="uptime-text">Uptime: N/A</p>
          </div>
          <div class="text-right">
            <p class="text-xs text-slate-600 mb-1">Process ID</p>
            <p id="pid-text" class="text-slate-400 font-mono text-sm">—</p>
          </div>
        </div>
      </div>

      <!-- System Metrics -->
      <div class="grid grid-cols-1 sm:grid-cols-2 gap-4 animate-fade-in">
        <!-- CPU -->
        <div class="glass metric-card rounded-2xl p-5">
          <div class="flex justify-between items-center mb-3">
            <span class="text-slate-400 text-sm font-medium">⚡ CPU Usage</span>
            <span id="cpu-pct" class="text-sky-400 font-bold text-lg">0%</span>
          </div>
          <div class="progress-bar">
            <div id="cpu-bar" class="progress-fill bg-gradient-to-r from-sky-500 to-blue-500" style="width:0%"></div>
          </div>
        </div>
        <!-- RAM -->
        <div class="glass metric-card rounded-2xl p-5">
          <div class="flex justify-between items-center mb-3">
            <span class="text-slate-400 text-sm font-medium">🧠 RAM Usage</span>
            <span id="ram-pct" class="text-violet-400 font-bold text-lg">0%</span>
          </div>
          <div class="progress-bar">
            <div id="ram-bar" class="progress-fill bg-gradient-to-r from-violet-500 to-purple-500" style="width:0%"></div>
          </div>
          <p id="ram-detail" class="text-slate-600 text-xs mt-2">0 MB / 0 MB</p>
        </div>
      </div>

      <!-- Control Panel -->
      <div class="glass rounded-2xl p-6 animate-fade-in">
        <h2 class="text-lg font-semibold text-slate-200 mb-5 flex items-center gap-2">
          <span>🎛️</span> Stream Control
        </h2>
        <div class="space-y-4">
          <div>
            <label for="video-url" class="block text-sm font-medium text-slate-400 mb-2">
              📹 Direct Video Link <span class="text-slate-600">(MP4/MKV/direct URL)</span>
            </label>
            <input
              id="video-url"
              type="url"
              placeholder="https://example.com/video.mp4"
              class="input-field w-full rounded-xl px-4 py-3 text-sm"
            />
          </div>
          <div>
            <label for="stream-key" class="block text-sm font-medium text-slate-400 mb-2">
              🔑 YouTube Stream Key
            </label>
            <input
              id="stream-key"
              type="password"
              placeholder="xxxx-xxxx-xxxx-xxxx-xxxx"
              class="input-field w-full rounded-xl px-4 py-3 text-sm font-mono"
            />
          </div>
          <div class="grid grid-cols-2 gap-4 pt-2">
            <button
              id="btn-start"
              onclick="startStream()"
              class="btn-start rounded-xl py-3.5 px-6 font-semibold text-white text-sm flex items-center justify-center gap-2"
            >
              <span>▶️</span> Start 24/7 Stream
            </button>
            <button
              id="btn-stop"
              onclick="stopStream()"
              class="btn-stop rounded-xl py-3.5 px-6 font-semibold text-white text-sm flex items-center justify-center gap-2"
            >
              <span>⏹️</span> Stop Stream
            </button>
          </div>
        </div>
      </div>

      <!-- Info Footer -->
      <div class="glass rounded-2xl p-5 animate-fade-in">
        <h3 class="text-sm font-semibold text-slate-400 mb-3 flex items-center gap-2"><span>🤖</span> Telegram Bot Commands</h3>
        <div class="grid grid-cols-1 sm:grid-cols-2 gap-2 text-xs font-mono">
          <div class="metric-card rounded-lg p-3"><span class="text-sky-400">/start</span> <span class="text-slate-500">— Show help & manual</span></div>
          <div class="metric-card rounded-lg p-3"><span class="text-sky-400">/stream &lt;url&gt; &lt;key&gt;</span> <span class="text-slate-500">— Start stream</span></div>
          <div class="metric-card rounded-lg p-3"><span class="text-sky-400">/stop</span> <span class="text-slate-500">— Stop active stream</span></div>
          <div class="metric-card rounded-lg p-3"><span class="text-sky-400">/status</span> <span class="text-slate-500">— Show status & metrics</span></div>
        </div>
      </div>

    </div>
  </div>

  <!-- Toast Notification -->
  <div id="toast" class="toast hidden">
    <div id="toast-inner" class="glass rounded-xl px-5 py-4 text-sm font-medium shadow-2xl"></div>
  </div>

  <script>
    let statusPollInterval = null;

    function showToast(message, type = 'info') {
      const toast = document.getElementById('toast');
      const inner = document.getElementById('toast-inner');
      const colors = { success: 'text-emerald-400', error: 'text-red-400', info: 'text-sky-400' };
      const icons  = { success: '✅', error: '❌', info: 'ℹ️' };
      inner.className = `glass rounded-xl px-5 py-4 text-sm font-medium shadow-2xl ${colors[type]}`;
      inner.textContent = icons[type] + '  ' + message;
      toast.classList.remove('hidden');
      setTimeout(() => toast.classList.add('hidden'), 4000);
    }

    function setLoading(btn, loading) {
      const el = document.getElementById(btn);
      el.disabled = loading;
    }

    async function startStream() {
      const url = document.getElementById('video-url').value.trim();
      const key = document.getElementById('stream-key').value.trim();
      if (!url) { showToast('Please enter a video URL.', 'error'); return; }
      if (!key) { showToast('Please enter a stream key.', 'error'); return; }
      setLoading('btn-start', true);
      try {
        const res  = await fetch('/start', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ url, key }),
        });
        const data = await res.json();
        showToast(data.message, data.success ? 'success' : 'error');
        if (data.success) pollStatus();
      } catch (e) {
        showToast('Network error. Please try again.', 'error');
      } finally {
        setLoading('btn-start', false);
      }
    }

    async function stopStream() {
      setLoading('btn-stop', true);
      try {
        const res  = await fetch('/stop', { method: 'POST' });
        const data = await res.json();
        showToast(data.message, data.success ? 'success' : 'error');
      } catch (e) {
        showToast('Network error. Please try again.', 'error');
      } finally {
        setLoading('btn-stop', false);
      }
    }

    async function pollStatus() {
      try {
        const res  = await fetch('/api/status');
        const data = await res.json();

        const dot  = document.getElementById('status-dot');
        const text = document.getElementById('status-text');
        const up   = document.getElementById('uptime-text');
        const pid  = document.getElementById('pid-text');

        if (data.is_active) {
          dot.className  = 'status-dot dot-live';
          text.className = 'text-2xl font-bold text-emerald-400';
          text.textContent = '🟢 Streaming Live';
          up.textContent  = 'Uptime: ' + data.uptime;
          pid.textContent = data.pid ? 'PID: ' + data.pid : '—';
        } else {
          dot.className  = 'status-dot dot-offline';
          text.className = 'text-2xl font-bold text-red-400';
          text.textContent = '🔴 Offline';
          up.textContent  = 'Uptime: N/A';
          pid.textContent = '—';
        }

        // Update metrics
        const cpu = data.cpu_percent || 0;
        const ram = data.ram_percent || 0;
        document.getElementById('cpu-pct').textContent = cpu.toFixed(1) + '%';
        document.getElementById('cpu-bar').style.width = cpu + '%';
        document.getElementById('ram-pct').textContent = ram.toFixed(1) + '%';
        document.getElementById('ram-bar').style.width = ram + '%';
        document.getElementById('ram-detail').textContent =
          data.ram_used_mb + ' MB / ' + data.ram_total_mb + ' MB';

      } catch (e) {
        console.error('Status poll failed:', e);
      }
    }

    // Auto-poll every 3 seconds
    pollStatus();
    statusPollInterval = setInterval(pollStatus, 3000);
  </script>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════════════════════
# Telegram Bot Handlers
# ═══════════════════════════════════════════════════════════════════════════════

HELP_TEXT = """
🎥 *YouTube 24/7 Live Streamer Bot*

I manage an FFmpeg process that continuously streams a video file to your YouTube Live channel.

*Available Commands:*

/start — Show this help message
/stream `<video_url>` `<stream_key>` — Start a 24/7 live stream
/stop — Stop the active live stream
/status — Show current stream status and system metrics

*Example:*
```
/stream https://example.com/video.mp4 xxxx-xxxx-xxxx-xxxx
```

⚠️ Keep your stream key secret! Messages are deleted after processing when possible.
"""


async def tg_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Telegram /start handler — display help."""
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")


async def tg_stream(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Telegram /stream <url> <key> handler."""
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "❌ Usage: `/stream <video_url> <stream_key>`",
            parse_mode="Markdown",
        )
        return

    video_url = args[0].strip()
    stream_key = args[1].strip()

    await update.message.reply_text("⏳ Launching FFmpeg stream, please wait…")
    success, message = start_stream(video_url, stream_key)
    icon = "✅" if success else "❌"
    await update.message.reply_text(f"{icon} {message}")


async def tg_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Telegram /stop handler."""
    success, message = stop_stream()
    icon = "✅" if success else "❌"
    await update.message.reply_text(f"{icon} {message}")


async def tg_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Telegram /status handler — report stream status and system metrics."""
    with _state_lock:
        active = stream_state.is_active
        pid    = stream_state.process.pid if stream_state.process else None

    stats  = get_system_stats()
    uptime = get_uptime()

    if active:
        status_line = f"🟢 *Live Streaming* (PID: `{pid}`)\n⏱ Uptime: `{uptime}`"
    else:
        status_line = "🔴 *Offline* — No stream is running."

    text = (
        f"📊 *Stream Status*\n\n"
        f"{status_line}\n\n"
        f"*System Resources:*\n"
        f"⚡ CPU: `{stats['cpu_percent']:.1f}%`\n"
        f"🧠 RAM: `{stats['ram_percent']:.1f}%` "
        f"({stats['ram_used_mb']} MB / {stats['ram_total_mb']} MB)"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════════════════════════
# Telegram Bot Thread Bootstrap
# ═══════════════════════════════════════════════════════════════════════════════

def run_telegram_bot(token: str) -> None:
    """
    Build and run the Telegram Application in its own asyncio event loop.
    This function is designed to be executed in a daemon thread.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    application = (
        Application.builder()
        .token(token)
        .build()
    )

    application.add_handler(CommandHandler("start",  tg_start))
    application.add_handler(CommandHandler("stream", tg_stream))
    application.add_handler(CommandHandler("stop",   tg_stop))
    application.add_handler(CommandHandler("status", tg_status))

    logger.info("Telegram bot started (polling).")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        close_loop=False,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Entry Point
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # ── Start Telegram bot in a background thread (if token is configured) ─────
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if bot_token:
        bot_thread = threading.Thread(
            target=run_telegram_bot,
            args=(bot_token,),
            daemon=True,
            name="TelegramBotThread",
        )
        bot_thread.start()
        logger.info("Telegram bot thread launched.")
    else:
        logger.warning(
            "TELEGRAM_BOT_TOKEN is not set. "
            "Telegram bot will not start. Set it in HF Space Secrets."
        )

    # ── Start Flask (blocking — must be last) ─────────────────────────────────
    logger.info("Starting Flask on 0.0.0.0:7860 …")
    app.run(
        host="0.0.0.0",
        port=7860,
        debug=False,
        use_reloader=False,   # Disable reloader (conflicts with threading)
        threaded=True,        # Allow concurrent Flask request handling
    )
