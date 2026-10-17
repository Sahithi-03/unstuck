import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .nebius import ProviderError


TAVILY_ENDPOINT = "https://api.tavily.com/search"


def search_resources(queries, concept=""):
    provider = os.getenv("RESOURCE_SEARCH_PROVIDER", "none").lower()
    if provider == "none":
        return {
            "available": False,
            "message": "Resource discovery is not configured. Set RESOURCE_SEARCH_PROVIDER=tavily and TAVILY_API_KEY.",
            "resources": [],
        }
    if provider != "tavily":
        raise ProviderError("The configured resource search provider is not supported.")
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        return {
            "available": False,
            "message": "Tavily resource search is selected but TAVILY_API_KEY is missing.",
            "resources": [],
        }

    query = " ".join(queries[:3])
    if concept:
        query += f" {concept}"
    body = json.dumps(
        {
            "api_key": api_key,
            "query": query[:600],
            "topic": "general",
            "max_results": 3,
            "include_answer": False,
        }
    ).encode("utf-8")
    request = Request(
        TAVILY_ENDPOINT,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=12) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ProviderError("Resource search is temporarily unavailable. Please try again.") from exc
    except OSError as exc:
        raise ProviderError("Resource search is temporarily unavailable. Please try again.") from exc

    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        raise ProviderError("The resource search provider returned an invalid response.", 502)
    resources = []
    for item in results:
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        url = item.get("url")
        parsed_url = urlparse(url) if isinstance(url, str) else None
        if (
            not isinstance(title, str)
            or not title.strip()
            or parsed_url is None
            or parsed_url.scheme not in ("https", "http")
            or not parsed_url.netloc
        ):
            continue
        snippet = item.get("content")
        resources.append(
            {
                "title": title[:200],
                "url": url,
                "source": parsed_url.netloc,
                "concept": concept or queries[0],
                "relevance": snippet[:500] if isinstance(snippet, str) and snippet else (
                    "Found by the configured search provider for this learning topic."
                ),
            }
        )
        if len(resources) == 3:
            break
    return {"available": True, "message": "", "resources": resources}
