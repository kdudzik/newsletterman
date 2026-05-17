# Newsletter Man

A personal newsletter reader that surfaces Gmail messages tagged **Read later**, summarizes them with GPT-4o-mini, and serves them via a local web UI.

## How it works

1. Gmail messages with the **Read later** label are fetched and cached locally (`.newsletter_cache/`).
2. Summaries are generated automatically in the background using `gpt-4o-mini`.
3. A FastAPI server serves the reader UI at `http://127.0.0.1:7431`.
4. The cache syncs every 60 seconds while the server is running.

## Features

- **Home feed** — all newsletters sorted newest first, with sender name, relative timestamp, estimated read time, and a one-line snippet.
- **Detail view** — full newsletter text with a GPT-4o-mini summary (bullet points, auto-rendered as HTML). Links back to the original Gmail thread.
- **On-demand summarize** — if a summary hasn't been generated yet, the detail page triggers it via a POST and renders the result inline.
- **Mark done / unread** — removes or restores the "Read later" label in Gmail directly from the UI (no need to open Gmail).
- **Manual refresh** — a refresh button on the home page syncs the label from Gmail immediately and kicks off summarization for any new entries.
- **Language detection** — summaries are written in Polish when the newsletter text is Polish, English otherwise.
- **Author aliases** — long sender names (e.g. "James Stanier from The Engineering Manager") can be mapped to short display names in `config.py`.
- **Subject filter** — set `NEWSLETTER_EXCLUDE_SUBJECT` to drop matching newsletters from the feed without touching Gmail.
- **Hot-reload** — the dev server watches `*.css`, `*.html`, and `*.js` for changes.
- **Personal relevance scoring** — if a `personal_context.md` file is present (describing your values, worldview, and priorities), each newsletter is scored 0–10 on **relevance** (alignment with your interests) and **challenge** (how much it tensions your worldview). Scores appear as instant-tooltip badges on cards. The sort group gains **Most relevant** and **Most challenging** options.

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

### Optional environment variables

| Variable | Description |
|---|---|
| `GMAIL_READ_LATER_LABEL` | Gmail label to watch (default: `Read later`) |
| `NEWSLETTER_EXCLUDE_SUBJECT` | Drop newsletters whose subject contains this string |
| `PERSONAL_CONTEXT_FILE` | Path to a markdown file describing your values and worldview for relevance scoring (default: `personal_context.md` if it exists) |

To regenerate `GOOGLE_REFRESH_TOKEN`, run the one-shot script in the project history that uses `InstalledAppFlow.run_local_server`.

## Customizing author names

Edit `config.py` to add display-name overrides:

```python
AUTHOR_ALIASES: dict[str, str] = {
    "James Stanier from The Engineering Manager": "James Stanier",
}
```

The key is the raw sender name extracted from the `From` header; the value is what the UI displays.

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
| `main.py` | FastAPI app — routes, lifespan, template filters, markdown rendering |
| `gmail_client.py` | Gmail API calls, local JSON cache, label management |
| `summarizer.py` | OpenAI summarization wrapper with language detection |
| `scorer.py` | GPT-4o-mini scoring against personal context (relevance + challenge) |
| `config.py` | Author alias overrides |
| `personal_context.md` | Your values, worldview, and priorities for scoring (gitignored — create your own) |
| `templates/` | Jinja2 HTML templates |
| `static/` | CSS / JS assets |
| `com.newsletterman.plist` | launchd agent descriptor |
