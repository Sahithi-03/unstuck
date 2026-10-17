import ipaddress
import json
import os
import re
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from .nebius import ProviderError


TAVILY_ENDPOINT = "https://api.tavily.com/search"
MAX_RESOURCES = 3
MAX_CONCEPTS = 5
DEFAULT_PREFERRED_DOMAINS = (
    "openstax.org",
    "ocw.mit.edu",
    "khanacademy.org",
    "math.libretexts.org",
)
SOURCE_NAMES = {
    "openstax.org": "OpenStax",
    "ocw.mit.edu": "MIT OpenCourseWare",
    "khanacademy.org": "Khan Academy",
    "math.libretexts.org": "LibreTexts",
}
DOMAIN_PATTERN = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
FEEDBACK_QUERY_STYLE = {
    "too_difficult": "beginner-friendly intuitive explanation with a simple example",
    "not_helpful": "alternative explanation with diagrams or worked examples",
}


def _preferred_domains():
    configured = os.getenv("RESOURCE_PREFERRED_DOMAINS")
    raw_domains = (
        configured.split(",")
        if configured is not None
        else DEFAULT_PREFERRED_DOMAINS
    )
    domains = []
    for raw_domain in raw_domains:
        domain = raw_domain.strip().lower().removeprefix("www.")
        if domain and DOMAIN_PATTERN.fullmatch(domain) and domain not in domains:
            domains.append(domain)
    return domains


def _public_host(hostname):
    hostname = hostname.rstrip(".").lower()
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".local"):
        return False
    try:
        return ipaddress.ip_address(hostname).is_global
    except ValueError:
        pass

    try:
        addresses = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    except OSError:
        return None
    if not addresses:
        return None
    try:
        return all(ipaddress.ip_address(record[4][0]).is_global for record in addresses)
    except ValueError:
        return False


def _normalize_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        return None
    try:
        parsed = urlsplit(value.strip())
        hostname = parsed.hostname
        if (
            parsed.scheme.lower() not in {"https", "http"}
            or not hostname
            or parsed.username
            or parsed.password
            or not parsed.netloc
            or parsed.port is not None and not 1 <= parsed.port <= 65535
        ):
            return None
    except ValueError:
        return None
    normalized_netloc = hostname.lower()
    if ":" in normalized_netloc:
        normalized_netloc = f"[{normalized_netloc}]"
    if parsed.port:
        normalized_netloc = f"{normalized_netloc}:{parsed.port}"
    normalized = urlunsplit(
        (parsed.scheme.lower(), normalized_netloc, parsed.path.rstrip("/") or "/", parsed.query, "")
    )
    return normalized


def _query_for(concept, feedback=None):
    query = f"{concept} mathematics educational notes tutorial"
    style = FEEDBACK_QUERY_STYLE.get(feedback)
    if style:
        query = f"{concept} mathematics {style}"
    return query[:400]


def _tavily_search(query, preferred_domains, api_key):
    body = {
        "api_key": api_key,
        "query": query,
        "topic": "general",
        "max_results": 5,
        "include_answer": False,
    }
    if preferred_domains:
        body["include_domains"] = preferred_domains
    request = Request(
        TAVILY_ENDPOINT,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=12) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        raise ProviderError(
            "Resource search is temporarily unavailable. Please try again."
        ) from exc
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        raise ProviderError("The resource search provider returned an invalid response.", 502)
    return results


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def check_accessibility(url):
    normalized = _normalize_url(url)
    if not normalized:
        return {"link_status": "not_checked", "link_check_detail": "URL was not eligible for a link check."}
    hostname = urlsplit(normalized).hostname
    public = _public_host(hostname)
    if public is not True:
        return {"link_status": "not_checked", "link_check_detail": "Public host could not be confirmed."}

    request = Request(normalized, method="HEAD", headers={"User-Agent": "UnstuckResourceChecker/1.0"})
    opener = build_opener(_NoRedirectHandler())
    try:
        with opener.open(request, timeout=4) as response:
            status = response.status
    except HTTPError as exc:
        if exc.code in {404, 410}:
            return {
                "link_status": "unavailable",
                "link_check_detail": f"Link check returned HTTP {exc.code}.",
            }
        return {
            "link_status": "not_checked",
            "link_check_detail": f"Link check could not confirm availability (HTTP {exc.code}).",
        }
    except (URLError, TimeoutError, OSError):
        return {"link_status": "not_checked", "link_check_detail": "Link reachability could not be checked."}

    if 200 <= status < 300:
        return {
            "link_status": "accessible",
            "link_check_detail": f"Link check received HTTP {status}; this does not assess educational or mathematical quality.",
        }
    return {
        "link_status": "not_checked",
        "link_check_detail": "Link reachability could not be confirmed.",
    }


def _source_name(hostname):
    hostname = hostname.lower().removeprefix("www.")
    for domain, name in SOURCE_NAMES.items():
        if hostname == domain or hostname.endswith(f".{domain}"):
            return name
    return hostname


def _resource_from_result(item, concept):
    if not isinstance(item, dict):
        return None
    title = item.get("title")
    url = _normalize_url(item.get("url"))
    if not isinstance(title, str) or not title.strip() or not url:
        return None

    hostname = urlsplit(url).hostname
    snippet = item.get("content")
    if isinstance(snippet, str) and snippet.strip():
        relevance = f"The search result describes: {snippet.strip()[:350]}"
    else:
        relevance = (
            f"Returned for a focused search on {concept}; check that the source covers this concept."
        )
    section = item.get("section")
    if not isinstance(section, str):
        section = ""
    return {
        "title": title.strip()[:200],
        "url": url,
        "source": _source_name(hostname),
        "concept": concept,
        "relevance": relevance,
        "suggested_section": section.strip()[:200],
        "learning_objective": f"Explain the main idea behind {concept} and when it is useful.",
        "checkpoint_question": (
            f"What key idea or rule does this resource use to explain {concept}, "
            "and how could you recognize when it applies?"
        ),
    }


def search_resources(concepts, feedback=None, exclude_urls=None):
    provider = os.getenv("RESOURCE_SEARCH_PROVIDER", "none").strip().lower()
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

    excluded = {
        normalized
        for value in (exclude_urls or [])
        if (normalized := _normalize_url(value))
    }
    preferred_domains = _preferred_domains()
    resources = []
    seen_urls = set(excluded)
    failures = 0
    concepts_searched = 0
    unique_concepts = []
    seen_concepts = set()
    for concept in concepts:
        key = concept.strip().casefold()
        if key and key not in seen_concepts:
            unique_concepts.append(concept.strip())
            seen_concepts.add(key)
    for concept in unique_concepts[:MAX_CONCEPTS]:
        concepts_searched += 1
        query = _query_for(concept, feedback)
        try:
            results = _tavily_search(query, preferred_domains, api_key)
        except ProviderError:
            failures += 1
            continue

        for item in results:
            resource = _resource_from_result(item, concept)
            if not resource or resource["url"] in seen_urls:
                continue
            seen_urls.add(resource["url"])
            resource.update(check_accessibility(resource["url"]))
            resources.append(resource)
            break
        if len(resources) >= MAX_RESOURCES:
            break

    if not resources and failures:
        return {
            "available": False,
            "message": "Resource search is temporarily unavailable. Please try again later.",
            "resources": [],
        }
    if not resources:
        message = "No matching resources were found for these concepts."
    elif failures:
        message = "Some concept searches were unavailable; showing results from successful searches."
    else:
        message = ""
    return {"available": True, "message": message, "resources": resources}
