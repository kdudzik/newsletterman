import os
import base64
import json
from pathlib import Path
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

from source_base import _CACHE_DIR, _wpm_minutes, _parse_ts, _strip_html

READ_LATER_LABEL = os.getenv("GMAIL_READ_LATER_LABEL", "Read later")
_EXCLUDE_SUBJECT = os.getenv("NEWSLETTER_EXCLUDE_SUBJECT", "")


def _cache_file(message_id: str) -> Path:
    return _CACHE_DIR / f"{message_id}.json"


def _load_entry(message_id: str) -> dict:
    f = _cache_file(message_id)
    if f.exists():
        try:
            return json.loads(f.read_text())
        except Exception:
            pass
    return {}


def _save_entry(message_id: str, data: dict) -> None:
    _CACHE_DIR.mkdir(exist_ok=True)
    _cache_file(message_id).write_text(json.dumps(data, indent=2))


def _delete_entry(message_id: str) -> None:
    f = _cache_file(message_id)
    if f.exists():
        f.unlink()


def _load_cache() -> dict:
    if not _CACHE_DIR.exists():
        return {}
    cache = {}
    for f in _CACHE_DIR.glob("gmail-*.json"):
        try:
            cache[f.stem[len("gmail-"):]] = json.loads(f.read_text())
        except Exception:
            pass
    return cache


def _save_cache(cache: dict) -> None:
    _CACHE_DIR.mkdir(exist_ok=True)
    for mid, data in cache.items():
        _cache_file(mid).write_text(json.dumps(data, indent=2))


def get_service():
    from google_auth import build_google_service
    return build_google_service("gmail", "v1", "https://www.googleapis.com/auth/gmail.modify")


def _find_label_id(service, name: str) -> str | None:
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    for label in labels:
        if label["name"].lower() == name.lower():
            return label["id"]
    return None


_parse_date = _parse_ts


def list_newsletters_cached() -> list[dict]:
    """Return newsletters from cache, sorted newest first by email date."""
    if not _CACHE_DIR.exists():
        return []
    newsletters = []
    for f in _CACHE_DIR.glob("gmail-*.json"):
        try:
            entry = json.loads(f.read_text())
            if "subject" in entry:
                if _EXCLUDE_SUBJECT and _EXCLUDE_SUBJECT in entry.get("subject", ""):
                    continue
                if "body" in entry and "word_count" not in entry:
                    entry["word_count"] = len(entry["body"].split())
                    entry["minutes"] = _wpm_minutes(entry["word_count"])
                    _save_entry(entry["id"], entry)
                newsletters.append({k: entry[k] for k in (
                    "id", "subject", "from", "date", "snippet", "summary", "read",
                    "word_count", "minutes", "relevance_score", "relevance_note",
                    "challenge_score", "challenge_note", "lean", "lean_note",
                    "trust_score", "trust_note",
                ) if k in entry})
        except Exception:
            pass
    newsletters.sort(key=lambda n: _parse_date(n.get("date", "")), reverse=True)
    return newsletters


def sync_newsletters(service) -> list[dict]:
    """Sync from Gmail: prune stale entries, fetch metadata for new ones."""
    label_id = _find_label_id(service, READ_LATER_LABEL)
    if not label_id:
        return []

    result = service.users().messages().list(
        userId="me",
        labelIds=[label_id],
        maxResults=100,
    ).execute()

    current_bare_ids = {msg["id"] for msg in result.get("messages", [])}

    if _CACHE_DIR.exists():
        for f in _CACHE_DIR.glob("gmail-*.json"):
            bare_id = f.stem[len("gmail-"):]
            try:
                entry = json.loads(f.read_text())
                if bare_id in current_bare_ids:
                    if entry.get("read"):
                        entry["read"] = False
                        f.write_text(json.dumps(entry, indent=2))
                else:
                    if not entry.get("read"):
                        entry["read"] = True
                        f.write_text(json.dumps(entry, indent=2))
            except Exception:
                f.unlink(missing_ok=True)

    newsletters = []
    for msg in result.get("messages", []):
        mid = msg["id"]
        eid = f"gmail-{mid}"
        cached = _load_entry(eid)
        if "subject" in cached:
            newsletters.append({k: cached[k] for k in ("id", "subject", "from", "date", "snippet", "summary") if k in cached})
            continue
        meta = service.users().messages().get(
            userId="me",
            id=mid,
            format="metadata",
            metadataHeaders=["Subject", "From", "Date"],
        ).execute()
        headers = {h["name"]: h["value"] for h in meta["payload"]["headers"]}
        subject = headers.get("Subject", "(no subject)")
        if _EXCLUDE_SUBJECT and _EXCLUDE_SUBJECT in subject:
            continue
        entry = {
            "id": eid,
            "source": "gmail",
            "subject": subject,
            "from": headers.get("From", ""),
            "date": headers.get("Date", ""),
            "snippet": meta.get("snippet", ""),
        }
        newsletters.append(entry)
        cached.update(entry)
        _save_entry(eid, cached)
    return newsletters


def get_newsletter_body(message_id: str, service) -> dict:
    bare_id = message_id[len("gmail-"):]
    cached = _load_entry(message_id)
    if "body" in cached:
        return {k: cached[k] for k in ("id", "subject", "from", "date", "body", "gmail_url", "word_count", "minutes") if k in cached}

    msg = service.users().messages().get(
        userId="me",
        id=bare_id,
        format="full",
    ).execute()
    headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
    data = {
        "id": message_id,
        "source": "gmail",
        "subject": headers.get("Subject", "(no subject)"),
        "from": headers.get("From", ""),
        "date": headers.get("Date", ""),
        "body": _extract_text(msg["payload"]),
        "gmail_url": f"https://mail.google.com/mail/u/0/#inbox/{bare_id}",
    }
    data["word_count"] = len(data["body"].split())
    data["minutes"] = _wpm_minutes(data["word_count"])
    cached.update(data)
    _save_entry(message_id, cached)
    return data


def _extract_text(payload: dict) -> str:
    mime = payload.get("mimeType", "")
    if mime == "text/plain":
        data = payload.get("body", {}).get("data", "")
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    if mime == "text/html":
        data = payload.get("body", {}).get("data", "")
        html = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
        return _strip_html(html)
    for part in payload.get("parts", []):
        text = _extract_text(part)
        if text:
            return text
    return ""


def restore_read_later_label(message_id: str, service) -> bool:
    label_id = _find_label_id(service, READ_LATER_LABEL)
    if not label_id:
        return False
    service.users().messages().modify(
        userId="me",
        id=message_id[len("gmail-"):],
        body={"addLabelIds": [label_id, "UNREAD"]},
    ).execute()
    cached = _load_entry(message_id)
    cached.pop("read", None)
    _save_entry(message_id, cached)
    return True


def remove_read_later_label(message_id: str, service) -> bool:
    label_id = _find_label_id(service, READ_LATER_LABEL)
    if not label_id:
        return False
    service.users().messages().modify(
        userId="me",
        id=message_id[len("gmail-"):],
        body={"removeLabelIds": [label_id, "UNREAD"]},
    ).execute()
    cached = _load_entry(message_id)
    cached["read"] = True
    _save_entry(message_id, cached)
    return True


# --- plugin ---

from source_base import Source


class GmailSource(Source):
    prefix = "gmail"

    def __init__(self, service=None):
        self._service = service

    def _service_for_actions(self):
        return self._service or get_service()

    def sync(self) -> list[dict]:
        service = self._service_for_actions()
        self._service = service
        return sync_newsletters(service)

    def get_body(self, entry_id: str) -> str:
        service = get_service()  # fresh per call — httplib2 is not thread-safe
        return get_newsletter_body(entry_id, service).get("body", "")

    def mark_done(self, entry_id: str) -> bool:
        service = self._service_for_actions()
        self._service = service
        return remove_read_later_label(entry_id, service)

    def mark_unread_entry(self, entry_id: str) -> bool:
        service = self._service_for_actions()
        self._service = service
        return restore_read_later_label(entry_id, service)

    def list_cached(self) -> list[dict]:
        return list_newsletters_cached()

    def load_entry(self, entry_id: str) -> dict:
        return _load_entry(entry_id)

    def save_entry(self, entry_id: str, data: dict) -> None:
        _save_entry(entry_id, data)
