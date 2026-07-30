---
title: YouTube 24/7 Live Streamer
emoji: 🎥
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# 🎥 YouTube 24/7 Live Streamer

A production-ready, self-hosted tool that runs a **continuous 24/7 YouTube Live Stream** from any direct video URL using **FFmpeg**, controllable via a sleek **Web Dashboard** and a **Telegram Bot**.

Deployed on Hugging Face Spaces using the **Docker SDK**.

---

## 🚀 Features

| Feature | Details |
|---|---|
| **Continuous Streaming** | `ffmpeg -stream_loop -1` for infinite looping |
| **Web Dashboard** | Dark-mode UI with real-time status polling every 3 seconds |
| **Telegram Bot** | Full stream control from any device via Telegram |
| **System Metrics** | Live CPU & RAM usage displayed in the dashboard and bot |
| **Process Safety** | Process group kill ensures no zombie FFmpeg instances |
| **HF Spaces Ready** | Serves on port `7860`, Docker SDK configured |

---

## 🛠️ Setup Guide

### Step 1 — Fork / Duplicate this Space

Click **"Duplicate this Space"** on Hugging Face to get your own private copy.

### Step 2 — Configure Secrets (Required)

Go to your Space → **Settings** → **Repository secrets** and add:

| Secret Name | Value | Required |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Your bot token from [@BotFather](https://t.me/BotFather) | Optional |
| `ALLOWED_CHAT_ID` | Your Telegram chat ID (for security) | Optional |

> **Note:** The web dashboard works without any secrets. The Telegram bot only starts if `TELEGRAM_BOT_TOKEN` is set.

### Step 3 — Start a Stream

**Via Web Dashboard:**
1. Open the Space URL
2. Enter your **Direct Video URL** (must be a direct link, e.g. `.mp4`)
3. Enter your **YouTube Stream Key** (from YouTube Studio → Go Live → Stream Key)
4. Click **"Start 24/7 Stream"**

**Via Telegram Bot:**
```
/stream https://your-video-url.com/video.mp4 your-youtube-stream-key
```

---

## 🤖 Telegram Bot Commands

| Command | Description |
|---|---|
| `/start` | Show help message and user manual |
| `/stream <url> <key>` | Launch the 24/7 FFmpeg stream |
| `/stop` | Terminate the active stream |
| `/status` | Show stream status, uptime, CPU & RAM |

---

## ⚙️ FFmpeg Parameters Explained

```
ffmpeg
  -re                          # Read input at native frame rate
  -stream_loop -1              # Loop the input indefinitely
  -i "<VIDEO_URL>"             # Input source
  -c:v libx264                 # H.264 video codec
  -preset superfast            # Encoding speed vs compression trade-off
  -b:v 2500k                   # Video bitrate (2.5 Mbps)
  -maxrate 2500k               # Max bitrate cap
  -bufsize 5000k               # Bitrate buffer (2x bitrate)
  -pix_fmt yuv420p             # Pixel format (YouTube compatible)
  -g 60                        # Keyframe interval (2s @ 30fps)
  -c:a aac                     # AAC audio codec
  -b:a 128k                    # Audio bitrate
  -ar 44100                    # Audio sample rate
  -f flv                       # RTMP container format
  "rtmp://a.rtmp.youtube.com/live2/<STREAM_KEY>"
```

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Docker Container (python:3.10-slim + ffmpeg)           │
│                                                         │
│  ┌──────────────────────┐  ┌───────────────────────┐   │
│  │  Flask Web Server    │  │  Telegram Bot         │   │
│  │  (Main Thread)       │  │  (Background Thread)  │   │
│  │  Port 7860           │  │  Asyncio Event Loop   │   │
│  └──────────┬───────────┘  └───────────┬───────────┘   │
│             │                          │               │
│             └─────────────┬────────────┘               │
│                           ▼                            │
│             ┌─────────────────────────┐                │
│             │  FFmpeg Subprocess      │                │
│             │  (Background Process)   │                │
│             │  RTMP → YouTube Live    │                │
│             └─────────────────────────┘                │
└─────────────────────────────────────────────────────────┘
```

---

## 📁 Repository Structure

```
yt-live-streamer/
├── app.py              # Main Flask + Telegram Bot application
├── requirements.txt    # Python dependencies
├── Dockerfile          # Docker image definition for HF Spaces
└── README.md           # This file (HF Spaces metadata + documentation)
```

---

## ⚠️ Important Notes

- **Stream Key Security:** Your YouTube stream key grants direct access to your YouTube channel. Never share it publicly. Use Telegram secrets instead of pasting in public chats.
- **Video URL:** Must be a **direct download link** to a video file (`.mp4`, `.mkv`, etc.) or an HLS/DASH stream URL that FFmpeg can read directly. YouTube URLs will **not** work directly without `yt-dlp`.
- **Hugging Face Spaces Limits:** Free tier Spaces may sleep after inactivity. Consider upgrading to a persistent Space for true 24/7 operation.
- **Graceful Restart:** If the Space restarts, you will need to re-enter your stream key via the dashboard or Telegram bot (keys are never persisted to disk for security).

---

## 📄 License

MIT License — Free to use, modify, and deploy.
