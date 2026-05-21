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
clear ideological framing or advocacy. Reserve XL/XR only for content that itself explicitly \
advocates for or promotes an extremist political position. Critical rule: negative framing, \
alarming tone, or reporting on extremist/intolerant views does NOT make an article XR or XL \
— only articles that endorse, celebrate, or call for extreme political action qualify. \
Data journalism, surveys, and analytical pieces on immigration or religion are typically C or \
at most R/L depending on editorial slant. Similarly, journalistic profiles or investigations of \
extremist movements (incels, neo-nazis, etc.) are C or L — the presence of words like \
"misogyny", "hate movement", or "extremist ideology" in the summary means those are the \
article's subject, not its position.

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


def score_political_lean(summary: str, language: str = "") -> dict:
    """Returns {lean, lean_note} or {} on failure."""
    if not summary:
        return {}
    try:
        client = _get_client()
        lang_note = f" Write lean_note in {language.upper()} language." if language and not language.lower().startswith("en") else ""
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=100,
            messages=[
                {"role": "system", "content": _LEAN_PROMPT + lang_note},
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


_TRUST_PROMPT = """\
You are a content quality analyst. Given a summary of any reading item — article, newsletter, \
video, podcast, Reddit post, forum thread, website, or anything else — plus its author/source \
and title, assess its trustworthiness and informational quality.

Consider:
- Are claims specific and appropriately hedged, or vague and absolute?
- Is framing measured or sensationalist (e.g. alarming headlines, overstatement)?
- Does the author/domain seem credible for this type of content?
- Does the content rely on evidence, data, or named sources — or pure assertion?
- For opinion/creative/entertainment content, score on internal consistency and honesty about \
what it is — not on factual rigor.

Score 0–10: 0 = clearly unreliable/clickbait/misinformation, 10 = rigorous, measured, well-sourced.
Most decent content should score 5–8. Reserve 0–3 for obvious clickbait or misinformation, \
9–10 for exceptional rigor.

Write trust_note in the SAME LANGUAGE as the summary.

Return ONLY valid JSON, no other text:
{"trust": <int 0-10>, "trust_note": "<one sentence explaining the score>"}\
"""


def score_trustworthiness(summary: str, author: str = "", title: str = "") -> dict:
    """Returns {trust_score, trust_note} or {} on failure."""
    if not summary:
        return {}
    try:
        client = _get_client()
        user_parts = []
        if title:
            user_parts.append(f"<TITLE>\n{title}\n</TITLE>")
        if author:
            user_parts.append(f"<AUTHOR>\n{author}\n</AUTHOR>")
        user_parts.append(f"<SUMMARY>\n{summary}\n</SUMMARY>")
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=120,
            messages=[
                {"role": "system", "content": _TRUST_PROMPT},
                {"role": "user", "content": "\n\n".join(user_parts)},
            ],
        )
        raw = response.choices[0].message.content.strip()
        data = json.loads(raw)
        trust = int(data["trust"])
        if not (0 <= trust <= 10):
            return {}
        return {"trust_score": trust, "trust_note": str(data.get("trust_note", ""))}
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
