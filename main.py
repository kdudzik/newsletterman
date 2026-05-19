import os
import re
import html
import asyncio
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from gmail_client import get_service, list_newsletters_cached, sync_newsletters, get_newsletter_body, remove_read_later_label, restore_read_later_label, _load_entry, _save_entry
import raindrop_client as _raindrop
import wyborcza_client as _wyborcza
import youtube_client as _youtube
import spotify_client as _spotify
from summarizer import summarize
import scorer as _scorer
try:
    from config import AUTHOR_ALIASES, PERSONAL_CONTEXT_FILE
except ImportError:
    AUTHOR_ALIASES: dict[str, str] = {}
    PERSONAL_CONTEXT_FILE: str = ""

def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


def _context_file() -> str:
    """Return path to personal context file if it exists, else empty string."""
    if PERSONAL_CONTEXT_FILE and Path(PERSONAL_CONTEXT_FILE).exists():
        return PERSONAL_CONTEXT_FILE
    return ""

_gmail_service = None
_youtube_enabled: bool = os.getenv("YOUTUBE_ENABLED", "").lower() in ("1", "true", "yes")
_spotify_enabled: bool = os.getenv("SPOTIFY_ENABLED", "").lower() in ("1", "true", "yes")
_raindrop_token: str = os.getenv("RAINDROP_TEST_TOKEN", "")
_wyborcza_schowek_url: str = os.getenv("WYBORCZA_SCHOWEK_URL", "")
_wyborcza_status = {
    "enabled": bool(_wyborcza_schowek_url),
    "ok": False,
    "error": "",
}
_youtube_status = {
    "enabled": _youtube_enabled,
    "ok": False,
    "error": "",
}
_spotify_status = {
    "enabled": _spotify_enabled,
    "ok": False,
    "error": "",
}


def _set_youtube_status(ok: bool, error: str = "") -> None:
    _youtube_status["enabled"] = _youtube_enabled
    _youtube_status["ok"] = ok
    _youtube_status["error"] = error


def _set_spotify_status(ok: bool, error: str = "") -> None:
    _spotify_status["enabled"] = _spotify_enabled
    _spotify_status["ok"] = ok
    _spotify_status["error"] = error


def _friendly_wyborcza_error(error: str) -> str:
    if not error:
        return ""

    lowered = error.lower()
    if (
        "401" in error
        or "403" in error
        or "cookie" in lowered
        or "unauthorized" in lowered
        or "forbidden" in lowered
        or "verification" in lowered
    ):
        return f"Auth may have expired. Details: {error}".strip()

    return error


def _is_raindrop(entry_id: str) -> bool:
    return entry_id.startswith("raindrop-")


def _is_wyborcza(entry_id: str) -> bool:
    return entry_id.startswith("wyborcza-")


def _is_youtube(entry_id: str) -> bool:
    return entry_id.startswith("youtube-")


def _is_spotify(entry_id: str) -> bool:
    return entry_id.startswith("spotify-")


def _load_any(entry_id: str) -> dict:
    if _is_raindrop(entry_id):
        return _raindrop._load_entry(entry_id)
    if _is_wyborcza(entry_id):
        return _wyborcza._load_entry(entry_id)
    if _is_youtube(entry_id):
        return _youtube._load_entry(entry_id)
    if _is_spotify(entry_id):
        return _spotify._load_entry(entry_id)
    return _load_entry(entry_id)


def _save_any(entry_id: str, data: dict) -> None:
    if _is_raindrop(entry_id):
        _raindrop._save_entry(entry_id, data)
    elif _is_wyborcza(entry_id):
        _wyborcza._save_entry(entry_id, data)
    elif _is_youtube(entry_id):
        _youtube._save_entry(entry_id, data)
    elif _is_spotify(entry_id):
        _spotify._save_entry(entry_id, data)
    else:
        _save_entry(entry_id, data)


def _set_wyborcza_status(ok: bool, error: str = "") -> None:
    _wyborcza_status["enabled"] = bool(_wyborcza_schowek_url)
    _wyborcza_status["ok"] = ok
    _wyborcza_status["error"] = _friendly_wyborcza_error(error)


def _sync_wyborcza() -> list[dict]:
    if not _wyborcza_schowek_url:
        _set_wyborcza_status(False, "")
        return []
    try:
        before = _cached_ids()
        items = _wyborcza.sync_articles(_wyborcza_schowek_url)
        _log_new_entries(items, before)
        _set_wyborcza_status(True, "")
        return items
    except Exception as e:
        _set_wyborcza_status(False, str(e))
        _log(f"[wyborcza] sync failed: {e}")
        return []


def _score_entry(entry_id: str) -> None:
    """Generate relevance/challenge/lean scores for one entry. Blocking."""
    ctx = _context_file()
    try:
        cached = _load_any(entry_id)
        summary = cached.get("summary", "")
        if not summary:
            return
        changed = False
        if ctx and cached.get("relevance_score") is None:
            scores = _scorer.score_newsletter(summary, ctx)
            if scores:
                cached.update(scores)
                changed = True
                _log(f"[score] rel/ch done: {entry_id}")
        if cached.get("lean") is None:
            language = cached.get("transcript_language", "") or cached.get("language", "")
            lean = _scorer.score_political_lean(summary, language)
            if lean:
                cached.update(lean)
                changed = True
                _log(f"[score] lean done: {entry_id} {lean['lean']}")
        if changed:
            _save_any(entry_id, cached)
    except Exception as e:
        _log(f"[score] error {entry_id}: {e}")


def _summarize_entry(entry_id: str, service) -> None:
    """Fetch body (if needed) and generate summary for one entry. Blocking."""
    try:
        cached = _load_any(entry_id)
        if cached.get("summary"):
            _score_entry(entry_id)
            return
        if _is_raindrop(entry_id):
            body = _raindrop.get_article_body(entry_id)
        elif _is_wyborcza(entry_id):
            body = _wyborcza.get_article_body(entry_id)
        elif _is_youtube(entry_id):
            body = _youtube.get_article_body(entry_id)
        elif _is_spotify(entry_id):
            body = _spotify.get_article_body(entry_id)
        else:
            data = get_newsletter_body(entry_id, service)
            body = data.get("body", "")
        if not body:
            return
        cached = _load_any(entry_id)
        subject = cached.get("subject", "")
        is_video = _is_youtube(entry_id)
        is_podcast = _is_spotify(entry_id)
        is_article = _is_raindrop(entry_id) or _is_wyborcza(entry_id)
        if is_podcast and len(body) < 400:
            summary = body
        else:
            language = cached.get("transcript_language", "") if is_video else cached.get("language", "") if is_podcast else ""
            summary = summarize(body, subject, is_article=is_article, is_video=is_video, is_podcast=is_podcast, language=language)
        cached["summary"] = summary
        _save_any(entry_id, cached)
        _log(f"[summarize] done: {entry_id}")
        if entry_id not in _add_events_logged:
            _append_queue_event("add", entry_id, cached)
        _score_entry(entry_id)
    except Exception as e:
        _log(f"[summarize] error {entry_id}: {e}")


def _all_cache_files():
    """Yield (path, entry_id) for all JSON files in the shared cache."""
    cache_dir = Path(__file__).parent / ".cache"
    if not cache_dir.exists():
        return
    for f in sorted(cache_dir.glob("*.json")):
        yield f, f.stem


async def _ensure_summaries() -> None:
    """Background task: summarize all cached entries missing a summary."""
    import json as _json
    # Build a dedicated service so background threads don't share httplib2
    # connections with the main event loop (httplib2 is not thread-safe).
    loop = asyncio.get_event_loop()
    try:
        bg_service = await loop.run_in_executor(None, get_service)
    except Exception as e:
        _log(f"[summarize] could not build service: {e}")
        return
    _log("[summarize] starting pass")
    for f, entry_id in _all_cache_files():
        try:
            entry = _json.loads(f.read_text())
        except Exception:
            continue
        if "subject" in entry and not entry.get("summary"):
            await loop.run_in_executor(None, _summarize_entry, entry_id, bg_service)
            if _is_youtube(entry_id) or _is_spotify(entry_id):
                await asyncio.sleep(1)


async def _ensure_scores() -> None:
    """Background task: score all cached entries that have a summary but no scores yet."""
    import json as _json
    loop = asyncio.get_event_loop()
    for f, entry_id in _all_cache_files():
        try:
            entry = _json.loads(f.read_text())
        except Exception:
            continue
        if entry.get("summary") and (entry.get("relevance_score") is None or entry.get("lean") is None):
            await loop.run_in_executor(None, _score_entry, entry_id)


async def _bg_sync():
    while True:
        await asyncio.sleep(60)
        try:
            _before = _cached_ids()
            _log_new_entries(sync_newsletters(_gmail_service), _before)
            if _raindrop_token:
                _before = _cached_ids()
                _log_new_entries(_raindrop.sync_articles(_raindrop_token), _before)
            if _wyborcza_schowek_url:
                _sync_wyborcza()
            if _youtube_enabled:
                try:
                    _before = _cached_ids()
                    _log_new_entries(_youtube.sync_articles(), _before)
                    _set_youtube_status(True)
                except Exception as e:
                    _set_youtube_status(False, str(e))
                    raise
            if _spotify_enabled:
                try:
                    _before = _cached_ids()
                    _log_new_entries(_spotify.sync_articles(), _before)
                    _set_spotify_status(True)
                except Exception as e:
                    _set_spotify_status(False, str(e))
                    raise
        except Exception as e:
            _log(f"[bg sync] {e}")
        await _ensure_summaries()
        await _ensure_scores()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _gmail_service, _add_events_logged
    _add_events_logged = _load_add_events_logged()
    _gmail_service = get_service()
    if _raindrop_token:
        try:
            _before = _cached_ids()
            _log_new_entries(_raindrop.sync_articles(_raindrop_token), _before)
        except Exception as e:
            _log(f"[raindrop] startup sync failed: {e}")
    if _wyborcza_schowek_url:
        _sync_wyborcza()
    else:
        _set_wyborcza_status(False, "")
    if _youtube_enabled:
        try:
            _before = _cached_ids()
            _log_new_entries(_youtube.sync_articles(), _before)
            _set_youtube_status(True)
        except Exception as e:
            _set_youtube_status(False, str(e))
            _log(f"[youtube] startup sync failed: {e}")
    if _spotify_enabled:
        try:
            _before = _cached_ids()
            _log_new_entries(_spotify.sync_articles(), _before)
            _set_spotify_status(True)
        except Exception as e:
            _set_spotify_status(False, str(e))
            _log(f"[spotify] startup sync failed: {e}")
    asyncio.create_task(_bg_sync())
    asyncio.create_task(_ensure_summaries())
    asyncio.create_task(_ensure_scores())
    yield


def _sender_name(from_str: str) -> str:
    m = re.match(r'^"?([^"<]+?)"?\s*<[^>]+>$', from_str.strip())
    name = m.group(1).strip() if m else from_str.split("<")[0].strip().strip('"')
    return AUTHOR_ALIASES.get(name, name)


def _relative_date(date_str: str) -> str:
    try:
        dt = parsedate_to_datetime(date_str)
        now = datetime.now(timezone.utc)
        diff = now - dt
        days = diff.days
        if days < 0:
            return "just now"
        if days == 0:
            hours = diff.seconds // 3600
            minutes = diff.seconds // 60
            if hours == 0:
                return "just now" if minutes == 0 else f"{minutes}min ago"
            return f"{hours}h ago"
        if days == 1:
            return "yesterday"
        if days < 7:
            return f"{days}d ago"
        if days < 30:
            return f"{days // 7}w ago"
        if days < 365:
            return f"{days // 30}mo ago"
        return f"{days // 365}y ago"
    except Exception:
        return date_str


app = FastAPI(title="Newsletter Man", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
templates.env.globals["css_version"] = lambda: int(Path("static/style.css").stat().st_mtime)
def _date_ts(date_str: str) -> int:
    try:
        return int(parsedate_to_datetime(date_str).timestamp())
    except Exception:
        return 0


def _safe_escape(text: str) -> str:
    """Unescape any existing entities, then re-escape only structural HTML characters."""
    text = html.unescape(text)
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


_INLINE_RE = re.compile(r'\[([^\]]+)\]\((https?://[^)]+)\)|\*\*(.+?)\*\*')

def _inline(text: str, render_links: bool = True) -> str:
    """Render markdown links and bold, escaping everything else."""
    result = []
    last = 0
    for m in _INLINE_RE.finditer(text):
        result.append(_safe_escape(text[last:m.start()]))
        if m.group(1) is not None:
            label = _safe_escape(m.group(1))
            if render_links:
                url = _safe_escape(m.group(2))
                result.append(f'<a href="{url}" target="_blank" rel="noopener">{label}</a>')
            else:
                result.append(label)
        else:
            result.append(f'<strong>{_safe_escape(m.group(3))}</strong>')
        last = m.end()
    result.append(_safe_escape(text[last:]))
    return ''.join(result)


def _bold(text: str) -> str:
    return _inline(text, render_links=True)


def _strip_markdown(text: str) -> str:
    """Strip bullet markers for homepage snippet; links shown as label text only (card is already a link)."""
    from markupsafe import Markup
    text = re.sub(r'^[-*•]\s+', '', text.strip(), flags=re.MULTILINE)
    text = re.sub(r'^\d+\.\s+', '', text, flags=re.MULTILINE)
    return Markup(_inline(text, render_links=True))


def _markdown_summary(text: str) -> str:
    """Convert GPT bullet-point markdown to safe HTML for the summary box."""
    from markupsafe import Markup
    lines = text.strip().splitlines()
    out = []
    in_list = False
    for line in lines:
        line = line.rstrip()
        is_bullet = re.match(r'^[-*•]\s+(.*)', line)
        is_numbered = re.match(r'^\d+\.\s+(.*)', line)
        if is_bullet or is_numbered:
            if not in_list:
                out.append('<ul>')
                in_list = True
            out.append(f'<li>{_bold((is_bullet or is_numbered).group(1))}</li>')
        elif not line:
            continue
        else:
            if in_list:
                out.append('</ul>')
                in_list = False
            out.append(f'<p>{_bold(line)}</p>')
    if in_list:
        out.append('</ul>')
    return Markup('\n'.join(out))


def _read_time(word_count) -> str:
    if not word_count or int(word_count) < 100:
        return ""
    minutes = max(1, round(int(word_count) / 200))
    return f"{minutes} min read"


def _duration_minutes(duration: str) -> int:
    """Convert HH:MM:SS or MM:SS to total minutes as int."""
    if not duration:
        return 0
    parts = duration.split(":")
    try:
        if len(parts) == 3:
            return max(1, int(parts[0]) * 60 + int(parts[1]))
        return max(1, int(parts[0]))
    except Exception:
        return 0


def _duration_min(duration: str) -> str:
    """Convert HH:MM:SS or MM:SS to '42 min'."""
    m = _duration_minutes(duration)
    return f"{m} min" if m else duration


def _read_time_minutes(word_count) -> int:
    if not word_count or int(word_count) < 100:
        return 0
    return max(1, round(int(word_count) / 200))


_add_events_logged: set[str] = set()


def _load_add_events_logged() -> set[str]:
    import json as _json
    path = Path(__file__).parent / ".cache" / "queue_events.jsonl"
    ids: set[str] = set()
    if path.exists():
        with open(path) as f:
            for line in f:
                try:
                    ev = _json.loads(line.strip())
                    if ev.get("event") == "add":
                        ids.add(ev["id"])
                except Exception:
                    pass
    return ids


def _cached_ids() -> set[str]:
    cache_dir = Path(__file__).parent / ".cache"
    if not cache_dir.exists():
        return set()
    ids: set[str] = set()
    for f in cache_dir.glob("*.json"):
        stem = f.stem
        ids.add(stem)
        # gmail files are stored as gmail-{bare_id} but events use bare ids
        if stem.startswith("gmail-"):
            ids.add(stem[len("gmail-"):])
    return ids


def _append_queue_event(event: str, entry_id: str, entry: dict) -> None:
    import json as _json
    source = entry.get("source") or "gmail"
    minutes = _duration_minutes(entry["duration"]) if "duration" in entry \
              else _read_time_minutes(entry.get("word_count", 0))
    if event == "add":
        if minutes == 0:
            return  # defer until word_count/duration is known
        _add_events_logged.add(entry_id)
    record = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "event": event, "id": entry_id, "source": source, "minutes": minutes}
    path = Path(__file__).parent / ".cache" / "queue_events.jsonl"
    with open(path, "a") as f:
        f.write(_json.dumps(record) + "\n")


def _log_new_entries(items: list[dict], before: set[str]) -> None:
    for item in items:
        eid = item.get("id", "")
        if eid and eid not in before and eid not in _add_events_logged:
            _append_queue_event("add", eid, item)


templates.env.filters["sender_name"] = _sender_name
templates.env.filters["relative_date"] = _relative_date
templates.env.filters["unescape"] = html.unescape
templates.env.filters["date_ts"] = _date_ts
templates.env.filters["markdown_summary"] = _markdown_summary
templates.env.filters["strip_markdown"] = _strip_markdown
templates.env.filters["duration_min"] = _duration_min
templates.env.filters["duration_minutes"] = _duration_minutes
templates.env.filters["read_time"] = _read_time
templates.env.filters["read_time_minutes"] = _read_time_minutes


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    from email.utils import parsedate_to_datetime
    newsletters = list_newsletters_cached()
    wyborcza_articles = []
    if _raindrop_token:
        articles = _raindrop.list_articles_cached()
    else:
        articles = []
    if _wyborcza_schowek_url:
        wyborcza_articles = _wyborcza.list_articles_cached()
    youtube_videos = _youtube.list_articles_cached() if _youtube_enabled else []
    spotify_episodes = _spotify.list_articles_cached() if _spotify_enabled else []

    all_entries = newsletters + articles + wyborcza_articles + youtube_videos + spotify_episodes
    def _ts(e):
        try:
            return parsedate_to_datetime(e.get("date", "")).timestamp()
        except Exception:
            return 0.0
    all_entries.sort(key=_ts, reverse=True)

    has_raindrop = bool(_raindrop_token) and any(e.get("source") == "raindrop" for e in all_entries)
    has_wyborcza = bool(_wyborcza_schowek_url) and any(e.get("source") == "wyborcza" for e in all_entries)
    has_youtube = _youtube_enabled and any(e.get("source") == "youtube" for e in all_entries)
    has_spotify = _spotify_enabled and any(e.get("source") == "spotify" for e in all_entries)
    return templates.TemplateResponse("index.html", {
        "request": request,
        "newsletters": all_entries,
        "has_personal_context": bool(_context_file()),
        "has_raindrop": has_raindrop,
        "has_wyborcza": has_wyborcza,
        "has_youtube": has_youtube,
        "has_spotify": has_spotify,
        "wyborcza_enabled": _wyborcza_status["enabled"],
        "wyborcza_error": _wyborcza_status["error"],
        "youtube_enabled": _youtube_status["enabled"],
        "youtube_error": _youtube_status["error"],
        "spotify_enabled": _spotify_status["enabled"],
        "spotify_error": _spotify_status["error"],
    })


@app.post("/refresh")
async def refresh():
    _before = _cached_ids()
    newsletters = sync_newsletters(_gmail_service)
    _log_new_entries(newsletters, _before)
    if _raindrop_token:
        _before = _cached_ids()
        articles = _raindrop.sync_articles(_raindrop_token)
        _log_new_entries(articles, _before)
    else:
        articles = []
    wyborcza_articles = _sync_wyborcza() if _wyborcza_schowek_url else []
    youtube_videos = []
    if _youtube_enabled:
        try:
            _before = _cached_ids()
            youtube_videos = _youtube.sync_articles()
            _log_new_entries(youtube_videos, _before)
            _set_youtube_status(True)
        except Exception as e:
            _set_youtube_status(False, str(e))
            _log(f"[youtube] refresh sync failed: {e}")
    spotify_episodes = []
    if _spotify_enabled:
        try:
            _before = _cached_ids()
            spotify_episodes = _spotify.sync_articles()
            _log_new_entries(spotify_episodes, _before)
            _set_spotify_status(True)
        except Exception as e:
            _set_spotify_status(False, str(e))
            _log(f"[spotify] refresh sync failed: {e}")
    asyncio.create_task(_ensure_summaries())
    asyncio.create_task(_ensure_scores())
    return {
        "count": len(newsletters) + len(articles) + len(wyborcza_articles) + len(youtube_videos) + len(spotify_episodes),
        "wyborcza_error": _wyborcza_status["error"],
        "youtube_error": _youtube_status["error"],
        "spotify_error": _spotify_status["error"],
    }


@app.post("/rescore")
async def rescore():
    import json as _json

    async def _clear_and_rescore():
        cache_dir = Path(__file__).parent / ".newsletter_cache"
        if not cache_dir.exists():
            return
        for f in cache_dir.glob("*.json"):
            try:
                entry = _json.loads(f.read_text())
            except Exception:
                continue
            if entry.get("summary"):
                for k in ("relevance_score", "relevance_note", "challenge_score", "challenge_note", "lean", "lean_note"):
                    entry.pop(k, None)
                f.write_text(_json.dumps(entry, ensure_ascii=False, indent=2))
        await _ensure_scores()

    asyncio.create_task(_clear_and_rescore())
    return {"status": "rescoring in background"}


@app.get("/newsletter/{message_id}", response_class=HTMLResponse)
async def newsletter_detail(request: Request, message_id: str):
    data = get_newsletter_body(message_id, _gmail_service)
    cached = _load_entry(message_id)
    data["summary"] = cached.get("summary", "")
    return templates.TemplateResponse("newsletter.html", {"request": request, **data})


@app.post("/newsletter/{message_id}/summarize")
async def api_summarize(message_id: str):
    cached = _load_entry(message_id)
    if cached.get("summary"):
        return {"summary": cached["summary"]}
    data = get_newsletter_body(message_id, _gmail_service)
    if not data["body"]:
        raise HTTPException(status_code=422, detail="No body content found")
    summary = summarize(data["body"], data["subject"])
    cached = _load_entry(message_id)
    cached["summary"] = summary
    _save_entry(message_id, cached)
    loop = asyncio.get_event_loop()
    asyncio.create_task(loop.run_in_executor(None, _score_entry, message_id))
    return {"summary": summary, "summary_html": str(_markdown_summary(summary))}


@app.post("/newsletter/{message_id}/done")
async def mark_done(message_id: str):
    ok = remove_read_later_label(message_id, _gmail_service)
    if not ok:
        raise HTTPException(status_code=404, detail="Label not found")
    _append_queue_event("read", message_id, _load_entry(message_id))
    return {"ok": True}


@app.post("/newsletter/{message_id}/unread")
async def mark_unread(message_id: str):
    ok = restore_read_later_label(message_id, _gmail_service)
    if not ok:
        raise HTTPException(status_code=404, detail="Label not found")
    _append_queue_event("unread", message_id, _load_entry(message_id))
    return {"ok": True}


@app.post("/article/{article_id}/done")
async def article_done(article_id: str):
    loop = asyncio.get_event_loop()
    if _is_raindrop(article_id):
        if not _raindrop_token:
            raise HTTPException(status_code=503, detail="Raindrop not configured")
        ok = await loop.run_in_executor(None, _raindrop.move_to_archive, article_id, _raindrop_token)
    elif _is_wyborcza(article_id):
        try:
            ok = await loop.run_in_executor(
                None,
                _wyborcza.remove_from_schowek,
                article_id,
                _wyborcza_schowek_url,
            )
        except Exception as e:
            _set_wyborcza_status(False, str(e))
            raise HTTPException(status_code=502, detail="Wyborcza Schowek remove failed")
    elif _is_youtube(article_id):
        if not _youtube_enabled:
            raise HTTPException(status_code=503, detail="YouTube not configured")
        ok = await loop.run_in_executor(None, _youtube.remove_from_watch_later, article_id)
    elif _is_spotify(article_id):
        if not _spotify_enabled:
            raise HTTPException(status_code=503, detail="Spotify not configured")
        ok = await loop.run_in_executor(None, _spotify.remove_from_saved, article_id)
    else:
        raise HTTPException(status_code=404, detail="Article not found")
    if not ok:
        raise HTTPException(status_code=404, detail="Article not found or move failed")
    _append_queue_event("read", article_id, _load_any(article_id))
    return {"ok": True}


@app.post("/article/{article_id}/unread")
async def article_unread(article_id: str):
    loop = asyncio.get_event_loop()
    if _is_raindrop(article_id):
        if not _raindrop_token:
            raise HTTPException(status_code=503, detail="Raindrop not configured")
        ok = await loop.run_in_executor(None, _raindrop.mark_unread, article_id, _raindrop_token)
    elif _is_wyborcza(article_id):
        try:
            ok = await loop.run_in_executor(
                None,
                _wyborcza.add_to_schowek,
                article_id,
                _wyborcza_schowek_url,
            )
        except Exception as e:
            _set_wyborcza_status(False, str(e))
            raise HTTPException(status_code=502, detail="Wyborcza Schowek add failed")
    elif _is_youtube(article_id):
        ok = await loop.run_in_executor(None, _youtube.mark_unread, article_id)
    elif _is_spotify(article_id):
        ok = await loop.run_in_executor(None, _spotify.mark_unread, article_id)
    else:
        raise HTTPException(status_code=404, detail="Article not found")
    if not ok:
        raise HTTPException(status_code=404, detail="Article not found")
    _append_queue_event("unread", article_id, _load_any(article_id))
    return {"ok": True}


@app.get("/api/entries/status")
async def entries_status(ids: str):
    from markupsafe import Markup
    result = {}
    for entry_id in ids.split(","):
        entry_id = entry_id.strip()
        if not entry_id:
            continue
        try:
            cached = _load_any(entry_id)
        except Exception:
            continue
        summary = cached.get("summary", "")
        scores: dict = {"subject": cached.get("subject", "")}
        if summary:
            scores["summary_html"] = str(_markdown_summary(summary))
            if cached.get("relevance_score") is not None:
                rs = cached["relevance_score"]
                cs = cached.get("challenge_score", 0)
                rn = cached.get("relevance_note", "")
                cn = cached.get("challenge_note", "")
                rl = "high" if rs >= 7 else ("mid" if rs >= 4 else "low")
                cl = "high" if cs >= 7 else ("mid" if cs >= 4 else "low")
                scores["relevance_score"] = rs
                scores["challenge_score"] = cs
                scores["relevance_html"] = f'<span class="score-badge relevance-{rl}">↑ {rs}<span class="score-tip">{_safe_escape(rn)}</span></span><span class="score-badge challenge-{cl}">⚡ {cs}<span class="score-tip">{_safe_escape(cn)}</span></span>'
            if cached.get("lean") is not None:
                lean = cached["lean"]
                lean_note = cached.get("lean_note", "")
                scores["lean"] = lean
                scores["lean_html"] = f'<span class="score-badge lean-{lean.lower()}">{lean}<span class="score-tip">{_safe_escape(lean_note)}</span></span>'
        result[entry_id] = scores
    return result


@app.get("/api/queue-history")
async def api_queue_history():
    import json as _json
    from collections import defaultdict
    path = Path(__file__).parent / ".cache" / "queue_events.jsonl"
    if not path.exists():
        return {"dates": [], "series": {}}

    events = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    events.append(_json.loads(line))
                except Exception:
                    continue
    if not events:
        return {"dates": [], "series": {}}

    events.sort(key=lambda e: e["ts"])
    queue: dict[str, dict] = {}
    snapshots: list[tuple[str, dict]] = []
    cur_day = events[0]["ts"][:10]

    for ev in events:
        day = ev["ts"][:10]
        if day != cur_day:
            snapshots.append((cur_day, dict(queue)))
            cur_day = day
        eid = ev["id"]
        if ev["event"] in ("add", "unread"):
            queue[eid] = {"source": ev["source"], "minutes": ev["minutes"]}
        elif ev["event"] in ("read", "delete"):
            queue.pop(eid, None)
    snapshots.append((cur_day, dict(queue)))

    all_sources = ["gmail", "raindrop", "wyborcza", "youtube", "spotify"]
    from datetime import date as _date, timedelta
    earliest_event = events[0]["ts"][:10]
    three_years_ago = str((_date.today()).replace(year=_date.today().year - 3))
    HISTORY_START = max(earliest_event, three_years_ago)
    end = snapshots[-1][0]
    d = _date.fromisoformat(HISTORY_START)
    end_d = _date.fromisoformat(end)
    # Derive queue state at HISTORY_START by replaying all earlier events
    snap_map_full = {day: state for day, state in snapshots}
    last_state_pre: dict[str, dict] = {}
    for snap_day, state in sorted(snap_map_full.items()):
        if snap_day < HISTORY_START:
            last_state_pre = state
        else:
            break
    all_dates = []
    while d <= end_d:
        all_dates.append(str(d))
        d += timedelta(days=1)

    snap_map = snap_map_full
    last_state: dict[str, dict] = last_state_pre
    series: dict[str, list[float]] = {s: [] for s in all_sources}
    for day in all_dates:
        if day in snap_map:
            last_state = snap_map[day]
        totals: dict[str, float] = defaultdict(float)
        for item in last_state.values():
            totals[item["source"]] += item["minutes"]
        for src in all_sources:
            series[src].append(round(totals.get(src, 0) / 60, 1))

    active = [s for s in all_sources if any(v > 0 for v in series[s])]
    return {"dates": all_dates, "series": {s: series[s] for s in active}}


@app.get("/api/queue-events")
async def api_queue_events(date: str):
    import json as _json
    path = Path(__file__).parent / ".cache" / "queue_events.jsonl"
    if not path.exists():
        return []
    results = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = _json.loads(line)
            except Exception:
                continue
            if ev.get("ts", "")[:10] != date:
                continue
            entry = _load_any(ev["id"])
            results.append({
                "event": ev["event"],
                "id": ev["id"],
                "source": ev["source"],
                "minutes": ev["minutes"],
                "subject": entry.get("subject", ev["id"]),
                "url": entry.get("url", entry.get("gmail_url", "")),
                "ts": ev["ts"],
            })
    results.sort(key=lambda e: e["ts"])
    return results


@app.get("/queue-history", response_class=HTMLResponse)
async def queue_history_page(request: Request):
    return templates.TemplateResponse("queue_history.html", {"request": request})


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 7431))
    uvicorn.run("main:app", host="127.0.0.1", port=port, reload=True, reload_includes=["*.css", "*.html", "*.js"])
