# Newsletter Man

A personal newsletter reader that surfaces Gmail messages tagged **Read later**, summarizes them with GPT-4o-mini, and serves them via a local web UI.

## How it works

1. Gmail messages with the **Read later** label are fetched and cached locally (`.newsletter_cache/`).
2. Summaries are generated in the background using `gpt-4o-mini`.
3. A FastAPI server serves the reader UI at `http://127.0.0.1:7431`.
4. The cache syncs every 15 minutes while the server is running.

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in your credentials (see below)
.venv/bin/python main.py
```

### Required environment variables

| Variable | Description |
|---|---|
| `OPENAI_API_KEY` | OpenAI API key for summarization |
| `GOOGLE_CLIENT_ID` | OAuth client ID from GCP project `personal-apis-kdudzik` |
| `GOOGLE_CLIENT_SECRET` | OAuth client secret |
| `GOOGLE_REFRESH_TOKEN` | Long-lived refresh token (Desktop app OAuth flow) |
| `PORT` | Server port (default: `7431`) |

To regenerate `GOOGLE_REFRESH_TOKEN`, run the one-shot script in the project history that uses `InstalledAppFlow.run_local_server`.

## Run as a background daemon (macOS)

```bash
cp com.newsletterman.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.newsletterman.plist
```

Logs: `logs/out.log` and `logs/err.log`.

After code changes, restart to pick up new bytecode:

```bash
launchctl unload ~/Library/LaunchAgents/com.newsletterman.plist \
  && launchctl load ~/Library/LaunchAgents/com.newsletterman.plist
```

## Project structure

| File | Purpose |
|---|---|
| `main.py` | FastAPI app — routes, startup, template filters |
| `gmail_client.py` | Gmail API calls and local JSON cache |
| `summarizer.py` | OpenAI summarization wrapper |
| `templates/` | Jinja2 HTML templates |
| `static/` | CSS / JS assets |
| `com.newsletterman.plist` | launchd agent descriptor |
