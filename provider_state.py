import json
import re
from datetime import datetime, timedelta
from pathlib import Path

_STATE_PATH = Path(__file__).parent / ".cache" / "provider_state.json"


def _load_state() -> dict:
    if not _STATE_PATH.exists():
        return {}
    try:
        return json.loads(_STATE_PATH.read_text())
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    _STATE_PATH.parent.mkdir(exist_ok=True)
    _STATE_PATH.write_text(json.dumps(state, indent=2))


def provider_retry_at(provider: str) -> datetime | None:
    state = _load_state().get(provider, {})
    raw = state.get("retry_at", "")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except Exception:
        return None


def provider_error(provider: str) -> str:
    state = _load_state().get(provider, {})
    return str(state.get("reason", "") or "")


def clear_provider_retry(provider: str) -> None:
    state = _load_state()
    if provider in state:
        state.pop(provider, None)
        _save_state(state)


def set_provider_retry(provider: str, retry_at: datetime, reason: str) -> None:
    state = _load_state()
    state[provider] = {
        "retry_at": retry_at.isoformat(),
        "reason": reason,
    }
    _save_state(state)


def parse_retry_after_seconds(error_text: str) -> int | None:
    match = re.search(r"retry (?:will occur )?after:\s*(\d+)\s*s", error_text, flags=re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None


def infer_retry_at(error_text: str) -> datetime:
    now = datetime.now().astimezone()
    retry_after = parse_retry_after_seconds(error_text)
    if retry_after is not None:
        return now + timedelta(seconds=retry_after)
    return now + timedelta(minutes=15)


def is_rate_limit_error(error_text: str) -> bool:
    lowered = error_text.lower()
    tokens = (
        "rate limit",
        "rate_limit",
        "too many requests",
        "retry after",
        "retry will occur after",
        "429",
        "quota",
    )
    return any(token in lowered for token in tokens)
