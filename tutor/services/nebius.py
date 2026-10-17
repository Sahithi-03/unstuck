import json
import os

from openai import OpenAI, OpenAIError


BASE_URL = "https://api.tokenfactory.nebius.com/v1/"
REQUEST_TIMEOUT_SECONDS = 25


class ProviderError(Exception):
    """A safe-to-display error from the configured AI provider."""

    def __init__(self, message, status_code=503):
        super().__init__(message)
        self.status_code = status_code


def complete_json(system_prompt, user_prompt):
    api_key = os.getenv("NEBIUS_API_KEY")
    model = os.getenv("NEBIUS_MODEL")
    if not api_key or not model:
        raise ProviderError("AI tutoring is not configured. Set NEBIUS_API_KEY and NEBIUS_MODEL.")

    try:
        client = OpenAI(
            api_key=api_key,
            base_url=os.getenv("NEBIUS_BASE_URL", BASE_URL),
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.3,
        )
    except OpenAIError as exc:
        raise ProviderError("The AI provider could not complete the request. Please try again.") from exc

    try:
        content = response.choices[0].message.content
    except (AttributeError, IndexError, TypeError) as exc:
        raise ProviderError("The AI provider returned an invalid response.", 502) from exc
    if not isinstance(content, str):
        raise ProviderError("The AI provider returned an invalid response.", 502)

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ProviderError("The AI provider returned malformed JSON.", 502) from exc
    if not isinstance(parsed, dict):
        raise ProviderError("The AI provider returned an invalid response shape.", 502)
    return parsed
