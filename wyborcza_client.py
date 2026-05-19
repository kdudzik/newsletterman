import hashlib
import json
import re
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urlparse

import browser_cookie3
import requests

from source_base import (
    Source,
    _CACHE_DIR, _load_entry, _save_entry, _wpm_minutes,
    _parse_ts, _extract_text, _sync_read_flags,
)

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)
_BAD_BODIES = {
    "Wyborcza.pl",
    "Nieznany błąd - nie można wyświetlić strony",
}


def _save_entry(article_id: str, data: dict) -> None:
    _CACHE_DIR.mkdir(exist_ok=True)
    (_CACHE_DIR / f"{article_id}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": _BROWSER_UA})
    cj = browser_cookie3.chrome(domain_name=".wyborcza.pl")
    cookies = {c.name: c.value for c in cj}
    if not cookies:
        raise RuntimeError("No Wyborcza cookies found in Chrome — make sure you're logged in to wyborcza.pl in Chrome")
    session.cookies.update(cookies)
    return session


def list_articles_cached() -> list[dict]:
    if not _CACHE_DIR.exists():
        return []
    articles = []
    for f in _CACHE_DIR.glob("wyborcza-*.json"):
        try:
            entry = json.loads(f.read_text())
            if "subject" in entry:
                articles.append({k: entry[k] for k in (
                    "id", "subject", "from", "date", "snippet", "summary",
                    "read", "word_count", "minutes", "relevance_score", "relevance_note",
                    "challenge_score", "challenge_note", "lean", "lean_note",
                    "trust_score", "trust_note",
                    "url", "source",
                ) if k in entry})
        except Exception:
            pass
    articles.sort(key=lambda a: _parse_ts(a.get("date", "")), reverse=True)
    return articles


def _is_midnight_utc(rfc_date: str) -> bool:
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(rfc_date).astimezone(timezone.utc)
        return dt.hour == 0 and dt.minute == 0
    except Exception:
        return False


def _article_id_from_url(url: str) -> str:
    return "wyborcza-" + hashlib.sha1(url.encode()).hexdigest()[:16]


def _page_id_from_url(url: str) -> int | None:
    match = re.search(r",(\d+),", url)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            return None
    return None


def _api_url(schowek_url: str, page: int = 0, size: int = 25) -> str:
    parsed = urlparse(schowek_url)
    return f"{parsed.scheme}://{parsed.netloc}/api/read-later/v2/pages/?page={page}&size={size}"


def _page_api_base(base_url: str) -> str:
    parsed = urlparse(base_url)
    return f"{parsed.scheme}://{parsed.netloc}/api/read-later/v2/pages"


def _resolve_page_id(cached: dict) -> int | None:
    raw = cached.get("page_id")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    return _page_id_from_url(cached.get("url", ""))


def sync_articles(schowek_url: str, _cookie_file: str = "") -> list[dict]:
    if not schowek_url:
        return []

    session = _session()
    extracted: list[dict] = []
    page = 0
    size = 25

    while True:
        resp = session.get(_api_url(schowek_url, page=page, size=size), timeout=20, allow_redirects=True)
        resp.raise_for_status()
        payload = resp.json()
        content = payload.get("content") or []
        extracted.extend(content)
        if payload.get("last", True):
            break
        page += 1

    current_ids = {
        _article_id_from_url(item.get("pageUrl", ""))
        for item in extracted
        if item.get("pageUrl")
    }

    _sync_read_flags("wyborcza", current_ids)

    now_dt = datetime.now(timezone.utc)
    today = now_dt.date().isoformat()
    now = format_datetime(now_dt)
    articles = []
    for item in extracted:
        url = (item.get("pageUrl") or "").split("#", 1)[0]
        if not url:
            continue
        article_id = _article_id_from_url(url)
        cached = _load_entry(article_id)
        host = urlparse(url).netloc.lower()
        title = (item.get("title") or "").strip() or cached.get("subject", "(no title)")
        lead = (item.get("lead") or "").strip()
        author = (item.get("author") or "").strip()
        create_date = (item.get("createDate") or "")[:10]
        cached_date = cached.get("date")
        if not cached_date:
            date = now
        elif create_date == today and _is_midnight_utc(cached_date):
            date = now
        else:
            date = cached_date
        entry = {
            "id": article_id,
            "subject": title,
            "from": author or host or "wyborcza.pl",
            "date": date,
            "snippet": lead or cached.get("snippet", ""),
            "page_id": item.get("pageId") or cached.get("page_id") or _page_id_from_url(url),
            "url": url,
            "source": "wyborcza",
        }
        cached.update(entry)
        _save_entry(article_id, cached)
        articles.append(cached)

    return articles


def get_article_body(article_id: str, _cookie_file: str = "") -> str:
    cached = _load_entry(article_id)
    body = cached.get("body", "")
    if body and body.strip() not in _BAD_BODIES:
        return body

    url = cached.get("url", "")
    if not url:
        return ""

    try:
        session = _session()
        resp = session.get(url, timeout=20, allow_redirects=True)
        resp.raise_for_status()
        html = resp.text
        body = _extract_text(html, url=url)
    except Exception as e:
        print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] [wyborcza] fetch failed for {url}: {e}")
        body = cached.get("snippet", "")

    cached["body"] = body
    cached["word_count"] = len(body.split())
    cached["minutes"] = _wpm_minutes(cached["word_count"])
    _save_entry(article_id, cached)
    return body


def mark_read_local(article_id: str) -> bool:
    cached = _load_entry(article_id)
    if not cached:
        return False
    cached["read"] = True
    _save_entry(article_id, cached)
    return True


def mark_unread_local(article_id: str) -> bool:
    cached = _load_entry(article_id)
    if not cached:
        return False
    cached.pop("read", None)
    _save_entry(article_id, cached)
    return True


def remove_from_schowek(article_id: str, schowek_url: str, _cookie_file: str = "") -> bool:
    cached = _load_entry(article_id)
    if not cached:
        return False

    page_id = _resolve_page_id(cached)
    url = cached.get("url", "")
    if not page_id or not url or not schowek_url:
        return False

    session = _session()
    resp = session.delete(
        f"{_page_api_base(schowek_url)}/{page_id}",
        headers={
            "Accept": "*/*",
            "Referer": url,
            "X-Requested-With": "XMLHttpRequest",
        },
        timeout=20,
    )
    resp.raise_for_status()

    cached["read"] = True
    _save_entry(article_id, cached)
    return True


def add_to_schowek(article_id: str, schowek_url: str, _cookie_file: str = "") -> bool:
    cached = _load_entry(article_id)
    if not cached:
        return False

    page_id = _resolve_page_id(cached)
    url = cached.get("url", "")
    if not page_id or not url or not schowek_url:
        return False

    session = _session()
    resp = session.put(
        f"{_page_api_base(schowek_url)}/",
        headers={
            "Accept": "*/*",
            "Content-Type": "application/json;charset=UTF-8",
            "Referer": url,
            "X-Requested-With": "XMLHttpRequest",
        },
        json=[{"pageId": page_id, "pageUrl": url}],
        timeout=20,
    )
    resp.raise_for_status()

    cached["page_id"] = page_id
    cached.pop("read", None)
    _save_entry(article_id, cached)
    return True


# --- plugin ---


class WyborczaSource(Source):
    prefix = "wyborcza"
    is_article = True

    def __init__(self, schowek_url: str):
        self._schowek_url = schowek_url

    def sync(self) -> list[dict]:
        return sync_articles(self._schowek_url)

    def get_body(self, entry_id: str) -> str:
        return get_article_body(entry_id)

    def mark_done(self, entry_id: str) -> bool:
        return remove_from_schowek(entry_id, self._schowek_url)

    def mark_unread_entry(self, entry_id: str) -> bool:
        return add_to_schowek(entry_id, self._schowek_url)

    def list_cached(self) -> list[dict]:
        return list_articles_cached()

    def load_entry(self, entry_id: str) -> dict:
        return _load_entry(entry_id)

    def save_entry(self, entry_id: str, data: dict) -> None:
        _save_entry(entry_id, data)

    def format_error(self, error: str) -> str:
        if not error:
            return ""
        lowered = error.lower()
        if (
            "401" in error or "403" in error
            or "cookie" in lowered or "unauthorized" in lowered
            or "forbidden" in lowered or "verification" in lowered
        ):
            return f"Auth may have expired. Details: {error}".strip()
        return error
