import json
import os
import re
import tempfile
import unicodedata
from pathlib import Path
from shutil import which

from googleapiclient.http import MediaIoBaseDownload
from pydub import AudioSegment
from provider_state import clear_provider_retry, infer_retry_at, is_rate_limit_error, set_provider_retry

from config import GDRIVE_PODCAST_FOLDER, GDRIVE_ARCHIVE_FOLDER

_FOLDER_NAME = GDRIVE_PODCAST_FOLDER
_FOLDER_IDS: list[str] = []

CHUNK_MS = 20 * 60 * 1000  # 20-minute chunks stay well under 25 MB


class TranscriptDeferredError(RuntimeError):
    def __init__(self, retry_at: str, reason: str):
        super().__init__(reason)
        self.retry_at = retry_at
        self.reason = reason

def _is_quota_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(token in text for token in ("quota", "rate limit", "rate_limit", "too many requests", "429"))


def _resolve_binary(env_name: str, candidates: list[str]) -> str:
    explicit = os.getenv(env_name, "").strip()
    if explicit and Path(explicit).exists():
        return explicit
    found = which(candidates[0])
    if found:
        return found
    for candidate in candidates[1:]:
        if Path(candidate).exists():
            return candidate
    raise RuntimeError(f"Missing required binary for audio transcription: {candidates[0]}")


def _configure_audio_tools() -> None:
    AudioSegment.converter = _resolve_binary("FFMPEG_BINARY", [
        "ffmpeg",
        "/usr/local/bin/ffmpeg",
        "/opt/homebrew/bin/ffmpeg",
    ])
    AudioSegment.ffprobe = _resolve_binary("FFPROBE_BINARY", [
        "ffprobe",
        "/usr/local/bin/ffprobe",
        "/opt/homebrew/bin/ffprobe",
    ])


def _resolve_folder_ids(drive_service) -> list[str]:
    global _FOLDER_IDS
    if not _FOLDER_IDS:
        res = drive_service.files().list(
            q=f"name='{_FOLDER_NAME}' and mimeType='application/vnd.google-apps.folder' and trashed=false",
            fields="files(id)",
        ).execute()
        _FOLDER_IDS = [f["id"] for f in res.get("files", [])]
        print(f"[gdrive] resolved {len(_FOLDER_IDS)} folder(s) for '{_FOLDER_NAME}'")
    return _FOLDER_IDS


def _extract_keywords(subject: str) -> list[str]:
    """Extract country/topic keywords from the parenthesized part of an episode title."""
    m = re.search(r"\(([^)]+)\)", subject)
    if not m:
        return []
    return [unicodedata.normalize("NFC", kw.strip().lower()) for kw in re.split(r"[-/]", m.group(1)) if kw.strip()]


def find_episode_file(drive_service, subject: str) -> str | None:
    """Return file_id for the Drive mp3 matching the episode, or None.

    Primary match: date string (YYYY-MM-DD) in filename.
    Fallback: all-keyword match on country/topic names from the title parentheses,
    for cases where Spotify and Drive use different dates for the same episode.
    """
    m = re.search(r"(\d{4}-\d{2}-\d{2})", subject)
    if not m:
        return None
    date = m.group(1)
    folder_ids = _resolve_folder_ids(drive_service)
    parent_filter = ""
    if folder_ids:
        parent_filter = " and (" + " or ".join(f"'{folder_id}' in parents" for folder_id in folder_ids) + ")"

    # Primary: exact date match
    res = drive_service.files().list(
        q=f"name contains '{date}' and mimeType='audio/mpeg' and trashed=false{parent_filter}",
        fields="files(id,name)",
        pageSize=5,
    ).execute()
    files = res.get("files", [])
    if files:
        print(f"[gdrive] matched '{files[0]['name']}' for date {date}")
        return files[0]["id"]

    # Fallback: list all folder mp3s and match on keywords from parentheses
    keywords = _extract_keywords(subject)
    if not keywords:
        print(f"[gdrive] no Drive file found for date {date}")
        return None
    all_files = drive_service.files().list(
        q=f"mimeType='audio/mpeg' and trashed=false{parent_filter}",
        fields="files(id,name)",
        pageSize=100,
    ).execute().get("files", [])
    for f in all_files:
        name_lower = unicodedata.normalize("NFC", f["name"].lower())
        if all(kw in name_lower for kw in keywords):
            print(f"[gdrive] keyword-matched '{f['name']}' for subject '{subject}'")
            return f["id"]

    print(f"[gdrive] no Drive file found for date {date}")
    return None


def _resolve_archive_folder_id(drive_service, parent_id: str) -> str:
    """Find or create the archive subfolder, returning its ID."""
    res = drive_service.files().list(
        q=f"name='{GDRIVE_ARCHIVE_FOLDER}' and mimeType='application/vnd.google-apps.folder' and '{parent_id}' in parents and trashed=false",
        fields="files(id)",
        pageSize=1,
    ).execute()
    hits = res.get("files", [])
    if hits:
        return hits[0]["id"]
    folder = drive_service.files().create(
        body={"name": GDRIVE_ARCHIVE_FOLDER, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]},
        fields="id",
    ).execute()
    archive_id = folder["id"]
    print(f"[gdrive] created '{GDRIVE_ARCHIVE_FOLDER}' folder {archive_id}")
    return archive_id


def move_to_archive(drive_service, file_id: str) -> bool:
    """Move a Drive mp3 into the archive subfolder of the podcast folder."""
    folder_ids = _resolve_folder_ids(drive_service)
    if not folder_ids:
        print(f"[gdrive] cannot move to archive: '{GDRIVE_PODCAST_FOLDER}' not resolved")
        return False
    parent_id = folder_ids[0]
    archive_id = _resolve_archive_folder_id(drive_service, parent_id)
    drive_service.files().update(
        fileId=file_id,
        addParents=archive_id,
        removeParents=parent_id,
        fields="id,parents",
    ).execute()
    print(f"[gdrive] moved {file_id} to '{GDRIVE_ARCHIVE_FOLDER}'")
    return True


def restore_from_archive(drive_service, file_id: str) -> bool:
    """Move a Drive mp3 back from the archive subfolder to the podcast folder."""
    folder_ids = _resolve_folder_ids(drive_service)
    if not folder_ids:
        print(f"[gdrive] cannot restore from archive: '{GDRIVE_PODCAST_FOLDER}' not resolved")
        return False
    parent_id = folder_ids[0]
    archive_id = _resolve_archive_folder_id(drive_service, parent_id)
    drive_service.files().update(
        fileId=file_id,
        addParents=parent_id,
        removeParents=archive_id,
        fields="id,parents",
    ).execute()
    print(f"[gdrive] restored {file_id} from '{GDRIVE_ARCHIVE_FOLDER}'")
    return True


def _chunk_cache_path(file_id: str) -> str:
    return f"/tmp/newsletterman_chunks_{file_id}.json"


def _load_chunk_cache(file_id: str) -> dict[int, str]:
    path = _chunk_cache_path(file_id)
    try:
        with open(path) as f:
            return {int(k): v for k, v in json.load(f).items()}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_chunk_cache(file_id: str, cache: dict[int, str]) -> None:
    with open(_chunk_cache_path(file_id), "w") as f:
        json.dump(cache, f)


def _clear_chunk_cache(file_id: str) -> None:
    try:
        os.unlink(_chunk_cache_path(file_id))
    except FileNotFoundError:
        pass


def transcribe_episode(drive_service, file_id: str, subject: str = "", description: str = "") -> str:
    """Download mp3 from Drive, split into chunks, transcribe via Groq (fallback: OpenAI)."""
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
        tmp_path = tmp.name
        request = drive_service.files().get_media(fileId=file_id)
        downloader = MediaIoBaseDownload(tmp, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
    print(f"[gdrive] downloaded {os.path.getsize(tmp_path) // (1024*1024)} MB")
    try:
        return _transcribe_chunked(tmp_path, file_id=file_id, subject=subject, description=description)
    finally:
        os.unlink(tmp_path)


def _build_whisper_prompt(subject: str, description: str = "") -> str:
    base = f"Podcast o polityce zagranicznej. Odcinek: {subject}." if subject else "Podcast o polityce zagranicznej."
    # Whisper uses the prompt to bias its vocabulary — the episode description
    # from Spotify already contains correctly spelled proper nouns (names, orgs,
    # acronyms, places) that appear in the episode, so appending it gives Whisper
    # the right priors without any manual curation.
    if description:
        # Trim to ~500 chars so the combined prompt stays well under Whisper's ~224-token limit
        snippet = description[:500].strip()
        return f"{base} {snippet}"
    return base


def _transcribe_chunked(mp3_path: str, file_id: str = "", subject: str = "", description: str = "") -> str:
    _configure_audio_tools()
    audio = AudioSegment.from_mp3(mp3_path)
    duration_min = len(audio) // 60000
    chunks = [audio[i:i + CHUNK_MS] for i in range(0, len(audio), CHUNK_MS)]
    print(f"[gdrive] transcribing {duration_min}min audio in {len(chunks)} chunk(s)")
    prompt = _build_whisper_prompt(subject, description=description)
    cache = _load_chunk_cache(file_id) if file_id else {}
    if cache:
        print(f"[gdrive] resuming from chunk cache: {len(cache)}/{len(chunks)} already done")
    parts: list[str] = [""] * len(chunks)
    for i, text in cache.items():
        if i < len(chunks):
            parts[i] = text
    for i, chunk in enumerate(chunks):
        if parts[i]:
            continue
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tf:
            chunk_path = tf.name
        try:
            chunk.export(chunk_path, format="mp3")
            with open(chunk_path, "rb") as fp:
                parts[i] = _transcribe_file(fp, prompt=prompt)
            if file_id:
                cache[i] = parts[i]
                _save_chunk_cache(file_id, cache)
            print(f"[gdrive] chunk {i+1}/{len(chunks)} done")
        finally:
            os.unlink(chunk_path)
    if file_id:
        _clear_chunk_cache(file_id)
    transcript = "\n\n".join(parts)
    print(f"[gdrive] transcript {len(transcript)} chars")
    return transcript


def _transcribe_file(fp, prompt: str = "") -> str:
    groq_key = os.getenv("GROQ_API_KEY")
    if groq_key:
        from groq import Groq
        try:
            result = Groq(api_key=groq_key).audio.transcriptions.create(
                model="whisper-large-v3-turbo",
                file=fp,
                response_format="text",
                prompt=prompt or None,
            )
        except Exception as e:
            if _is_quota_error(e):
                retry_at = infer_retry_at(str(e))
                set_provider_retry(
                    "groq_transcription",
                    retry_at,
                    f"Groq transcription is rate limited. {str(e)}",
                )
                raise TranscriptDeferredError(
                    retry_at=retry_at.isoformat(),
                    reason="Groq transcript quota reached. Podcast transcription will retry later.",
                ) from e
            raise
        clear_provider_retry("groq_transcription")
        return result if isinstance(result, str) else result.text
    else:
        import openai
        try:
            result = openai.OpenAI().audio.transcriptions.create(
                model="whisper-1",
                file=fp,
                response_format="text",
                prompt=prompt or None,
            )
        except Exception as e:
            if is_rate_limit_error(str(e)):
                retry_at = infer_retry_at(str(e))
                set_provider_retry(
                    "openai_transcription",
                    retry_at,
                    f"OpenAI transcription is rate limited. {str(e)}",
                )
                raise TranscriptDeferredError(
                    retry_at=retry_at.isoformat(),
                    reason="OpenAI transcript quota reached. Podcast transcription will retry later.",
                ) from e
            raise
        clear_provider_retry("openai_transcription")
        return result if isinstance(result, str) else result.text
