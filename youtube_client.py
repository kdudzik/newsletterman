import json
import os
import hashlib
import re
import time
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path

import requests
import browser_cookie3

_CACHE_DIR = Path(__file__).parent / ".cache"
_INNERTUBE_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"
_INNERTUBE_URL = "https://www.youtube.com/youtubei/v1/browse"
_INNERTUBE_CLIENT = {
    "clientName": "WEB",
    "clientVersion": "2.20240101.00.00",
    "hl": "en",
    "gl": "US",
}
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)


def _cache_file(article_id: str) -> Path:
    return _CACHE_DIR / f"{article_id}.json"


def _load_entry(article_id: str) -> dict:
    f = _cache_file(article_id)
    if f.exists():
        try:
            return json.loads(f.read_text())
        except Exception:
            pass
    return {}


def _save_entry(article_id: str, data: dict) -> None:
    _CACHE_DIR.mkdir(exist_ok=True)
    _cache_file(article_id).write_text(json.dumps(data, indent=2))


def _iso_to_rfc2822(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return format_datetime(dt)
    except Exception:
        return iso


def _parse_ts(rfc_date: str) -> float:
    try:
        return parsedate_to_datetime(rfc_date).timestamp()
    except Exception:
        return 0.0


def _sapisidhash(sapisid: str) -> str:
    ts = str(int(time.time()))
    digest = hashlib.sha1(f"{ts} {sapisid} https://www.youtube.com".encode()).hexdigest()
    return f"SAPISIDHASH {ts}_{digest}"


def _session() -> requests.Session:
    """Build a requests.Session with YouTube cookies read from Chrome."""
    cj = browser_cookie3.chrome(domain_name=".youtube.com")
    cookies = {c.name: c.value for c in cj}
    if not cookies:
        raise RuntimeError("No YouTube cookies found in Chrome — make sure you're logged in to YouTube in Chrome")
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
    sapisid = cookies.get("SAPISID", "")
    s = requests.Session()
    headers = {
        "User-Agent": _BROWSER_UA,
        "Cookie": cookie_str,
        "X-Origin": "https://www.youtube.com",
        "Origin": "https://www.youtube.com",
        "Referer": "https://www.youtube.com/",
    }
    if sapisid:
        headers["Authorization"] = _sapisidhash(sapisid)
    s.headers.update(headers)
    return s


def _innertube_post(session: requests.Session, browse_id: str, continuation: str | None = None) -> dict:
    body: dict = {
        "context": {"client": _INNERTUBE_CLIENT},
    }
    if continuation:
        body["continuation"] = continuation
    else:
        body["browseId"] = browse_id
    resp = session.post(
        _INNERTUBE_URL,
        params={"key": _INNERTUBE_KEY},
        json=body,
        headers={"Content-Type": "application/json", "X-YouTube-Client-Name": "1", "X-YouTube-Client-Version": "2.20240101.00.00"},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def _extract_videos(data: dict) -> tuple[list[dict], str | None]:
    """Pull video items and next continuation token out of an Innertube response."""
    videos: list[dict] = []
    continuation_token: str | None = None

    def _walk(obj):
        nonlocal continuation_token
        if isinstance(obj, dict):
            if "videoId" in obj and "title" in obj:
                videos.append(obj)
            if "continuationCommand" in obj:
                token = obj.get("continuationCommand", {}).get("token")
                if token:
                    continuation_token = token
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)

    _walk(data)
    return videos, continuation_token


_RELATIVE_RE = re.compile(
    r"(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago", re.IGNORECASE
)
_UNIT_SECONDS = {
    "second": 1, "minute": 60, "hour": 3600, "day": 86400,
    "week": 604800, "month": 2592000, "year": 31536000,
}


def _parse_relative_date(text: str) -> datetime | None:
    """Parse 'N units ago' strings into an absolute UTC datetime."""
    m = _RELATIVE_RE.search(text)
    if not m:
        return None
    from datetime import timedelta
    n, unit = int(m.group(1)), m.group(2).lower()
    delta = timedelta(seconds=n * _UNIT_SECONDS[unit])
    return datetime.now(timezone.utc) - delta


def _parse_video(raw: dict, position: int = 0) -> dict | None:
    video_id = raw.get("videoId")
    if not video_id:
        return None

    def _text(obj):
        if isinstance(obj, str):
            return obj
        if isinstance(obj, dict):
            runs = obj.get("runs")
            if runs:
                return "".join(r.get("text", "") for r in runs)
            return obj.get("simpleText", "")
        return ""

    title = _text(raw.get("title", ""))
    channel = _text(raw.get("shortBylineText", raw.get("longBylineText", "")))
    length_text = _text(raw.get("lengthText", ""))
    desc_snippet = _text(raw.get("descriptionSnippet", ""))

    # publishedTimeText or videoInfo.runs contain relative publish date ("2 days ago")
    pub_text = _text(raw.get("publishedTimeText", ""))
    if not pub_text:
        for run in (raw.get("videoInfo") or {}).get("runs", []):
            t = run.get("text", "")
            if _RELATIVE_RE.search(t):
                pub_text = t
                break
    date_dt = _parse_relative_date(pub_text) if pub_text else None
    if date_dt is None:
        # fallback: stagger by position so ordering is stable
        from datetime import timedelta
        date_dt = datetime.now(timezone.utc) - timedelta(hours=position)
    date_rfc = format_datetime(date_dt)

    # best available thumbnail (mqdefault = 320×180, always exists)
    thumbnail_url = f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"

    # setVideoId is the playlist-item token needed for removal from WL
    set_video_id = raw.get("setVideoId") or (
        raw.get("navigationEndpoint", {})
        .get("watchEndpoint", {})
        .get("playlistSetVideoId", "")
    )

    return {
        "video_id": video_id,
        "set_video_id": set_video_id,
        "subject": title or "(no title)",
        "from": channel,
        "date": date_rfc,
        "snippet": desc_snippet,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "thumbnail": thumbnail_url,
        "duration": length_text,
        "source": "youtube",
    }





# --- public API ---

def build_service() -> object:
    """Verify Chrome YouTube cookies are accessible; returns a sentinel."""
    cj = browser_cookie3.chrome(domain_name=".youtube.com")
    cookies = {c.name: c.value for c in cj}
    if not cookies:
        raise RuntimeError("No YouTube cookies found in Chrome — log in to YouTube in Chrome first")
    return True


def list_articles_cached() -> list[dict]:
    """Return cached YouTube Watch Later videos, newest first."""
    if not _CACHE_DIR.exists():
        return []
    articles = []
    for f in _CACHE_DIR.glob("youtube-*.json"):
        try:
            entry = json.loads(f.read_text())
            if "subject" in entry:
                articles.append({k: entry[k] for k in (
                    "id", "subject", "from", "date", "snippet", "summary",
                    "read", "word_count", "relevance_score", "relevance_note",
                    "challenge_score", "challenge_note", "lean", "lean_note",
                    "url", "thumbnail", "duration", "source",
                ) if k in entry})
        except Exception:
            pass
    articles.sort(key=lambda a: _parse_ts(a.get("date", "")), reverse=True)
    return articles


def sync_articles(_service=None) -> list[dict]:
    """Fetch Watch Later via Innertube, prune stale entries, cache new ones."""
    session = _session()
    raw_videos: list[dict] = []
    data = _innertube_post(session, "VLWL")
    batch, token = _extract_videos(data)
    raw_videos.extend(batch)
    while token and len(raw_videos) < 500:
        data = _innertube_post(session, "VLWL", continuation=token)
        batch, token = _extract_videos(data)
        raw_videos.extend(batch)

    if not raw_videos:
        raise RuntimeError("Innertube returned no videos for Watch Later — cookies may be expired or invalid")
    print(f"[youtube] sync: {len(raw_videos)} raw video items fetched")

    # deduplicate by videoId (walk can yield duplicates)
    seen: set[str] = set()
    unique: list[dict] = []
    for v in raw_videos:
        vid = v.get("videoId")
        if vid and vid not in seen:
            seen.add(vid)
            unique.append(v)

    print(f"[youtube] sync: {len(unique)} unique videos after dedup")
    current_ids = {f"youtube-{v['videoId']}" for v in unique}

    if _CACHE_DIR.exists():
        for f in _CACHE_DIR.glob("youtube-*.json"):
            try:
                entry = json.loads(f.read_text())
                if f.stem in current_ids:
                    if entry.get("read"):
                        entry["read"] = False
                        f.write_text(json.dumps(entry, indent=2))
                else:
                    if not entry.get("read"):
                        entry["read"] = True
                        f.write_text(json.dumps(entry, indent=2))
            except Exception:
                f.unlink(missing_ok=True)

    articles = []
    for position, raw in enumerate(unique):
        parsed = _parse_video(raw, position)
        if not parsed:
            continue
        video_id = parsed["video_id"]
        article_id = f"youtube-{video_id}"
        cached = _load_entry(article_id)
        if "subject" in cached:
            changed = False
            if parsed.get("set_video_id"):
                cached["set_video_id"] = parsed["set_video_id"]
                changed = True
            # Always refresh date from live publishedTimeText (overwrite fake position-based dates)
            if parsed.get("date"):
                cached["date"] = parsed["date"]
                changed = True
            if changed:
                _save_entry(article_id, cached)
            articles.append(cached)
            continue
        entry = {"id": article_id, **parsed}
        cached.update(entry)
        _save_entry(article_id, cached)
        articles.append(entry)

    return articles


def get_article_body(article_id: str) -> str:
    """Fetch video transcript (or description as fallback), cache and return it."""
    cached = _load_entry(article_id)
    if len(cached.get("body", "")) > 50:
        return cached["body"]

    video_id = cached.get("video_id", article_id.removeprefix("youtube-"))
    body, lang = _fetch_transcript(video_id)
    if not body:
        body = _fetch_description(video_id)
        lang = ""
    if not body:
        body = cached.get("snippet", "")
        lang = ""

    cached["body"] = body
    cached["word_count"] = len(body.split()) if body else 0
    if lang:
        cached["transcript_language"] = lang
    _save_entry(article_id, cached)
    return body


def _fetch_description(video_id: str) -> str:
    """Fetch video description via Innertube next endpoint. Returns empty string on failure."""
    try:
        resp = requests.post(
            "https://www.youtube.com/youtubei/v1/next",
            params={"key": _INNERTUBE_KEY},
            json={"context": {"client": _INNERTUBE_CLIENT}, "videoId": video_id},
            headers={
                "Content-Type": "application/json",
                "User-Agent": _BROWSER_UA,
                "X-YouTube-Client-Name": "1",
                "X-YouTube-Client-Version": _INNERTUBE_CLIENT["clientVersion"],
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        def _find_description(obj):
            if isinstance(obj, dict):
                if "videoSecondaryInfoRenderer" in obj:
                    r = obj["videoSecondaryInfoRenderer"]
                    # attributedDescription (newer API)
                    content = r.get("attributedDescription", {}).get("content", "")
                    if content:
                        return content
                    # legacy runs format
                    runs = r.get("description", {}).get("runs", [])
                    if runs:
                        return "".join(x.get("text", "") for x in runs)
                for v in obj.values():
                    result = _find_description(v)
                    if result:
                        return result
            elif isinstance(obj, list):
                for item in obj:
                    result = _find_description(item)
                    if result:
                        return result
            return ""

        return _find_description(data)
    except Exception as e:
        print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] [youtube] description fetch failed for {video_id}: {type(e).__name__}: {e}")
        return ""


def _fetch_transcript(video_id: str) -> tuple[str, str]:
    """Returns (text, language_code). Empty strings on failure."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        api = YouTubeTranscriptApi()
        transcript_list = list(api.list(video_id))
        preferred = (
            next((t for t in transcript_list if not t.is_generated and not t.is_translatable), None)
            or next((t for t in transcript_list if not t.is_generated), None)
            or next((t for t in transcript_list if not getattr(t, "is_translation", False)), None)
            or transcript_list[0]
        )
        entries = preferred.fetch()
        text = " ".join(getattr(e, "text", "") for e in entries).strip()
        return text, preferred.language_code
    except Exception as e:
        print(f"[{datetime.now(timezone.utc).strftime("%H:%M:%S")}] [youtube] transcript fetch failed for {video_id}: {type(e).__name__}: {e}")
        return "", ""


def remove_from_watch_later(article_id: str, _cookie_file: str = "") -> bool:
    """Remove video from Watch Later playlist via Innertube, then mark locally as read."""
    cached = _load_entry(article_id)
    if not cached:
        return False

    video_id = cached.get("video_id", article_id.removeprefix("youtube-"))
    set_video_id = cached.get("set_video_id", "")

    try:
        session = _session()
        if set_video_id:
            actions = [{"action": "ACTION_REMOVE_VIDEO", "setVideoId": set_video_id}]
        else:
            actions = [{"action": "ACTION_REMOVE_VIDEO_BY_VIDEO_ID", "removedVideoId": video_id}]
        resp = session.post(
            "https://www.youtube.com/youtubei/v1/browse/edit_playlist",
            params={"key": _INNERTUBE_KEY},
            json={
                "context": {"client": _INNERTUBE_CLIENT},
                "playlistId": "WL",
                "actions": actions,
            },
            headers={
                "Content-Type": "application/json",
                "X-YouTube-Client-Name": "1",
                "X-YouTube-Client-Version": _INNERTUBE_CLIENT["clientVersion"],
            },
            timeout=15,
        )
        resp.raise_for_status()
    except Exception as e:
        print(f"[youtube] WL removal failed for {video_id}: {type(e).__name__}: {e}")

    cached["read"] = True
    _save_entry(article_id, cached)
    return True


def mark_unread(article_id: str) -> bool:
    cached = _load_entry(article_id)
    if not cached:
        return False

    video_id = cached.get("video_id", article_id.removeprefix("youtube-"))

    try:
        session = _session()
        resp = session.post(
            "https://www.youtube.com/youtubei/v1/browse/edit_playlist",
            params={"key": _INNERTUBE_KEY},
            json={
                "context": {"client": _INNERTUBE_CLIENT},
                "playlistId": "WL",
                "actions": [{"action": "ACTION_ADD_VIDEO", "addedVideoId": video_id}],
            },
            headers={
                "Content-Type": "application/json",
                "X-YouTube-Client-Name": "1",
                "X-YouTube-Client-Version": _INNERTUBE_CLIENT["clientVersion"],
            },
            timeout=15,
        )
        resp.raise_for_status()
    except Exception as e:
        print(f"[youtube] WL re-add failed for {video_id}: {type(e).__name__}: {e}")

    cached.pop("read", None)
    _save_entry(article_id, cached)
    return True
