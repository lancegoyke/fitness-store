"""Service helpers for the challenges app."""

import os

DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"


def generate_challenge_summary(
    description: str,
    api_key: str | None = None,
    model: str = DEFAULT_GEMINI_MODEL,
) -> str:
    """Generate a short summary of a challenge description using Google Gemini.

    Returns a string no longer than 300 characters (the Challenge.summary
    max_length). Raises RuntimeError if no API key is available and ValueError
    if the API returns no text.
    """
    from google import genai

    api_key = api_key or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY not configured")

    client = genai.Client(api_key=api_key)
    prompt = (
        "Summarize the following challenge description in no more than "
        "300 characters: " + description
    )
    response = client.models.generate_content(model=model, contents=prompt)
    if not response.text:
        raise ValueError("Gemini returned an empty summary")
    return response.text[:300]
