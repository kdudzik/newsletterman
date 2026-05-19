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

from source_base import registry
from gmail_client import get_service, GmailSource
from raindrop_client import RaindropSource
from wyborcza_client import WyborczaSource
from youtube_client import YouTubeSource, build_service as _yt_build_service
from spotify_client import SpotifySource
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


def _score_entry(entry_id: str) -> None:
    """Generate relevance/challenge/lean scores for one entry. Blocking."""
    ctx = _context_file()
    try:
        source = registry.for_entry(entry_id)
        cached = source.load_entry(entry_id)
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
        if cached.get("trust_score") is None:
            trust = _scorer.score_trustworthiness(
                summary,
                author=cached.get("from", ""),
                title=cached.get("subject", ""),
            )
            if trust:
                cached.update(trust)
                changed = True
                _log(f"[score] trust done: {entry_id} {trust['trust_score']}")
        if changed:
            source.save_entry(entry_id, cached)
    except Exception as e:
        _log(f"[score] error {entry_id}: {e}")


def _summarize_entry(entry_id: str) -> None:
    """Fetch body (if needed) and generate summary for one entry. Blocking."""
    try:
        source = registry.for_entry(entry_id)
        cached = source.load_entry(entry_id)
        if cached.get("summary"):
            _score_entry(entry_id)
            return
        body = source.get_body(entry_id)
        if not body:
            return
        cached = source.load_entry(entry_id)
        subject = cached.get("subject", "")
        if source.is_podcast and len(body) < 400:
            summary = body
        else:
            language = cached.get("transcript_language", "") if source.is_video else cached.get("language", "") if source.is_podcast else ""
            summary = summarize(body, subject, is_article=source.is_article, is_video=source.is_video, is_podcast=source.is_podcast, language=language)
        cached["summary"] = summary
        source.save_entry(entry_id, cached)
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
    loop = asyncio.get_event_loop()
    _log("[summarize] starting pass")
    for f, entry_id in _all_cache_files():
        try:
            entry = _json.loads(f.read_text())
        except Exception:
            continue
        if "subject" in entry and not entry.get("summary"):
            source = registry.for_entry(entry_id)
            await loop.run_in_executor(None, _summarize_entry, entry_id)
            if source.throttle_after_body:
                await asyncio.sleep(1)


_scoring_in_progress: set[str] = set()


async def _ensure_scores() -> None:
    """Background task: score all cached entries that have a summary but no scores yet."""
    import json as _json
    loop = asyncio.get_event_loop()
    for f, entry_id in _all_cache_files():
        if entry_id in _scoring_in_progress:
            continue
        try:
            entry = _json.loads(f.read_text())
        except Exception:
            continue
        if entry.get("summary") and (entry.get("relevance_score") is None or entry.get("lean") is None or entry.get("trust_score") is None):
            _scoring_in_progress.add(entry_id)
            try:
                await loop.run_in_executor(None, _score_entry, entry_id)
            finally:
                _scoring_in_progress.discard(entry_id)


def _sync_all_sources(label: str = "") -> list[int]:
    counts = []
    for source in registry.values():
        _before = _cached_ids()
        try:
            items = source.sync()
            _log_new_entries(items, _before)
            source.clear_error()
            counts.append(len(items))
        except Exception as e:
            source.set_error(str(e))
            suffix = f" {label}" if label else ""
            _log(f"[{source.prefix}]{suffix} sync failed: {e}")
    return counts


async def _bg_sync():
    while True:
        await asyncio.sleep(60)
        try:
            _sync_all_sources()
        except Exception as e:
            _log(f"[bg sync] {e}")
        await _ensure_summaries()
        await _ensure_scores()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _add_events_logged
    _add_events_logged = _load_add_events_logged()

    gmail_service = get_service()
    registry.register(GmailSource(gmail_service), fallback=True)

    if token := os.getenv("RAINDROP_TEST_TOKEN", ""):
        registry.register(RaindropSource(token))

    if schowek_url := os.getenv("WYBORCZA_SCHOWEK_URL", ""):
        registry.register(WyborczaSource(schowek_url))

    if os.getenv("YOUTUBE_ENABLED", "").lower() in ("1", "true", "yes"):
        try:
            _yt_build_service()
            registry.register(YouTubeSource())
        except Exception as e:
            _log(f"[youtube] not available: {e}")

    if os.getenv("SPOTIFY_ENABLED", "").lower() in ("1", "true", "yes"):
        try:
            registry.register(SpotifySource())
        except Exception as e:
            _log(f"[spotify] not available: {e}")

    _sync_all_sources("startup")

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


def _duration_min(duration: str) -> str:
    """Convert HH:MM:SS or MM:SS to '42 min'."""
    if not duration:
        return duration
    parts = duration.split(":")
    try:
        if len(parts) == 3:
            m = max(1, int(parts[0]) * 60 + int(parts[1]))
        else:
            m = max(1, int(parts[0]))
    except Exception:
        return duration
    return f"{m} min"


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
    minutes = entry.get("minutes", 0)
    if event == "add":
        if minutes == 0:
            return  # defer until minutes is known
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
templates.env.filters["read_time"] = _read_time


def _source_status(prefix: str) -> tuple[bool, str]:
    s = registry.get(prefix)
    return (True, s.last_error) if s else (False, "")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    all_entries = []
    for source in registry.values():
        all_entries.extend(source.list_cached())

    def _ts(e):
        try:
            return parsedate_to_datetime(e.get("date", "")).timestamp()
        except Exception:
            return 0.0
    all_entries.sort(key=_ts, reverse=True)

    wyborcza_enabled, wyborcza_error = _source_status("wyborcza")
    youtube_enabled, youtube_error = _source_status("youtube")
    spotify_enabled, spotify_error = _source_status("spotify")

    return templates.TemplateResponse("index.html", {
        "request": request,
        "items": all_entries,
        "has_personal_context": bool(_context_file()),
        "has_raindrop": any(e.get("source") == "raindrop" for e in all_entries),
        "has_wyborcza": any(e.get("source") == "wyborcza" for e in all_entries),
        "has_youtube": any(e.get("source") == "youtube" for e in all_entries),
        "has_spotify": any(e.get("source") == "spotify" for e in all_entries),
        "wyborcza_enabled": wyborcza_enabled,
        "wyborcza_error": wyborcza_error,
        "youtube_enabled": youtube_enabled,
        "youtube_error": youtube_error,
        "spotify_enabled": spotify_enabled,
        "spotify_error": spotify_error,
    })


@app.post("/refresh")
async def refresh():
    counts = _sync_all_sources("refresh")
    asyncio.create_task(_ensure_summaries())
    asyncio.create_task(_ensure_scores())
    _, wyborcza_error = _source_status("wyborcza")
    _, youtube_error = _source_status("youtube")
    _, spotify_error = _source_status("spotify")
    return {
        "count": sum(counts),
        "wyborcza_error": wyborcza_error,
        "youtube_error": youtube_error,
        "spotify_error": spotify_error,
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


@app.get("/entry/{entry_id}", response_class=HTMLResponse)
async def entry_detail(request: Request, entry_id: str):
    source = registry.for_entry(entry_id)
    loop = asyncio.get_event_loop()
    body = await loop.run_in_executor(None, source.get_body, entry_id)
    entry = source.load_entry(entry_id)
    entry["body"] = body
    entry.setdefault("url", entry.get("gmail_url", ""))
    summary = entry.get("summary", "")
    return templates.TemplateResponse("newsletter.html", {"request": request, **entry, "summary": summary})


@app.post("/entry/{entry_id}/summarize")
async def entry_summarize(entry_id: str):
    source = registry.for_entry(entry_id)
    cached = source.load_entry(entry_id)
    if cached.get("summary"):
        return {"summary": cached["summary"]}
    loop = asyncio.get_event_loop()
    body = await loop.run_in_executor(None, source.get_body, entry_id)
    if not body:
        raise HTTPException(status_code=422, detail="No body content found")
    cached = source.load_entry(entry_id)
    subject = cached.get("subject", "")
    if source.is_podcast and len(body) < 400:
        summary = body
    else:
        language = cached.get("transcript_language", "") if source.is_video else cached.get("language", "") if source.is_podcast else ""
        summary = summarize(body, subject, is_article=source.is_article, is_video=source.is_video, is_podcast=source.is_podcast, language=language)
    cached["summary"] = summary
    source.save_entry(entry_id, cached)
    asyncio.create_task(asyncio.get_event_loop().run_in_executor(None, _score_entry, entry_id))
    return {"summary": summary, "summary_html": str(_markdown_summary(summary))}


@app.post("/entry/{entry_id}/done")
async def entry_done(entry_id: str):
    source = registry.for_entry(entry_id)
    loop = asyncio.get_event_loop()
    try:
        ok = await loop.run_in_executor(None, source.mark_done, entry_id)
    except Exception as e:
        source.set_error(str(e))
        raise HTTPException(status_code=502, detail=str(e))
    if not ok:
        raise HTTPException(status_code=404, detail="Not found or failed")
    _append_queue_event("read", entry_id, source.load_entry(entry_id))
    return {"ok": True}


@app.post("/entry/{entry_id}/unread")
async def entry_unread(entry_id: str):
    source = registry.for_entry(entry_id)
    loop = asyncio.get_event_loop()
    try:
        ok = await loop.run_in_executor(None, source.mark_unread_entry, entry_id)
    except Exception as e:
        source.set_error(str(e))
        raise HTTPException(status_code=502, detail=str(e))
    if not ok:
        raise HTTPException(status_code=404, detail="Not found")
    _append_queue_event("unread", entry_id, source.load_entry(entry_id))
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
            cached = registry.for_entry(entry_id).load_entry(entry_id)
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
            if cached.get("trust_score") is not None:
                ts = cached["trust_score"]
                tn = cached.get("trust_note", "")
                tl = "high" if ts >= 7 else ("mid" if ts >= 4 else "low")
                scores["trust_score"] = ts
                scores["trust_html"] = f'<span class="score-badge trust-{tl}">🛡 {ts}<span class="score-tip">{_safe_escape(tn)}</span></span>'
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
            entry = registry.for_entry(ev["id"]).load_entry(ev["id"])
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
