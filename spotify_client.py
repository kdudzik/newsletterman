import json
import os
import re
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path

import spotipy
from spotipy.oauth2 import SpotifyOAuth

from source_base import (
    Source,
    _CACHE_DIR, _load_entry, _save_entry,
    _parse_ts,
)


def _is_transcribable_show(show_name: str) -> bool:
    """Return True for shows whose audio is fetched from Drive rather than Spotify description."""
    try:
        from config import SPOTIFY_DRIVE_TRANSCRIBE_SHOWS
    except ImportError:
        SPOTIFY_DRIVE_TRANSCRIBE_SHOWS = ["podsumowanie"]
    name = (show_name or "").lower()
    return any(keyword in name for keyword in SPOTIFY_DRIVE_TRANSCRIBE_SHOWS)


def _clean_description(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"([.!?])([A-ZĄĆĘŁŃÓŚŹŻ])", r"\1 \2", text)
    text = re.sub(r" {2,}", " ", text)
    return text.strip()
_SPOTIFY_CACHE = Path(__file__).parent.parent / "spotify-export" / ".cache"
_SCOPE = "user-library-read user-library-modify user-read-playback-position"
_TRANSCRIPTION_ACTIVE_PATH = _CACHE_DIR / "_spotify_transcription_active.json"


def _iso_to_rfc2822(iso: str) -> str:
    try:
        if len(iso) == 4:
            dt = datetime(int(iso), 1, 1, tzinfo=timezone.utc)
        elif len(iso) == 7:
            dt = datetime(int(iso[:4]), int(iso[5:7]), 1, tzinfo=timezone.utc)
        else:
            dt = datetime.strptime(iso, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return format_datetime(dt)
    except Exception:
        return iso


def _deferred_until(cached: dict) -> datetime | None:
    raw = cached.get("transcription_deferred_until", "")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except Exception:
        return None


def _clear_transcription_deferred(cached: dict) -> bool:
    changed = False
    for key in ("transcription_deferred_until", "transcription_status", "transcription_error"):
        if key in cached:
            cached.pop(key, None)
            changed = True
    return changed


def _active_transcription_entry() -> str:
    if not _TRANSCRIPTION_ACTIVE_PATH.exists():
        return ""
    try:
        data = json.loads(_TRANSCRIPTION_ACTIVE_PATH.read_text())
    except Exception:
        return ""
    return str(data.get("entry_id", "") or "")


def _set_active_transcription(entry_id: str) -> None:
    _CACHE_DIR.mkdir(exist_ok=True)
    _TRANSCRIPTION_ACTIVE_PATH.write_text(json.dumps({
        "entry_id": entry_id,
        "started_at": datetime.now().astimezone().isoformat(),
    }))


def _clear_active_transcription(entry_id: str) -> None:
    if not _TRANSCRIPTION_ACTIVE_PATH.exists():
        return
    try:
        data = json.loads(_TRANSCRIPTION_ACTIVE_PATH.read_text())
    except Exception:
        _TRANSCRIPTION_ACTIVE_PATH.unlink(missing_ok=True)
        return
    if data.get("entry_id") == entry_id:
        _TRANSCRIPTION_ACTIVE_PATH.unlink(missing_ok=True)


def _client() -> spotipy.Spotify:
    cache_path = str(_SPOTIFY_CACHE) if _SPOTIFY_CACHE.exists() else None
    return spotipy.Spotify(auth_manager=SpotifyOAuth(
        client_id=os.getenv("SPOTIPY_CLIENT_ID"),
        client_secret=os.getenv("SPOTIPY_CLIENT_SECRET"),
        redirect_uri=os.getenv("SPOTIPY_REDIRECT_URI", "http://127.0.0.1:8888/callback"),
        scope=_SCOPE,
        cache_path=cache_path,
        open_browser=False,
    ))


def sync_articles(_service=None) -> list[dict]:
    sp = _client()
    results = sp.current_user_saved_episodes(limit=50)
    entries = []
    seen_ids = set()
    while results:
        for item in results.get("items", []):
            ep = item.get("episode")
            if not ep:
                continue
            episode_id = ep.get("id")
            if not episode_id:
                continue
            entry_id = f"spotify-{episode_id}"
            if entry_id in seen_ids:
                continue
            seen_ids.add(entry_id)

            show = ep.get("show", {})
            title = ep.get("name") or "(no title)"
            show_name = show.get("name") or ""
            release_date = ep.get("release_date") or ""
            date_rfc = _iso_to_rfc2822(release_date) if release_date else format_datetime(datetime.now(timezone.utc))
            duration_ms = ep.get("duration_ms") or 0
            duration_s = duration_ms // 1000
            duration_min = duration_s // 60
            description = _clean_description(ep.get("description") or "")
            url = ep.get("external_urls", {}).get("spotify", f"https://open.spotify.com/episode/{episode_id}")
            images = ep.get("images") or show.get("images") or []
            thumbnail = images[0]["url"] if images else ""

            languages = ep.get("languages") or []
            language = (languages[0] or "").lower() if languages else ""

            entry = {
                "id": entry_id,
                "subject": title,
                "from": show_name,
                "date": date_rfc,
                "snippet": description[:200] if description else "",
                "description": description,
                "url": url,
                "thumbnail": thumbnail,
                "duration": f"{duration_min}:{duration_s % 60:02d}" if duration_min else "",
                "minutes": max(1, duration_min) if duration_s > 0 else 0,
                "source": "spotify",
                "episode_id": episode_id,
                "language": language,
            }

            cached = _load_entry(entry_id)
            cached.update(entry)
            cached.pop("read", None)  # episode is in saved list → treat as unread
            _save_entry(entry_id, cached)
            entries.append(entry)
        results = sp.next(results) if results.get("next") else None
    return entries


def list_articles_cached() -> list[dict]:
    entries = []
    if not _CACHE_DIR.exists():
        return entries
    for f in _CACHE_DIR.glob("spotify-*.json"):
        try:
            entry = json.loads(f.read_text())
        except Exception:
            continue
        if "subject" in entry:
            entries.append({k: entry[k] for k in (
                "id", "subject", "from", "date", "snippet", "summary",
                "status", "url", "thumbnail", "duration", "minutes", "source",
                "transcription_status", "transcription_deferred_until", "transcription_error",
                "relevance_score", "relevance_note",
                "challenge_score", "challenge_note",
                "lean", "lean_note",
                "trust_score", "trust_note",
            ) if k in entry})
    entries.sort(key=lambda a: _parse_ts(a.get("date", "")), reverse=True)
    return entries


def get_article_body(entry_id: str, drive_service=None) -> str:
    cached = _load_entry(entry_id)

    # Consumed episodes don't need a transcript
    if cached.get("status") == "consumed":
        return ""

    # Return cached transcript if already fetched
    if cached.get("body"):
        return cached["body"]

    deferred_until = _deferred_until(cached)
    now = datetime.now().astimezone()
    if deferred_until and deferred_until > now:
        return ""
    if deferred_until and _clear_transcription_deferred(cached):
        _save_entry(entry_id, cached)

    # Podsumowanie: try Drive transcription (file may not exist yet — don't cache failure)
    if _is_transcribable_show(cached.get("from")) and drive_service:
        active_entry = _active_transcription_entry()
        if active_entry and active_entry != entry_id:
            cached["transcription_status"] = "queued"
            _save_entry(entry_id, cached)
            return ""
        from gdrive_audio import TranscriptDeferredError, find_episode_file, transcribe_episode
        file_id = find_episode_file(drive_service, cached.get("subject", ""))
        if file_id:
            cached["transcription_status"] = "running"
            cached.pop("transcription_error", None)
            _save_entry(entry_id, cached)
            _set_active_transcription(entry_id)
            try:
                transcript = transcribe_episode(drive_service, file_id, subject=cached.get("subject", ""))
            except TranscriptDeferredError as e:
                cached["transcription_status"] = "deferred"
                cached["transcription_deferred_until"] = e.retry_at
                cached["transcription_error"] = e.reason
                _save_entry(entry_id, cached)
                _clear_active_transcription(entry_id)
                print(f"[spotify] transcript deferred for {entry_id} until {e.retry_at}")
                return ""
            except Exception:
                cached["transcription_status"] = "queued"
                _save_entry(entry_id, cached)
                _clear_active_transcription(entry_id)
                raise
            if transcript:
                cached["body"] = transcript
                _clear_transcription_deferred(cached)
                _clear_active_transcription(entry_id)
                _save_entry(entry_id, cached)
                return transcript
            cached["transcription_status"] = "queued"
            _save_entry(entry_id, cached)
            _clear_active_transcription(entry_id)
        return ""

    # Fallback: Spotify description
    description = cached.get("description", "")
    if not description:
        episode_id = cached.get("episode_id", entry_id.removeprefix("spotify-"))
        try:
            sp = _client()
            ep = sp.episode(episode_id)
            description = _clean_description(ep.get("description", "") or ep.get("html_description", ""))
            cached["description"] = description
            _save_entry(entry_id, cached)
        except Exception as e:
            print(f"[spotify] description fetch failed for {episode_id}: {e}")
    return description


def remove_from_saved(entry_id: str) -> bool:
    cached = _load_entry(entry_id)
    if not cached:
        return False
    episode_id = cached.get("episode_id", entry_id.removeprefix("spotify-"))
    try:
        sp = _client()
        sp.current_user_saved_episodes_delete([episode_id])
    except Exception as e:
        print(f"[spotify] remove saved episode failed for {episode_id}: {e}")
    cached.pop("read", None)
    cached["status"] = "consumed"
    _save_entry(entry_id, cached)
    return True


def mark_unread(entry_id: str) -> bool:
    cached = _load_entry(entry_id)
    if not cached:
        return False
    episode_id = cached.get("episode_id", entry_id.removeprefix("spotify-"))
    try:
        sp = _client()
        sp.current_user_saved_episodes_add([episode_id])
    except Exception as e:
        print(f"[spotify] re-save episode failed for {episode_id}: {e}")
    cached.pop("read", None)
    cached.pop("status", None)
    _save_entry(entry_id, cached)
    return True


# --- plugin ---


class SpotifySource(Source):
    prefix = "spotify"
    is_podcast = True
    throttle_after_body = True

    def __init__(self, drive_service=None):
        self._drive_service = drive_service

    def _ensure_drive_service(self):
        if self._drive_service is not None:
            return self._drive_service
        try:
            from google_auth import build_google_service
            self._drive_service = build_google_service(
                "drive", "v3", "https://www.googleapis.com/auth/drive.readonly"
            )
        except Exception as e:
            self.last_error = f"Drive not available for transcripts: {e}"
            return None
        return self._drive_service

    def sync(self) -> list[dict]:
        return sync_articles()

    def get_body(self, entry_id: str) -> str:
        drive_service = self._ensure_drive_service() if _is_transcribable_show(_load_entry(entry_id).get("from")) else self._drive_service
        body = get_article_body(entry_id, drive_service=drive_service)
        cached = _load_entry(entry_id)
        deferred_until = _deferred_until(cached)
        if deferred_until and deferred_until > datetime.now().astimezone():
            self.last_error = cached.get("transcription_error", "")
        elif self.last_error.startswith("Transcript quota reached") or self.last_error.startswith("Drive not available for transcripts"):
            self.clear_error()
        return body

    def mark_consumed(self, entry_id: str) -> bool:
        return remove_from_saved(entry_id)

    def _external_consume(self, entry_id: str) -> None:
        remove_from_saved(entry_id)

    def mark_restored(self, entry_id: str) -> bool:
        return mark_unread(entry_id)

    def list_cached(self) -> list[dict]:
        return list_articles_cached()

    def load_entry(self, entry_id: str) -> dict:
        return _load_entry(entry_id)

    def save_entry(self, entry_id: str, data: dict) -> None:
        _save_entry(entry_id, data)
