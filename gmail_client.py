import os
import base64
import json
import re
from email.utils import parsedate_to_datetime
from pathlib import Path
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

READ_LATER_LABEL = os.getenv("GMAIL_READ_LATER_LABEL", "Read later")
_EXCLUDE_SUBJECT = os.getenv("NEWSLETTER_EXCLUDE_SUBJECT", "")

_CACHE_DIR = Path(__file__).parent / ".newsletter_cache"


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
    for f in _CACHE_DIR.glob("*.json"):
        try:
            cache[f.stem] = json.loads(f.read_text())
        except Exception:
            pass
    return cache


def _save_cache(cache: dict) -> None:
    _CACHE_DIR.mkdir(exist_ok=True)
    for mid, data in cache.items():
        _cache_file(mid).write_text(json.dumps(data, indent=2))


def get_service():
    creds = Credentials(
        token=None,
        refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/gmail.modify"],
    )
    creds.refresh(Request())
    return build("gmail", "v1", credentials=creds)


def _find_label_id(service, name: str) -> str | None:
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    for label in labels:
        if label["name"].lower() == name.lower():
            return label["id"]
    return None


def _parse_date(date_str: str) -> float:
    try:
        return parsedate_to_datetime(date_str).timestamp()
    except Exception:
        return 0.0


def list_newsletters_cached() -> list[dict]:
    """Return newsletters from cache, sorted newest first by email date."""
    if not _CACHE_DIR.exists():
        return []
    newsletters = []
    for f in _CACHE_DIR.glob("*.json"):
        try:
            entry = json.loads(f.read_text())
            if "subject" in entry:
                if _EXCLUDE_SUBJECT and _EXCLUDE_SUBJECT in entry.get("subject", ""):
                    continue
                if "body" in entry and "word_count" not in entry:
                    entry["word_count"] = len(entry["body"].split())
                    _save_entry(entry["id"], entry)
                newsletters.append({k: entry[k] for k in ("id", "subject", "from", "date", "snippet", "summary", "read", "word_count", "relevance_score", "relevance_note", "challenge_score", "challenge_note", "lean", "lean_note") if k in entry})
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

    current_ids = {msg["id"] for msg in result.get("messages", [])}

    if _CACHE_DIR.exists():
        for f in _CACHE_DIR.glob("*.json"):
            if f.stem not in current_ids:
                try:
                    entry = json.loads(f.read_text())
                    if not entry.get("read"):
                        f.unlink()
                except Exception:
                    f.unlink()

    newsletters = []
    for msg in result.get("messages", []):
        mid = msg["id"]
        cached = _load_entry(mid)
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
            "id": mid,
            "subject": subject,
            "from": headers.get("From", ""),
            "date": headers.get("Date", ""),
            "snippet": meta.get("snippet", ""),
        }
        newsletters.append(entry)
        cached.update(entry)
        _save_entry(mid, cached)
    return newsletters


def get_newsletter_body(message_id: str, service) -> dict:
    cached = _load_entry(message_id)
    if "body" in cached:
        return {k: cached[k] for k in ("id", "subject", "from", "date", "body", "gmail_url", "word_count") if k in cached}

    msg = service.users().messages().get(
        userId="me",
        id=message_id,
        format="full",
    ).execute()
    headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
    data = {
        "id": message_id,
        "subject": headers.get("Subject", "(no subject)"),
        "from": headers.get("From", ""),
        "date": headers.get("Date", ""),
        "body": _extract_text(msg["payload"]),
        "gmail_url": f"https://mail.google.com/mail/u/0/#inbox/{message_id}",
    }
    data["word_count"] = len(data["body"].split())
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


def _strip_html(html: str) -> str:
    text = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL)
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    return re.sub(r"\s+", " ", text).strip()


def restore_read_later_label(message_id: str, service) -> bool:
    label_id = _find_label_id(service, READ_LATER_LABEL)
    if not label_id:
        return False
    service.users().messages().modify(
        userId="me",
        id=message_id,
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
        id=message_id,
        body={"removeLabelIds": [label_id, "UNREAD"]},
    ).execute()
    cached = _load_entry(message_id)
    cached["read"] = True
    _save_entry(message_id, cached)
    return True
