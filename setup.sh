#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
REPO_DIR="$(pwd)"

echo "==> Creating virtualenv"
python3 -m venv .venv

echo "==> Installing dependencies"
.venv/bin/pip install -r requirements.txt

echo "==> Creating logs dir"
mkdir -p logs

echo "==> Copying env file"
[ -f .env ] || cp .env.example .env

echo "==> Generating launchd plist"
sed "s|__REPO_DIR__|${REPO_DIR}|g" com.newsletterman.plist.template > com.newsletterman.plist

echo ""
echo "Next steps:"
echo "  1. Edit .env and fill in your API keys (see .env.example for details)"
echo "  2. Install the launch daemon:"
echo "       cp com.newsletterman.plist ~/Library/LaunchAgents/"
echo "       launchctl load ~/Library/LaunchAgents/com.newsletterman.plist"
echo ""
echo "Server will run at http://127.0.0.1:7431"
