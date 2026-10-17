import json
import logging
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


logger = logging.getLogger(__name__)
BASE_URL = "https://api.tokenfactory.us-north1.nebius.com/v1/"
REQUEST_TIMEOUT_SECONDS = 60


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
        base_url = os.getenv("NEBIUS_BASE_URL", BASE_URL).rstrip("/")
        request = Request(
            f"{base_url}/chat/completions",
            data=json.dumps({
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.3,
            }).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            response_data = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        logger.warning("Nebius request failed with HTTP status %s.", exc.code)
        raise ProviderError("The AI provider could not complete the request. Please try again.") from exc
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        logger.warning("Nebius request failed (%s).", type(exc).__name__)
        raise ProviderError("The AI provider could not complete the request. Please try again.") from exc

    try:
        content = response_data["choices"][0]["message"]["content"]
    except (AttributeError, IndexError, KeyError, TypeError) as exc:
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
