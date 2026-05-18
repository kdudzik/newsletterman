from openai import OpenAI

_client = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI()
    return _client


_POLISH_CHARS = set("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ")


def _is_polish(text: str) -> bool:
    sample = text[:1000]
    return sum(1 for c in sample if c in _POLISH_CHARS) >= 5


def summarize(text: str, subject: str, is_article: bool = False, is_video: bool = False, is_podcast: bool = False, language: str = "") -> str:
    client = _get_client()
    polish = (language == "pl") if language else _is_polish(text + " " + subject)
    lang_instruction = (
        "Write the summary in Polish." if polish
        else "Write the summary in English."
    )
    if is_podcast:
        system_prompt = (
            "You are a podcast summarizer. Given the description of a podcast episode, "
            "write a concise summary in 3-5 bullet points. Be specific and highlight "
            "the most interesting or useful content. Do not include any URLs or links. "
            f"{lang_instruction}"
        )
        user_content = f"Podcast episode: {subject}\n\n{text[:8000]}"
    elif is_video:
        system_prompt = (
            "You are a video summarizer. Given a transcript or description of a YouTube video, "
            "write a concise summary in 3-5 bullet points. Be specific and highlight "
            "the most interesting or useful content. Do not include any URLs or links. "
            f"{lang_instruction}"
        )
        user_content = f"Video: {subject}\n\n{text[:8000]}"
    elif is_article:
        system_prompt = (
            "You are an article summarizer. Given the text of an article, "
            "write a concise summary in 3-5 bullet points. Be specific and highlight "
            "the most actionable or interesting content. Skip ads and boilerplate. "
            f"Do not include any URLs or links. {lang_instruction}"
        )
        user_content = f"Article: {subject}\n\n{text[:8000]}"
    else:
        system_prompt = (
            "You are a newsletter summarizer. Given the text of a newsletter, "
            "write a concise summary in 3-5 bullet points. Be specific and highlight "
            "the most actionable or interesting content. Skip ads and boilerplate. "
            "If the newsletter contains items with source URLs, include each URL as a "
            "markdown link (e.g. [Czytaj więcej](url) or [Read more](url)) at the end "
            f"of the relevant bullet. {lang_instruction}"
        )
        user_content = f"Newsletter: {subject}\n\n{text[:8000]}"
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        max_tokens=800,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    )
    return response.choices[0].message.content
