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
from summarizer import summarize
import scorer as _scorer
try:
    from config import AUTHOR_ALIASES, PERSONAL_CONTEXT_FILE
except ImportError:
    AUTHOR_ALIASES: dict[str, str] = {}
    PERSONAL_CONTEXT_FILE: str = ""

def _context_file() -> str:
    """Return path to personal context file if it exists, else empty string."""
    if PERSONAL_CONTEXT_FILE and Path(PERSONAL_CONTEXT_FILE).exists():
        return PERSONAL_CONTEXT_FILE
    return ""

_gmail_service = None
_raindrop_token: str = os.getenv("RAINDROP_TEST_TOKEN", "")


def _is_raindrop(entry_id: str) -> bool:
    return entry_id.startswith("raindrop-")


def _load_any(entry_id: str) -> dict:
    if _is_raindrop(entry_id):
        return _raindrop._load_entry(entry_id)
    return _load_entry(entry_id)


def _save_any(entry_id: str, data: dict) -> None:
    if _is_raindrop(entry_id):
        _raindrop._save_entry(entry_id, data)
    else:
        _save_entry(entry_id, data)


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
                print(f"[score] rel/ch done: {entry_id}")
        if cached.get("lean") is None:
            lean = _scorer.score_political_lean(summary)
            if lean:
                cached.update(lean)
                changed = True
                print(f"[score] lean done: {entry_id} {lean['lean']}")
        if changed:
            _save_any(entry_id, cached)
    except Exception as e:
        print(f"[score] error {entry_id}: {e}")


def _summarize_entry(entry_id: str, service) -> None:
    """Fetch body (if needed) and generate summary for one entry. Blocking."""
    try:
        cached = _load_any(entry_id)
        if cached.get("summary"):
            _score_entry(entry_id)
            return
        if _is_raindrop(entry_id):
            body = _raindrop.get_article_body(entry_id)
        else:
            data = get_newsletter_body(entry_id, service)
            body = data.get("body", "")
        if not body:
            return
        cached = _load_any(entry_id)
        subject = cached.get("subject", "")
        summary = summarize(body, subject, is_article=_is_raindrop(entry_id))
        cached["summary"] = summary
        _save_any(entry_id, cached)
        print(f"[summarize] done: {entry_id}")
        _score_entry(entry_id)
    except Exception as e:
        print(f"[summarize] error {entry_id}: {e}")


def _all_cache_files():
    """Yield (path, entry_id) for all JSON files in both caches."""
    import json as _json
    for cache_dir in (
        Path(__file__).parent / ".newsletter_cache",
        Path(__file__).parent / ".raindrop_cache",
    ):
        if not cache_dir.exists():
            continue
        for f in sorted(cache_dir.glob("*.json")):
            yield f, f.stem


async def _ensure_summaries() -> None:
    """Background task: summarize all cached entries missing a summary."""
    import json as _json
    # Build a dedicated service so background threads don't share httplib2
    # connections with the main event loop (httplib2 is not thread-safe).
    try:
        bg_service = get_service()
    except Exception as e:
        print(f"[summarize] could not build service: {e}")
        return
    loop = asyncio.get_event_loop()
    for f, entry_id in _all_cache_files():
        try:
            entry = _json.loads(f.read_text())
        except Exception:
            continue
        if "subject" in entry and not entry.get("summary"):
            await loop.run_in_executor(None, _summarize_entry, entry_id, bg_service)


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
            sync_newsletters(_gmail_service)
            if _raindrop_token:
                _raindrop.sync_articles(_raindrop_token)
            await _ensure_summaries()
            await _ensure_scores()
        except Exception as e:
            print(f"[bg sync] {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _gmail_service
    _gmail_service = get_service()
    if _raindrop_token:
        try:
            _raindrop.sync_articles(_raindrop_token)
        except Exception as e:
            print(f"[raindrop] startup sync failed: {e}")
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
            return "just now" if hours == 0 else f"{hours}h ago"
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
    if not word_count:
        return ""
    minutes = max(1, round(int(word_count) / 200))
    return f"{minutes} min read"


def _read_time_minutes(word_count) -> int:
    if not word_count:
        return 0
    return max(1, round(int(word_count) / 200))


templates.env.filters["sender_name"] = _sender_name
templates.env.filters["relative_date"] = _relative_date
templates.env.filters["unescape"] = html.unescape
templates.env.filters["date_ts"] = _date_ts
templates.env.filters["markdown_summary"] = _markdown_summary
templates.env.filters["strip_markdown"] = _strip_markdown
templates.env.filters["read_time"] = _read_time
templates.env.filters["read_time_minutes"] = _read_time_minutes


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    from email.utils import parsedate_to_datetime
    newsletters = list_newsletters_cached()
    if _raindrop_token:
        articles = _raindrop.list_articles_cached()
        all_entries = newsletters + articles
        def _ts(e):
            try:
                return parsedate_to_datetime(e.get("date", "")).timestamp()
            except Exception:
                return 0.0
        all_entries.sort(key=_ts, reverse=True)
    else:
        all_entries = newsletters
    has_raindrop = bool(_raindrop_token) and any(e.get("source") == "raindrop" for e in all_entries)
    return templates.TemplateResponse("index.html", {
        "request": request,
        "newsletters": all_entries,
        "has_personal_context": bool(_context_file()),
        "has_raindrop": has_raindrop,
    })


@app.post("/refresh")
async def refresh():
    newsletters = sync_newsletters(_gmail_service)
    if _raindrop_token:
        articles = _raindrop.sync_articles(_raindrop_token)
    else:
        articles = []
    asyncio.create_task(_ensure_summaries())
    asyncio.create_task(_ensure_scores())
    return {"count": len(newsletters) + len(articles)}


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
    return {"ok": True}


@app.post("/newsletter/{message_id}/unread")
async def mark_unread(message_id: str):
    ok = restore_read_later_label(message_id, _gmail_service)
    if not ok:
        raise HTTPException(status_code=404, detail="Label not found")
    return {"ok": True}


@app.post("/article/{article_id}/done")
async def article_done(article_id: str):
    if not _raindrop_token:
        raise HTTPException(status_code=503, detail="Raindrop not configured")
    loop = asyncio.get_event_loop()
    ok = await loop.run_in_executor(None, _raindrop.move_to_archive, article_id, _raindrop_token)
    if not ok:
        raise HTTPException(status_code=404, detail="Article not found or move failed")
    return {"ok": True}


@app.post("/article/{article_id}/unread")
async def article_unread(article_id: str):
    ok = _raindrop.mark_unread_local(article_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Article not found")
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 7431))
    uvicorn.run("main:app", host="127.0.0.1", port=port, reload=True, reload_includes=["*.css", "*.html", "*.js"])
