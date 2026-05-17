# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the server

```bash
.venv/bin/python main.py
# runs on http://127.0.0.1:7431
```

Install dependencies:
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

## Architecture

Single-file FastAPI app with several modules:

- [main.py](main.py) — routes, lifespan (Gmail auth on startup), Jinja2 templates
- [gmail_client.py](gmail_client.py) — all Gmail API calls; `get_service()` builds a one-time authenticated client using static OAuth credentials from env (no token.json, no browser flow)
- [raindrop_client.py](raindrop_client.py) — Raindrop.io API calls, local JSON cache, archive/unread management
- [wyborcza_client.py](wyborcza_client.py) — Wyborcza.pl Schowek sync and authenticated article fetching via browser cookies
- [youtube_client.py](youtube_client.py) — YouTube Watch Later sync via Innertube API (cookie auth), transcript/description fetching, WL removal
- [summarizer.py](summarizer.py) — OpenAI summarization wrapper with language detection and video/article/newsletter prompt variants
- [scorer.py](scorer.py) — GPT-4o-mini scoring for relevance, challenge, and political lean

The Gmail service object is created once at startup and held in `_gmail_service` (module-level global in main.py), then passed explicitly into every `gmail_client` function.

Entry IDs are prefixed by source: `raindrop-*`, `wyborcza-*`, `youtube-*`, bare IDs for Gmail. `_load_any` / `_save_any` / `_is_*` helpers in main.py route by prefix.

## Credentials

Required in `.env` (see `.env.example`):
- `OPENAI_API_KEY`
- `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` / `GOOGLE_REFRESH_TOKEN`

OAuth credentials come from a GCP project with Gmail API enabled. The OAuth client is a Desktop app type. To regenerate `GOOGLE_REFRESH_TOKEN` (covers Gmail + YouTube scopes), run:
```bash
.venv/bin/python get_token.py
```

## Always-on daemon

[com.newsletterman.plist](com.newsletterman.plist) is a launchd agent. Install with:
```bash
cp com.newsletterman.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.newsletterman.plist
```
Logs go to `logs/out.log` and `logs/err.log`.

Restart after code changes (launchd runs stale bytecode otherwise):
```bash
launchctl unload ~/Library/LaunchAgents/com.newsletterman.plist && launchctl load ~/Library/LaunchAgents/com.newsletterman.plist
```
