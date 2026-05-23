from openai import OpenAI

_client = None

_MODEL = "gpt-4o-mini"
_MAX_OUTPUT_TOKENS = 800
_SHORT_TEXT_CHARS = 12000
_CHUNK_TARGET_CHARS = 9000
_CHUNK_OVERLAP_CHARS = 600
_MAX_CHUNKS = 8


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI()
    return _client


_POLISH_CHARS = set("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ")


def _is_polish(text: str) -> bool:
    sample = text[:1000]
    return sum(1 for c in sample if c in _POLISH_CHARS) >= 5


def _kind(is_article: bool = False, is_video: bool = False, is_podcast: bool = False) -> str:
    if is_podcast:
        return "podcast"
    if is_video:
        return "video"
    if is_article:
        return "article"
    return "newsletter"


def _lang_instruction(polish: bool) -> str:
    return "Write the summary in Polish." if polish else "Write the summary in English."


def _system_prompt(kind: str, polish: bool, partial: bool = False) -> str:
    lang_instruction = _lang_instruction(polish)
    if partial:
        partial_instruction = (
            "Summarize only this fragment. Focus on concrete claims, events, and takeaways "
            "from this section. If this fragment is mostly filler, keep the output brief. "
            "Use 2-4 bullet points."
        )
    else:
        partial_instruction = (
            "Write a concise summary in 3-5 bullet points. Be specific and highlight the "
            "most important or useful content. Use neutral, reportorial language — describe "
            "what the article reports or argues without adopting its emotional tone or framing."
        )

    if kind == "podcast":
        return (
            "You are a podcast summarizer. "
            f"{partial_instruction} "
            "For long transcripts, make sure the final summary covers all major topics from "
            "across the episode, not just the opening section. "
            "Do not include any URLs or links. "
            f"{lang_instruction}"
        )
    if kind == "video":
        return (
            "You are a video summarizer. "
            f"{partial_instruction} "
            "Do not include any URLs or links. "
            f"{lang_instruction}"
        )
    if kind == "article":
        return (
            "You are an article summarizer. "
            f"{partial_instruction} "
            "Skip ads and boilerplate. Do not include any URLs or links. "
            f"{lang_instruction}"
        )
    return (
        "You are a newsletter summarizer. "
        f"{partial_instruction} "
        "Skip ads and boilerplate. If the newsletter contains items with source URLs, include "
        "each URL as a markdown link (e.g. [Czytaj więcej](url) or [Read more](url)) at the end "
        f"of the relevant bullet. {lang_instruction}"
    )


def _combine_prompt(kind: str, polish: bool) -> str:
    lang_instruction = _lang_instruction(polish)
    link_instruction = (
        "Preserve relevant markdown links in the final bullets where they add value. "
        if kind == "newsletter" else
        "Do not include any URLs or links. "
    )
    return (
        f"You are a {kind} summarizer. Combine fragment summaries into one clean final summary. "
        "Deduplicate repeated points, preserve important distinctions, and ensure the final "
        "result covers the major topics across the whole source. Use 3-6 bullet points. "
        "Do not mention fragment numbers. "
        f"{link_instruction}"
        f"{lang_instruction}"
    )


def _content_label(kind: str) -> str:
    return {
        "podcast": "Podcast episode",
        "video": "Video",
        "article": "Article",
        "newsletter": "Newsletter",
    }[kind]


def _chat(system_prompt: str, user_content: str) -> str:
    client = _get_client()
    response = client.chat.completions.create(
        model=_MODEL,
        max_tokens=_MAX_OUTPUT_TOKENS,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    )
    return (response.choices[0].message.content or "").strip()


def _find_split_point(text: str, start: int, target_end: int, hard_end: int) -> int:
    for marker in ("\n\n", "\n", ". ", "! ", "? ", "; ", ", "):
        idx = text.rfind(marker, start, hard_end)
        if idx != -1 and idx >= target_end - 2000:
            return idx + len(marker)
    return hard_end


def _chunk_text(text: str, target_chars: int = _CHUNK_TARGET_CHARS, overlap_chars: int = _CHUNK_OVERLAP_CHARS) -> list[str]:
    stripped = text.strip()
    if len(stripped) <= _SHORT_TEXT_CHARS:
        return [stripped]

    chunks = []
    start = 0
    total = len(stripped)
    while start < total and len(chunks) < _MAX_CHUNKS:
        target_end = min(total, start + target_chars)
        hard_end = min(total, start + target_chars + 2000)
        if target_end >= total:
            chunks.append(stripped[start:total].strip())
            break
        end = _find_split_point(stripped, start, target_end, hard_end)
        if end - start < target_chars // 2:
            end = hard_end
        chunk = stripped[start:end].strip()
        if not chunk:
            break
        chunks.append(chunk)
        if end >= total:
            break
        next_start = max(end - overlap_chars, start + 1)
        if next_start >= total:
            break
        start = next_start

    if not chunks:
        return [stripped[:target_chars]]

    if chunks[-1] != stripped and len(chunks) == _MAX_CHUNKS and start < total:
        tail = stripped[max(total - target_chars, 0):].strip()
        if tail and tail != chunks[-1]:
            chunks[-1] = tail
    return chunks


def _single_pass_summary(text: str, subject: str, kind: str, polish: bool) -> str:
    label = _content_label(kind)
    system_prompt = _system_prompt(kind, polish, partial=False)
    user_content = f"{label}: {subject}\n\n{text}"
    return _chat(system_prompt, user_content)


def _partial_summary(chunk: str, subject: str, kind: str, polish: bool, index: int, total: int) -> str:
    label = _content_label(kind)
    system_prompt = _system_prompt(kind, polish, partial=True)
    user_content = (
        f"{label}: {subject}\n"
        f"Fragment {index}/{total}\n\n"
        f"{chunk}"
    )
    return _chat(system_prompt, user_content)


def _combine_summaries(partials: list[str], subject: str, kind: str, polish: bool) -> str:
    label = _content_label(kind)
    joined = "\n\n".join(
        f"[Fragment summary {idx}/{len(partials)}]\n{summary}"
        for idx, summary in enumerate(partials, start=1)
    )
    user_content = (
        f"{label}: {subject}\n\n"
        "Combine these fragment summaries into one final summary:\n\n"
        f"{joined}"
    )
    return _chat(_combine_prompt(kind, polish), user_content)


def _fact_check_summary(summary: str, subject: str) -> str:
    """Use a stronger model to catch proper-noun errors introduced by ASR or the summarizer."""
    client = _get_client()
    system = (
        "You are a spelling corrector for a foreign-policy podcast summary written in Polish. "
        "The summary was generated from a speech-recognition transcript and may contain "
        "garbled proper nouns — misspelled names of people, organizations, acronyms, or places. "
        "Fix ONLY clear ASR transcription errors: wrong letters, missing diacritics, "
        "garbled acronym capitalisation (e.g. CONAIE not Conaie, RAE not RAJ), "
        "or phonetically garbled foreign phrases (e.g. 'Cartel de los Soles' not 'Choles'). "
        "NEVER substitute one real person or entity for another, even if the surrounding "
        "sentence seems factually wrong to you — that is not your job. "
        "Do not change facts, numbers, dates, or Polish grammar. "
        "Return only the corrected summary text, no preamble, no explanations."
    )
    response = client.chat.completions.create(
        model="gpt-4o",
        max_tokens=_MAX_OUTPUT_TOKENS,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": f"Episode: {subject}\n\nSummary:\n{summary}"},
        ],
    )
    result = (response.choices[0].message.content or "").strip()
    # Strip any "Episode:/Summary:" header the model may have echoed back
    if result.startswith("Episode:"):
        result = result.split("\n\n", 1)[-1] if "\n\n" in result else result
    if result.lower().startswith("summary:"):
        result = result[result.index(":") + 1:].lstrip()
    return result.strip() or summary


def summarize(
    text: str,
    subject: str,
    is_article: bool = False,
    is_video: bool = False,
    is_podcast: bool = False,
    language: str = "",
) -> str:
    kind = _kind(is_article=is_article, is_video=is_video, is_podcast=is_podcast)
    polish = (language == "pl") if language else _is_polish(text + " " + subject)
    clean_text = text.strip()
    if len(clean_text) <= _SHORT_TEXT_CHARS:
        summary = _single_pass_summary(clean_text, subject, kind, polish)
    else:
        chunks = _chunk_text(clean_text)
        if len(chunks) == 1:
            summary = _single_pass_summary(chunks[0], subject, kind, polish)
        else:
            partials = []
            for idx, chunk in enumerate(chunks, start=1):
                partial = _partial_summary(chunk, subject, kind, polish, idx, len(chunks))
                if partial:
                    partials.append(partial)
            if not partials:
                summary = _single_pass_summary(clean_text[:_SHORT_TEXT_CHARS], subject, kind, polish)
            elif len(partials) == 1:
                summary = partials[0]
            else:
                summary = _combine_summaries(partials, subject, kind, polish)

    if kind == "podcast" and summary:
        summary = _fact_check_summary(summary, subject)
    return summary
