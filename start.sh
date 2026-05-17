#!/bin/bash
# ─────────────────────────────────────────────────────────
#  ORB + VWAP Auto-Trader — Mac Startup Script
#  Run this once to install deps, then every morning to start
# ─────────────────────────────────────────────────────────

set -e
cd "$(dirname "$0")"

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║     ORB + VWAP Auto-Trader  |  Upstox API v2        ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""

# Check Python 3
if ! command -v python3 &> /dev/null; then
  echo "❌  Python 3 not found. Install from https://python.org"
  exit 1
fi

PYTHON=$(command -v python3)
echo "✅  Python: $($PYTHON --version)"

# Create virtualenv if needed
if [ ! -d "venv" ]; then
  echo "📦  Creating virtual environment..."
  $PYTHON -m venv venv
fi

source venv/bin/activate

# Install / upgrade deps
echo "📦  Installing dependencies..."
pip install -q --upgrade pip
pip install -q -r requirements.txt
echo "✅  Dependencies ready"

# Check credentials
if grep -q "YOUR_CLIENT_ID_HERE" app.py; then
  echo ""
  echo "⚠️   IMPORTANT: You haven't set your Upstox API credentials yet!"
  echo "     Option A — Edit app.py lines 24–25:"
  echo "       CLIENT_ID     = 'your_actual_client_id'"
  echo "       CLIENT_SECRET = 'your_actual_client_secret'"
  echo ""
  echo "     Option B — Set environment variables (more secure):"
  echo "       export UPSTOX_CLIENT_ID=your_client_id"
  echo "       export UPSTOX_CLIENT_SECRET=your_client_secret"
  echo ""
  echo "     Get credentials: https://developer.upstox.com"
  echo "     Redirect URI must be: http://localhost:8080/callback"
  echo ""
  read -p "Press Enter to start anyway (backtest won't work until credentials are set)..."
fi

echo ""
echo "🚀  Starting server on http://localhost:8080"
echo "    Open your browser and go to: http://localhost:8080"
echo "    Then click 'Login with Upstox' to authorise."
echo ""
echo "    Press Ctrl+C to stop the server."
echo ""

python app.py
