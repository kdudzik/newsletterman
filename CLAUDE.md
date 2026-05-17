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

Single-file FastAPI app with three modules:

- [main.py](main.py) — routes, lifespan (Gmail auth on startup), Jinja2 templates
- [gmail_client.py](gmail_client.py) — all Gmail API calls; `get_service()` builds a one-time authenticated client using static OAuth credentials from env (no token.json, no browser flow)
- [summarizer.py](summarizer.py) — thin wrapper around OpenAI `gpt-4o-mini`

The Gmail service object is created once at startup and held in `_gmail_service` (module-level global in main.py), then passed explicitly into every `gmail_client` function.

## Credentials

Four env vars required in `.env` (see `.env.example`):
- `OPENAI_API_KEY`
- `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` / `GOOGLE_REFRESH_TOKEN`

OAuth credentials come from a GCP project with Gmail API enabled. The OAuth client is a Desktop app type. To regenerate `GOOGLE_REFRESH_TOKEN`, run the one-shot script in the project history (uses `InstalledAppFlow.run_local_server`).

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
