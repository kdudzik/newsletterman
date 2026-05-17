import json
import os
from openai import OpenAI

_LEAN_PROMPT = """\
You are a political media analyst. Given a summary of a reading item such as a newsletter \
or article, classify its political lean based solely on the content and framing — not the \
reader's views.

Use this scale:
- XL: Extreme Left
- L:  Left / Centre-Left
- C:  Centre / Apolitical / Non-political
- R:  Right / Centre-Right
- XR: Extreme Right

Most reading items (tech, science, finance, lifestyle) are C. Reserve L/R for content with \
clear ideological framing or advocacy. Reserve XL/XR only for explicitly partisan or \
extremist content.

Write the lean_note in the SAME LANGUAGE as the summary.

Return ONLY valid JSON, no other text:
{"lean": "<XL|L|C|R|XR>", "lean_note": "<one sentence explaining the classification>"}\
"""

_client = None
_context_cache: dict[str, str] = {}


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI()
    return _client


def _load_context(path: str) -> str:
    if path not in _context_cache:
        with open(path, encoding="utf-8") as f:
            _context_cache[path] = f.read()
    return _context_cache[path]


_SYSTEM_PROMPT = """\
You are a personal reading advisor. You will be given a description of a reader's values, \
worldview, and life priorities, followed by a summary of a reading item such as a \
newsletter or article. Score the item on two dimensions:

1. RELEVANCE (0–10): How well does this content align with the reader's stated values, \
interests, and life areas? 10 = directly serves their priorities; 0 = completely irrelevant.

2. CHALLENGE (0–10): How much does this content tension, expand, or push back on their \
existing worldview? Consider: does it introduce perspectives they haven't considered, \
challenge assumptions implicit in their values, or cover topics outside their usual frame? \
10 = strongly challenges or expands; 0 = fully confirms what they already believe.

Write both notes in the SAME LANGUAGE as the item summary. Address the reader \
directly in second person (e.g. "You" in English, "Ty"/"Twoje" in Polish).

Return ONLY valid JSON in this exact shape, no other text:
{
  "relevance": <int>,
  "relevance_note": "<one sentence addressed to the reader explaining why this is or isn't relevant to their values or life areas>",
  "challenge": <int>,
  "challenge_note": "<one sentence addressed to the reader explaining what assumption or belief this challenges, or why it doesn't>"
}\
"""


def score_political_lean(summary: str) -> dict:
    """Returns {lean, lean_note} or {} on failure."""
    if not summary:
        return {}
    try:
        client = _get_client()
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=100,
            messages=[
                {"role": "system", "content": _LEAN_PROMPT},
                {"role": "user", "content": f"<SUMMARY>\n{summary}\n</SUMMARY>"},
            ],
        )
        raw = response.choices[0].message.content.strip()
        data = json.loads(raw)
        lean = str(data["lean"]).upper()
        if lean not in ("XL", "L", "C", "R", "XR"):
            return {}
        return {"lean": lean, "lean_note": str(data.get("lean_note", ""))}
    except Exception:
        return {}


def score_newsletter(summary: str, context_path: str) -> dict:
    """Returns {relevance, relevance_note, challenge, challenge_note} or {} on failure."""
    if not summary or not context_path:
        return {}
    try:
        context = _load_context(context_path)
    except OSError:
        return {}
    try:
        client = _get_client()
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=200,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"<CONTEXT>\n{context}\n</CONTEXT>\n\n<SUMMARY>\n{summary}\n</SUMMARY>",
                },
            ],
        )
        raw = response.choices[0].message.content.strip()
        data = json.loads(raw)
        return {
            "relevance_score": int(data["relevance"]),
            "relevance_note": str(data.get("relevance_note", "")),
            "challenge_score": int(data["challenge"]),
            "challenge_note": str(data.get("challenge_note", "")),
        }
    except Exception:
        return {}
