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


def summarize(text: str, subject: str) -> str:
    client = _get_client()
    polish = _is_polish(text)
    lang_instruction = (
        "Write the summary in Polish." if polish
        else "Write the summary in English."
    )
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        max_tokens=800,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a newsletter summarizer. Given the text of a newsletter, "
                    "write a concise summary in 3-5 bullet points. Be specific and highlight "
                    "the most actionable or interesting content. Skip ads and boilerplate. "
                    "If the newsletter contains items with source URLs, include each URL as a "
                    "markdown link (e.g. [Czytaj więcej](url) or [Read more](url)) at the end "
                    f"of the relevant bullet. {lang_instruction}"
                ),
            },
            {
                "role": "user",
                "content": f"Newsletter: {subject}\n\n{text[:8000]}",
            },
        ],
    )
    return response.choices[0].message.content
