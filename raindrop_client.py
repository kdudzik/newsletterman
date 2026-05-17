import os
import json
import re
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path

import requests

from gmail_client import _strip_html

_CACHE_DIR = Path(__file__).parent / ".raindrop_cache"
_API_BASE = "https://api.raindrop.io/rest/v1"
_UNSORTED_ID = -1


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
    """Convert ISO 8601 date string to RFC 2822 format for compatibility with date filters."""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return format_datetime(dt)
    except Exception:
        return iso


def _parse_ts(rfc_date: str) -> float:
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(rfc_date).timestamp()
    except Exception:
        return 0.0


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def list_articles_cached() -> list[dict]:
    """Return Raindrop articles from cache, newest first."""
    if not _CACHE_DIR.exists():
        return []
    articles = []
    for f in _CACHE_DIR.glob("*.json"):
        try:
            entry = json.loads(f.read_text())
            if "subject" in entry:
                articles.append({k: entry[k] for k in (
                    "id", "subject", "from", "date", "snippet", "summary",
                    "read", "word_count", "relevance_score", "relevance_note",
                    "challenge_score", "challenge_note", "lean", "lean_note",
                    "url", "source",
                ) if k in entry})
        except Exception:
            pass
    articles.sort(key=lambda a: _parse_ts(a.get("date", "")), reverse=True)
    return articles


def sync_articles(token: str) -> list[dict]:
    """Fetch unsorted Raindrop bookmarks, prune stale entries, cache new ones."""
    resp = requests.get(
        f"{_API_BASE}/raindrops/{_UNSORTED_ID}",
        headers=_headers(token),
        params={"perpage": 50},
        timeout=15,
    )
    resp.raise_for_status()
    items = resp.json().get("items", [])
    current_ids = {f"raindrop-{item['_id']}" for item in items}

    if _CACHE_DIR.exists():
        for f in _CACHE_DIR.glob("*.json"):
            if f.stem not in current_ids:
                try:
                    entry = json.loads(f.read_text())
                    if not entry.get("read"):
                        f.unlink()
                except Exception:
                    f.unlink()

    articles = []
    for item in items:
        article_id = f"raindrop-{item['_id']}"
        cached = _load_entry(article_id)
        if "subject" in cached:
            articles.append(cached)
            continue
        entry = {
            "id": article_id,
            "raindrop_id": item["_id"],
            "subject": item.get("title") or item.get("excerpt", "")[:80] or "(no title)",
            "from": item.get("domain", ""),
            "date": _iso_to_rfc2822(item.get("created", "")),
            "snippet": item.get("excerpt", ""),
            "url": item.get("link", ""),
            "source": "raindrop",
        }
        cached.update(entry)
        _save_entry(article_id, cached)
        articles.append(entry)
    return articles


def get_article_body(article_id: str) -> str:
    """Fetch full article text from the article URL, cache it."""
    cached = _load_entry(article_id)
    if "body" in cached:
        return cached["body"]

    url = cached.get("url", "")
    if not url:
        return ""

    try:
        resp = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; newsletterman/1.0)"},
            timeout=20,
            allow_redirects=True,
        )
        resp.raise_for_status()
        body = _strip_html(resp.text)
    except Exception as e:
        print(f"[raindrop] fetch failed for {url}: {e}")
        body = cached.get("snippet", "")

    cached["body"] = body
    cached["word_count"] = len(body.split())
    _save_entry(article_id, cached)
    return body


def _find_or_create_archive(token: str) -> int | None:
    """Return the Raindrop collection ID for 'Archive', creating it if needed."""
    resp = requests.get(f"{_API_BASE}/collections", headers=_headers(token), timeout=10)
    resp.raise_for_status()
    for col in resp.json().get("items", []):
        if col.get("title", "").lower() == "archive":
            return col["_id"]
    create = requests.post(
        f"{_API_BASE}/collection",
        headers=_headers(token),
        json={"title": "Archive"},
        timeout=10,
    )
    create.raise_for_status()
    return create.json().get("item", {}).get("_id")


def move_to_archive(article_id: str, token: str) -> bool:
    """Move a Raindrop bookmark to the Archive collection and mark local cache as read."""
    cached = _load_entry(article_id)
    raindrop_id = cached.get("raindrop_id")
    if not raindrop_id:
        return False
    try:
        archive_id = _find_or_create_archive(token)
        if archive_id is None:
            return False
        requests.put(
            f"{_API_BASE}/raindrop/{raindrop_id}",
            headers=_headers(token),
            json={"collection": {"$id": archive_id}},
            timeout=10,
        ).raise_for_status()
    except Exception as e:
        print(f"[raindrop] move_to_archive error: {e}")
        return False
    cached["read"] = True
    _save_entry(article_id, cached)
    return True


def mark_unread_local(article_id: str) -> bool:
    """Remove the read flag locally (no Raindrop API call)."""
    cached = _load_entry(article_id)
    if not cached:
        return False
    cached.pop("read", None)
    _save_entry(article_id, cached)
    return True
