import json
import re
import unicodedata
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path

import trafilatura

_CACHE_DIR = Path(__file__).parent / ".cache"


def _cache_file(entry_id: str) -> Path:
    return _CACHE_DIR / f"{entry_id}.json"


def _load_entry(entry_id: str) -> dict:
    f = _cache_file(entry_id)
    if f.exists():
        try:
            return json.loads(f.read_text())
        except Exception:
            pass
    return {}


def _save_entry(entry_id: str, data: dict) -> None:
    _CACHE_DIR.mkdir(exist_ok=True)
    _cache_file(entry_id).write_text(json.dumps(data, indent=2))


def _wpm_minutes(word_count) -> int:
    wc = int(word_count) if word_count else 0
    return max(1, round(wc / 200)) if wc >= 100 else 0


def _parse_ts(rfc_date: str) -> float:
    try:
        return parsedate_to_datetime(rfc_date).timestamp()
    except Exception:
        return 0.0


def _iso_to_rfc2822(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return format_datetime(dt)
    except Exception:
        return iso


def _strip_html(html: str) -> str:
    text = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL)
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"\s+", " ", text).strip()
    # Remove zero-width and invisible formatting characters left by email clients
    text = "".join(c for c in text if unicodedata.category(c) not in ("Cf", "Cc") or c in "\n\t")
    return text


def _extract_text(html: str, url: str = "") -> str:
    text = trafilatura.extract(html, url=url, include_comments=False, include_tables=False)
    if text and len(text.split()) > 50:
        return text
    return _strip_html(html)


def _sync_status_flags(prefix: str, current_ids: set) -> None:
    """Sync entry status based on whether they appear in current_ids.

    Entries still present in source: clear status (back to inbox) only if they had none.
    Entries gone from source: set status=consumed only if they have no explicit status yet.
    """
    if not _CACHE_DIR.exists():
        return
    for f in _CACHE_DIR.glob(f"{prefix}-*.json"):
        try:
            entry = json.loads(f.read_text())
            if f.stem in current_ids:
                changed = False
                if entry.get("read"):
                    del entry["read"]
                    changed = True
                if entry.get("status"):
                    del entry["status"]
                    changed = True
                if changed:
                    f.write_text(json.dumps(entry, indent=2))
            else:
                if not entry.get("status"):
                    entry.pop("read", None)
                    entry["status"] = "consumed"
                    f.write_text(json.dumps(entry, indent=2))
        except Exception:
            f.unlink(missing_ok=True)


class Source(ABC):
    prefix: str
    is_video: bool = False
    is_podcast: bool = False
    is_article: bool = False
    throttle_after_body: bool = False
    last_error: str = ""

    @abstractmethod
    def sync(self) -> list[dict]: ...

    @abstractmethod
    def get_body(self, entry_id: str) -> str: ...

    @abstractmethod
    def mark_consumed(self, entry_id: str) -> bool: ...

    def _external_consume(self, entry_id: str) -> None:
        """Trigger the external API action that removes the entry from the source (e.g. remove label, archive). Called by mark_skipped only when the entry is not already done."""

    def mark_skipped(self, entry_id: str) -> bool:
        cached = self.load_entry(entry_id)
        if cached.get("status") not in ("consumed", "skipped"):
            self._external_consume(entry_id)
        cached.pop("read", None)
        cached["status"] = "skipped"
        self.save_entry(entry_id, cached)
        return True

    @abstractmethod
    def mark_restored(self, entry_id: str) -> bool: ...

    @abstractmethod
    def list_cached(self) -> list[dict]: ...

    @abstractmethod
    def load_entry(self, entry_id: str) -> dict: ...

    @abstractmethod
    def save_entry(self, entry_id: str, data: dict) -> None: ...

    def format_error(self, error: str) -> str:
        return error

    def set_error(self, error: str) -> None:
        self.last_error = self.format_error(error)

    def clear_error(self) -> None:
        self.last_error = ""


class _Registry:
    def __init__(self):
        self._sources: dict[str, "Source"] = {}
        self._fallback: "Source | None" = None

    def register(self, source: "Source", fallback: bool = False) -> None:
        self._sources[source.prefix] = source
        if fallback:
            self._fallback = source

    def for_entry(self, entry_id: str) -> "Source":
        for prefix, source in self._sources.items():
            if source is not self._fallback and entry_id.startswith(prefix + "-"):
                return source
        if self._fallback:
            return self._fallback
        raise KeyError(f"No source for entry: {entry_id}")

    def values(self):
        return self._sources.values()

    def get(self, prefix: str) -> "Source | None":
        return self._sources.get(prefix)

    def __contains__(self, prefix: str) -> bool:
        return prefix in self._sources


registry = _Registry()
