# Unstuck

Unstuck is a small Django-based mathematics learning companion. It helps students identify the ideas behind an assignment, find learning resources, and make progress through incremental hints and feedback on their own work.

The tutor is designed not to return a complete solution. Server-side response validation and an answer-disclosure guard provide an additional check, but heuristic safeguards cannot guarantee that a language model will never reveal an answer. Review AI output as part of any classroom deployment.

## Requirements

- Python 3.10 or newer
- A Nebius AI Studio API key and a model ID available to your account
- Optional: Tavily API key for live resource discovery

## Setup

From the project directory, create and activate a virtual environment, then install requirements:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python manage.py migrate
```

Copy `.env.example` to `.env`. Add a long random `DJANGO_SECRET_KEY`, your `NEBIUS_API_KEY`, and an available `NEBIUS_MODEL`. The default Nebius API base URL is `https://api.studio.nebius.com/v1`.

Run Django's checks and tests, then start the development server:

```powershell
python manage.py check
python manage.py test
python manage.py runserver
```

Open <http://127.0.0.1:8000/>.

## Configuration

| Variable | Required | Purpose |
| --- | --- | --- |
| `DJANGO_SECRET_KEY` | For deployment | Django signing key; the development fallback is not secure for production. |
| `DJANGO_DEBUG` | No | Enables Django debug mode; defaults to `true` for local development. |
| `DJANGO_ALLOWED_HOSTS` | No | Comma-separated host names; defaults to localhost and 127.0.0.1. |
| `NEBIUS_API_KEY` | For AI features | Server-side Nebius API credential. |
| `NEBIUS_BASE_URL` | No | Nebius OpenAI-compatible API URL; defaults to `https://api.studio.nebius.com/v1`. |
| `NEBIUS_MODEL` | For AI features | Model ID enabled for your Nebius account. |
| `RESOURCE_SEARCH_PROVIDER` | No | `none` (default) or `tavily`. |
| `TAVILY_API_KEY` | For Tavily | Enables the optional live educational resource search. |

Without the Nebius key or model, AI endpoints return a clear configuration error. Without a resource provider, the app explicitly reports that resource discovery is unavailable; it does not invent links.

## Learning flow and API

- `GET /` serves the responsive interface and MathLive/KaTeX assets.
- `POST /api/assignments/analyze/` identifies concepts, prerequisites, an ordered learning path, and search queries.
- `POST /api/resources/search/` returns up to three real provider results, or a clear unavailable status.
- `POST /api/tutor/chat/` supports `hint`, `explain`, and `check_work` modes.
- `POST /api/tutor/check/` gives cautious feedback on the student's current mathematical working.

The browser sends same-origin JSON requests with Django CSRF tokens. Assignment and recent conversation context, hint level, and learning stage are kept in the Django session. SQLite is configured as the database; this MVP does not need user accounts or custom database models.

## Manual smoke test

1. Start Django with the configured environment variables and open the home page.
2. Paste a problem and analyze it; verify the summary, concepts, prerequisites, and ordered path appear.
3. With resource search disabled, verify the interface says discovery is not configured rather than showing fabricated links. Enable Tavily to test real resource cards.
4. Enter a question and a line of working, request a hint, then ask another question; verify the hint level and conversation continue.
5. Switch to “Explain a concept” and “Check my thinking”; confirm the responses are mode-appropriate and include uncertainty in working checks.
6. Try empty input and a deliberately invalid request; verify readable errors appear and the page remains usable.
7. Inspect mathematical expressions in responses and try the MathLive equation editor.
