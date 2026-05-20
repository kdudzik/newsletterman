import os
import re
import html
import asyncio
import unicodedata
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from dotenv import load_dotenv
from threading import Event, Lock, Thread
import time

load_dotenv(Path(__file__).parent / ".env")
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from source_base import registry
from gmail_client import get_service, GmailSource
from google_auth import build_google_service
from raindrop_client import RaindropSource
from wyborcza_client import WyborczaSource
from youtube_client import YouTubeSource, build_service as _yt_build_service
from spotify_client import SpotifySource, _is_transcribable_show
from summarizer import summarize
import scorer as _scorer
from provider_state import (
    clear_provider_retry,
    infer_retry_at,
    is_rate_limit_error,
    provider_error,
    provider_retry_at,
    set_provider_retry,
)
try:
    from config import AUTHOR_ALIASES, PERSONAL_CONTEXT_FILE
except ImportError:
    AUTHOR_ALIASES: dict[str, str] = {}
    PERSONAL_CONTEXT_FILE: str = ""

def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


_BG_SYNC_INTERVAL_SECONDS = 15
_SUMMARY_BODY_READY_BATCH = 4
_SUMMARY_FETCH_BATCH = 1
_OPENAI_CHAT_PROVIDER = "openai_chat"
_MONITOR_SNAPSHOT_TTL_SECONDS = 2.0
_SUMMARY_VERSION = "hierarchical_v2"
_FAST_DRAIN_THRESHOLD = 10


def _context_file() -> str:
    """Return path to personal context file if it exists, else empty string."""
    if PERSONAL_CONTEXT_FILE and Path(PERSONAL_CONTEXT_FILE).exists():
        return PERSONAL_CONTEXT_FILE
    return ""


def _is_title_only_summary(cached: dict) -> bool:
    """True when the summary contains no real content (e.g. Spotify episodes with no description)."""
    summary = (cached.get("summary") or "").strip()
    subject = (cached.get("subject") or "").strip()
    if not summary or summary == subject:
        return True
    return False


def _normalize_topic(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _subject_topics(subject: str) -> list[str]:
    match = re.search(r"\((.*?)\)", subject or "")
    if not match:
        return []
    raw_topics = re.split(r"\s*-\s*", match.group(1))
    topics = []
    for topic in raw_topics:
        normalized = _normalize_topic(topic)
        if normalized and normalized not in topics:
            topics.append(normalized)
    return topics


def _is_incomplete_multitopic_summary(cached: dict) -> bool:
    """Detect stale summaries that only cover the opening segment of a multi-topic podcast."""
    if cached.get("source") != "spotify":
        return False
    if cached.get("summary_version") == _SUMMARY_VERSION:
        return False
    summary = (cached.get("summary") or "").strip()
    body = cached.get("body") or ""
    topics = _subject_topics(cached.get("subject", ""))
    if not summary or len(body) < 12000 or len(topics) < 2:
        return False

    summary_words = set(_normalize_topic(summary).split())
    mentioned = 0
    for topic in topics:
        topic_words = [word for word in topic.split() if len(word) >= 4]
        if topic_words and all(word in summary_words for word in topic_words):
            mentioned += 1
    return mentioned < len(topics)


def _deferred_retry_at(cached: dict) -> datetime | None:
    raw = cached.get("transcription_deferred_until", "")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except Exception:
        return None


def _score_entry(entry_id: str) -> None:
    """Generate relevance/challenge/lean scores for one entry. Blocking."""
    ctx = _context_file()
    try:
        source = registry.for_entry(entry_id)
        cached = source.load_entry(entry_id)
        summary = cached.get("summary", "")
        if not summary:
            return
        if _is_title_only_summary(cached):
            _score_keys = ("relevance_score", "relevance_note", "challenge_score", "challenge_note",
                           "lean", "lean_note", "trust_score", "trust_note")
            if any(k in cached for k in _score_keys):
                for k in _score_keys:
                    cached.pop(k, None)
                source.save_entry(entry_id, cached)
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
        if is_rate_limit_error(str(e)):
            set_provider_retry(_OPENAI_CHAT_PROVIDER, infer_retry_at(str(e)), str(e))
        _log(f"[score] error {entry_id}: {e}")


def _summarize_entry(entry_id: str) -> None:
    """Fetch body (if needed) and generate summary for one entry. Blocking."""
    try:
        source = registry.for_entry(entry_id)
        cached = source.load_entry(entry_id)
        has_summary = bool(cached.get("summary"))
        if has_summary and not _is_title_only_summary(cached) and not _is_incomplete_multitopic_summary(cached):
            _score_entry(entry_id)
            return
        body = source.get_body(entry_id)
        if not body:
            if has_summary:
                _score_entry(entry_id)
            return
        cached = source.load_entry(entry_id)
        subject = cached.get("subject", "")
        if source.is_podcast and len(body) < 400:
            summary = body
        else:
            language = cached.get("transcript_language", "") if source.is_video else cached.get("language", "") if source.is_podcast else ""
            summary = summarize(body, subject, is_article=source.is_article, is_video=source.is_video, is_podcast=source.is_podcast, language=language)
        cached["summary"] = summary
        cached["summary_version"] = _SUMMARY_VERSION
        source.save_entry(entry_id, cached)
        _log(f"[summarize] done: {entry_id}")
        if entry_id not in _add_events_logged:
            _append_queue_event("add", entry_id, cached)
        _score_entry(entry_id)
    except Exception as e:
        if is_rate_limit_error(str(e)):
            set_provider_retry(_OPENAI_CHAT_PROVIDER, infer_retry_at(str(e)), str(e))
        _log(f"[summarize] error {entry_id}: {e}")


def _all_cache_files():
    """Yield (path, entry_id) for all JSON files in the shared cache."""
    cache_dir = Path(__file__).parent / ".cache"
    if not cache_dir.exists():
        return
    for f in sorted(cache_dir.glob("*.json")):
        yield f, f.stem


def _ensure_summaries_sync() -> None:
    """Background task: process a small batch, favoring items whose body is already cached."""
    import json as _json
    retry_at = provider_retry_at(_OPENAI_CHAT_PROVIDER)
    if retry_at and retry_at > datetime.now().astimezone():
        _log(f"[summarize] paused until {retry_at.isoformat()} due to OpenAI rate limit")
        return
    if retry_at:
        clear_provider_retry(_OPENAI_CHAT_PROVIDER)
    _log("[summarize] starting pass")
    pending: list[tuple[Path, str, dict]] = []
    for f, entry_id in _all_cache_files():
        try:
            entry = _json.loads(f.read_text())
        except Exception:
            continue
        retry_at = _deferred_retry_at(entry)
        if retry_at and retry_at > datetime.now().astimezone():
            continue
        if "subject" in entry and (
            not entry.get("summary")
            or _is_title_only_summary(entry)
            or _is_incomplete_multitopic_summary(entry)
        ):
            pending.append((f, entry_id, entry))

    if not pending:
        return

    fast_drain = len(pending) <= _FAST_DRAIN_THRESHOLD
    ready_limit = 9999 if fast_drain else _SUMMARY_BODY_READY_BATCH
    fetch_limit = 9999 if fast_drain else _SUMMARY_FETCH_BATCH
    processed_ready = 0
    processed_fetch = 0
    for _, entry_id, entry in pending:
        body_ready = bool(entry.get("body"))
        if body_ready and processed_ready >= ready_limit:
            continue
        if not body_ready and processed_fetch >= fetch_limit:
            continue
        source = registry.for_entry(entry_id)
        _summarize_entry(entry_id)
        if body_ready:
            processed_ready += 1
        else:
            processed_fetch += 1
        if source.throttle_after_body and not body_ready:
            time.sleep(1)
        if processed_ready >= ready_limit and processed_fetch >= fetch_limit:
            break


_scoring_in_progress: set[str] = set()
_monitor_snapshot_cache: dict = {"ts": 0.0, "data": None}
_monitor_snapshot_lock = Lock()
_bg_thread: Thread | None = None
_bg_stop = Event()


def _ensure_scores_sync() -> None:
    """Background task: score all cached entries that have a summary but no scores yet."""
    import json as _json
    retry_at = provider_retry_at(_OPENAI_CHAT_PROVIDER)
    if retry_at and retry_at > datetime.now().astimezone():
        _log(f"[score] paused until {retry_at.isoformat()} due to OpenAI rate limit")
        return
    if retry_at:
        clear_provider_retry(_OPENAI_CHAT_PROVIDER)
    for f, entry_id in _all_cache_files():
        if entry_id in _scoring_in_progress:
            continue
        try:
            entry = _json.loads(f.read_text())
        except Exception:
            continue
        _score_keys = ("relevance_score", "lean", "trust_score")
        needs_cleanup = _is_title_only_summary(entry) and any(k in entry for k in _score_keys)
        needs_scoring = entry.get("summary") and not _is_title_only_summary(entry) and (entry.get("relevance_score") is None or entry.get("lean") is None or entry.get("trust_score") is None)
        if needs_cleanup or needs_scoring:
            _scoring_in_progress.add(entry_id)
            try:
                _score_entry(entry_id)
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


def _bg_sync_loop() -> None:
    while not _bg_stop.wait(_BG_SYNC_INTERVAL_SECONDS):
        try:
            _sync_all_sources()
        except Exception as e:
            _log(f"[bg sync] {e}")
        _ensure_summaries_sync()
        _ensure_scores_sync()


def _startup_warmup_sync() -> None:
    try:
        _sync_all_sources("startup")
    except Exception as e:
        _log(f"[startup] sync failed: {e}")
    _ensure_summaries_sync()
    _ensure_scores_sync()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _add_events_logged, _bg_thread
    _add_events_logged = _load_add_events_logged()
    _bg_stop.clear()

    registry.register(GmailSource(), fallback=True)

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

    Thread(target=_startup_warmup_sync, name="startup-warmup", daemon=True).start()
    if _bg_thread is None or not _bg_thread.is_alive():
        _bg_thread = Thread(target=_bg_sync_loop, name="bg-sync", daemon=True)
        _bg_thread.start()
    yield
    _bg_stop.set()


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


def _provider_health() -> list[dict]:
    providers = [
        ("openai_chat", "openai"),
        ("groq_transcription", "groq"),
        ("openai_transcription", "openai-transcription"),
    ]
    rows = []
    now = datetime.now().astimezone()
    for key, label in providers:
        retry_at = provider_retry_at(key)
        reason = provider_error(key)
        blocked = bool(retry_at and retry_at > now)
        rows.append({
            "source": label,
            "ok": not blocked,
            "error": reason if blocked else "",
            "retry_at": retry_at.isoformat() if blocked and retry_at else "",
        })
    return rows


def _build_monitor_snapshot() -> dict:
    source_health = []
    for prefix in ("gmail", "raindrop", "wyborcza", "youtube", "spotify"):
        enabled, error = _source_status(prefix)
        if enabled:
            source_health.append({
                "source": prefix,
                "ok": not bool(error),
                "error": error,
            })
    return {
        "counts": _monitor_counts(),
        "rows": _monitor_rows(),
        "source_health": source_health,
        "provider_health": _provider_health(),
        "recent_lines": _recent_pipeline_lines(),
    }


def _get_monitor_snapshot() -> dict:
    now = time.time()
    with _monitor_snapshot_lock:
        cached = _monitor_snapshot_cache.get("data")
        ts = float(_monitor_snapshot_cache.get("ts", 0.0))
        if cached is not None and now - ts < _MONITOR_SNAPSHOT_TTL_SECONDS:
            return cached

    data = _build_monitor_snapshot()
    with _monitor_snapshot_lock:
        _monitor_snapshot_cache["ts"] = now
    _monitor_snapshot_cache["data"] = data
    return data


def _run_pipeline_pass(label: str = "") -> None:
    if label:
        try:
            _sync_all_sources(label)
        except Exception as e:
            _log(f"[pipeline {label}] {e}")
    _ensure_summaries_sync()
    _ensure_scores_sync()


def _entry_stage(entry_id: str, entry: dict) -> str:
    if entry.get("read"):
        return "done"
    if entry.get("summary") and not _is_title_only_summary(entry) and not _is_incomplete_multitopic_summary(entry):
        if (
            entry.get("relevance_score") is None
            or entry.get("lean") is None
            or entry.get("trust_score") is None
        ):
            return "scoring"
        return "done"
    if entry.get("transcription_status") == "running":
        return "transcribing"
    retry_at = _deferred_retry_at(entry)
    if (
        entry.get("source") == "spotify"
        and _is_transcribable_show(entry.get("from"))
        and not entry.get("body")
    ):
        return "awaiting_transcript"
    if not entry.get("body"):
        return "fetching"
    if not entry.get("summary") or _is_title_only_summary(entry) or _is_incomplete_multitopic_summary(entry):
        return "summarizing"
    return "done"


def _monitor_rows(limit_per_stage: int = 25) -> dict[str, list[dict]]:
    import json as _json

    stage_rows: dict[str, list[dict]] = {
        "fetching": [],
        "awaiting_transcript": [],
        "transcribing": [],
        "summarizing": [],
        "scoring": [],
        "done": [],
    }
    for f, entry_id in _all_cache_files():
        try:
            entry = _json.loads(f.read_text())
        except Exception:
            continue
        if "subject" not in entry:
            continue
        stage = _entry_stage(entry_id, entry)
        row = {
            "id": entry_id,
            "subject": entry.get("subject", entry_id),
            "source": entry.get("source", "gmail"),
            "author": _sender_name(entry.get("from", "")),
            "date": entry.get("date", ""),
            "minutes": entry.get("minutes", 0),
            "body_chars": len(entry.get("body", "") or ""),
            "summary_chars": len(entry.get("summary", "") or ""),
            "retry_at": entry.get("transcription_deferred_until", ""),
            "error": entry.get("transcription_error", ""),
            "url": entry.get("url", entry.get("gmail_url", "")),
        }
        stage_rows.setdefault(stage, []).append(row)

    def _sort_key(row: dict) -> float:
        return _date_ts(row.get("date", ""))

    for stage, rows in stage_rows.items():
        rows.sort(key=_sort_key, reverse=True)
        stage_rows[stage] = rows[:limit_per_stage]
    return stage_rows


def _monitor_counts() -> dict[str, int]:
    import json as _json

    counts = {
        "fetching": 0,
        "awaiting_transcript": 0,
        "transcribing": 0,
        "summarizing": 0,
        "scoring": 0,
        "done": 0,
        "total": 0,
    }
    for f, entry_id in _all_cache_files():
        try:
            entry = _json.loads(f.read_text())
        except Exception:
            continue
        if "subject" not in entry:
            continue
        stage = _entry_stage(entry_id, entry)
        counts[stage] = counts.get(stage, 0) + 1
        counts["total"] += 1
    return counts


def _recent_pipeline_lines(limit: int = 40) -> list[str]:
    path = Path(__file__).parent / "logs" / "out.log"
    if not path.exists():
        return []
    patterns = ("[summarize]", "[score]", "[gdrive]", "[spotify]", "sync failed")
    try:
        lines = path.read_text(errors="replace").splitlines()
    except Exception:
        return []
    selected = [line for line in lines if any(pattern in line for pattern in patterns)]
    return selected[-limit:]


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
    loop = asyncio.get_event_loop()
    counts = await loop.run_in_executor(None, _sync_all_sources, "refresh")
    Thread(target=_run_pipeline_pass, name="refresh-pipeline", daemon=True).start()
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

    def _clear_and_rescore():
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
        _ensure_scores_sync()

    Thread(target=_clear_and_rescore, name="rescore", daemon=True).start()
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
    if cached.get("summary") and not _is_title_only_summary(cached) and not _is_incomplete_multitopic_summary(cached):
        return {"summary": cached["summary"]}
    loop = asyncio.get_event_loop()
    body = await loop.run_in_executor(None, source.get_body, entry_id)
    if not body:
        cached = source.load_entry(entry_id)
        retry_at = _deferred_retry_at(cached)
        if retry_at and retry_at > datetime.now().astimezone():
            return JSONResponse(
                status_code=202,
                content={
                    "detail": cached.get("transcription_error", "Transcript is queued for a later retry."),
                    "retry_at": retry_at.isoformat(),
                },
            )
        raise HTTPException(status_code=422, detail="No body content found")
    cached = source.load_entry(entry_id)
    subject = cached.get("subject", "")
    if source.is_podcast and len(body) < 400:
        summary = body
    else:
        language = cached.get("transcript_language", "") if source.is_video else cached.get("language", "") if source.is_podcast else ""
        summary = summarize(body, subject, is_article=source.is_article, is_video=source.is_video, is_podcast=source.is_podcast, language=language)
    cached["summary"] = summary
    cached["summary_version"] = _SUMMARY_VERSION
    source.save_entry(entry_id, cached)
    asyncio.get_event_loop().run_in_executor(None, _score_entry, entry_id)
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
            if _is_title_only_summary(cached):
                scores["scores_na"] = True
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
                "author": _sender_name(entry.get("from", "")) if entry.get("from") else "",
                "url": entry.get("url", entry.get("gmail_url", "")),
                "ts": ev["ts"],
            })
    results.sort(key=lambda e: e["ts"])
    return results


@app.get("/queue-history", response_class=HTMLResponse)
async def queue_history_page(request: Request):
    return templates.TemplateResponse("queue_history.html", {"request": request})


@app.get("/monitor", response_class=HTMLResponse)
async def monitor_page(request: Request):
    loop = asyncio.get_event_loop()
    snapshot = await loop.run_in_executor(None, _get_monitor_snapshot)
    return templates.TemplateResponse("monitor.html", {
        "request": request,
        **snapshot,
    })


@app.get("/monitor/content", response_class=HTMLResponse)
async def monitor_content(request: Request):
    loop = asyncio.get_event_loop()
    snapshot = await loop.run_in_executor(None, _get_monitor_snapshot)
    return templates.TemplateResponse("monitor_content.html", {
        "request": request,
        **snapshot,
    })


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 7431))
    reload_enabled = os.getenv("UVICORN_RELOAD", "").lower() in ("1", "true", "yes")
    uvicorn.run(
        "main:app",
        host="127.0.0.1",
        port=port,
        reload=reload_enabled,
        reload_includes=["*.css", "*.html", "*.js"] if reload_enabled else None,
        loop="asyncio",
        http="h11",
        timeout_keep_alive=2,
    )
