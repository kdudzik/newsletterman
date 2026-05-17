import os
import json
import re
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urlparse

import threading

import requests
import trafilatura

from gmail_client import _strip_html


def _extract_text(html: str, url: str = "") -> str:
    """Extract main article text using trafilatura, falling back to regex strip."""
    text = trafilatura.extract(html, url=url, include_comments=False, include_tables=False)
    if text and len(text.split()) > 50:
        return text
    return _strip_html(html)

_CACHE_DIR = Path(__file__).parent / ".raindrop_cache"
_API_BASE = "https://api.raindrop.io/rest/v1"
_UNSORTED_ID = -1
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)
_REDDIT_JSON_UA = "newsletterman/1.0 (+local raindrop sync)"
_BAD_REDDIT_BODIES = {
    "Reddit - Please wait for verification",
    "Please wait for verification",
}


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


def _is_reddit_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host.endswith("reddit.com") or host.endswith("redd.it")


def _looks_like_bad_reddit_cache(body: str) -> bool:
    cleaned = " ".join((body or "").split())
    return cleaned in _BAD_REDDIT_BODIES


def _reddit_json_url(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host.endswith("redd.it"):
        path = parsed.path.rstrip("/")
        return f"https://www.reddit.com{path}.json?raw_json=1&limit=500"
    path = parsed.path.rstrip("/")
    if not path.endswith(".json"):
        path = f"{path}.json"
    return f"https://www.reddit.com{path}?raw_json=1&limit=500"


def _reddit_comment_lines(children: list[dict], depth: int = 0, max_comments: int = 40) -> list[str]:
    lines: list[str] = []
    for child in children:
        if len(lines) >= max_comments:
            break
        if child.get("kind") != "t1":
            continue
        data = child.get("data") or {}
        body = (data.get("body") or "").strip()
        if not body or body in {"[deleted]", "[removed]"}:
            continue
        author = data.get("author") or "[unknown]"
        prefix = "  " * depth
        lines.append(f"{prefix}{author}: {body}")
        replies = data.get("replies")
        if isinstance(replies, dict):
            reply_children = ((replies.get("data") or {}).get("children")) or []
            if reply_children and len(lines) < max_comments:
                remaining = max_comments - len(lines)
                lines.extend(_reddit_comment_lines(reply_children, depth + 1, remaining))
    return lines


def _extract_reddit_thread(url: str) -> dict | None:
    try:
        resp = requests.get(
            _reddit_json_url(url),
            headers={"User-Agent": _REDDIT_JSON_UA},
            timeout=20,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception:
        return None

    if not isinstance(payload, list) or len(payload) < 2:
        return None

    post_children = (((payload[0] or {}).get("data") or {}).get("children")) or []
    if not post_children:
        return None

    post = (post_children[0] or {}).get("data") or {}
    title = (post.get("title") or "").strip()
    selftext = (post.get("selftext") or "").strip()
    subreddit = post.get("subreddit_name_prefixed") or post.get("subreddit")
    author = post.get("author") or "[unknown]"

    parts = []
    if title:
        parts.append(title)
    meta = " | ".join(p for p in [subreddit, f"u/{author}" if author else ""] if p)
    if meta:
        parts.append(meta)
    if selftext:
        parts.append(selftext)

    comment_children = (((payload[1] or {}).get("data") or {}).get("children")) or []
    comment_lines = _reddit_comment_lines(comment_children)
    if comment_lines:
        parts.append("Comments:")
        parts.extend(comment_lines)

    body = "\n\n".join(part for part in parts if part).strip()
    snippet = selftext or (comment_lines[0] if comment_lines else "")
    return {
        "subject": title,
        "snippet": snippet[:400],
        "body": body,
    }


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

    # Kick off background word-count prefetch for any article missing it
    needs_wc = [a["id"] for a in articles if not a.get("word_count")]
    if needs_wc:
        threading.Thread(target=_prefetch_word_counts, args=(needs_wc,), daemon=True).start()

    return articles


def _prefetch_word_counts(article_ids: list[str]) -> None:
    for article_id in article_ids:
        try:
            get_article_body(article_id)
        except Exception as e:
            print(f"[raindrop] prefetch failed for {article_id}: {e}")


def get_article_body(article_id: str) -> str:
    """Fetch full article text from the article URL, cache it."""
    cached = _load_entry(article_id)
    url = cached.get("url", "")
    is_reddit = _is_reddit_url(url)

    if "body" in cached and not (is_reddit and _looks_like_bad_reddit_cache(cached["body"])):
        return cached["body"]

    if not url:
        return ""

    if is_reddit:
        reddit = _extract_reddit_thread(url)
        body = (reddit or {}).get("body", "")
        cached["body"] = body
        cached["word_count"] = len(body.split())
        if reddit:
            if reddit.get("subject"):
                cached["subject"] = reddit["subject"]
            if reddit.get("snippet"):
                cached["snippet"] = reddit["snippet"]
        _save_entry(article_id, cached)
        return body

    try:
        resp = requests.get(
            url,
            headers={"User-Agent": _BROWSER_UA},
            timeout=20,
            allow_redirects=True,
        )
        resp.raise_for_status()
        body = _extract_text(resp.text, url=url)
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
