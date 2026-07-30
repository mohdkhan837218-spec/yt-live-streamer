#!/bin/bash
# ============================================================
#  YouTube 24/7 Live Streamer — Linux/Mac Run Script
#  Usage: bash run.sh
# ============================================================

set -e

echo ""
echo " ============================================="
echo "  YouTube 24/7 Live Streamer - Starting..."
echo " ============================================="
echo ""

# Check Python
if ! command -v python3 &>/dev/null; then
    echo " [ERROR] python3 not found. Please install Python 3.9+"
    exit 1
fi

# Create venv if not exists
if [ ! -d ".venv" ]; then
    echo " [INFO] Creating virtual environment..."
    python3 -m venv .venv
fi

# Activate venv
echo " [INFO] Activating virtual environment..."
source .venv/bin/activate

# Install dependencies
echo " [INFO] Installing dependencies..."
pip install -r requirements.txt --quiet

# Load .env file if it exists
if [ -f ".env" ]; then
    echo " [INFO] Loading .env file..."
    export $(grep -v "^#" .env | xargs)
fi

# Start the app
echo ""
echo " [INFO] Starting Flask app on http://localhost:7860"
echo " [INFO] Press Ctrl+C to stop"
echo ""
python app.py
