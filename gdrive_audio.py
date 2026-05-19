import os
import re
import tempfile

from googleapiclient.http import MediaIoBaseDownload
from pydub import AudioSegment

_FOLDER_NAME = "3R Podsumowania tygodnia"
_FOLDER_IDS: list[str] = []

CHUNK_MS = 20 * 60 * 1000  # 20-minute chunks stay well under 25 MB


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


def find_episode_file(drive_service, subject: str) -> str | None:
    """Return file_id for the Drive mp3 whose title contains the episode date, or None."""
    m = re.search(r"(\d{4}-\d{2}-\d{2})", subject)
    if not m:
        return None
    date = m.group(1)
    folder_ids = _resolve_folder_ids(drive_service)
    parent_filter = ""
    if folder_ids:
        parent_filter = " and (" + " or ".join(f"'{folder_id}' in parents" for folder_id in folder_ids) + ")"
    res = drive_service.files().list(
        q=f"name contains '{date}' and mimeType='audio/mpeg' and trashed=false{parent_filter}",
        fields="files(id,name)",
        pageSize=5,
    ).execute()
    files = res.get("files", [])
    if files:
        print(f"[gdrive] matched '{files[0]['name']}' for date {date}")
        return files[0]["id"]
    print(f"[gdrive] no Drive file found for date {date}")
    return None


def transcribe_episode(drive_service, file_id: str) -> str:
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
        return _transcribe_chunked(tmp_path)
    finally:
        os.unlink(tmp_path)


def _transcribe_chunked(mp3_path: str) -> str:
    audio = AudioSegment.from_mp3(mp3_path)
    duration_min = len(audio) // 60000
    chunks = [audio[i:i + CHUNK_MS] for i in range(0, len(audio), CHUNK_MS)]
    print(f"[gdrive] transcribing {duration_min}min audio in {len(chunks)} chunk(s)")
    parts = []
    for i, chunk in enumerate(chunks):
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tf:
            chunk_path = tf.name
        try:
            chunk.export(chunk_path, format="mp3")
            with open(chunk_path, "rb") as fp:
                parts.append(_transcribe_file(fp))
            print(f"[gdrive] chunk {i+1}/{len(chunks)} done")
        finally:
            os.unlink(chunk_path)
    transcript = "\n\n".join(parts)
    print(f"[gdrive] transcript {len(transcript)} chars")
    return transcript


def _transcribe_file(fp) -> str:
    groq_key = os.getenv("GROQ_API_KEY")
    if groq_key:
        from groq import Groq
        result = Groq(api_key=groq_key).audio.transcriptions.create(
            model="whisper-large-v3-turbo",
            file=fp,
            response_format="text",
        )
        return result if isinstance(result, str) else result.text
    else:
        import openai
        result = openai.OpenAI().audio.transcriptions.create(
            model="whisper-1",
            file=fp,
            response_format="text",
        )
        return result if isinstance(result, str) else result.text
