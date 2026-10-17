# Lazyn't

Lazyn't is a mathematics learning companion for the student who wants the easy way out but accidentally ends up learning something. Work smarter. Unfortunately, still work. It helps students identify the ideas behind an assignment, find learning resources, and make progress through incremental hints and feedback on their own work.

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

Copy `.env.example` to `.env`. Add a long random `DJANGO_SECRET_KEY`, your `NEBIUS_API_KEY`, and an available `NEBIUS_MODEL`. The default Nebius API base URL is `https://api.tokenfactory.nebius.com/v1/`.

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
| `RESOURCE_PREFERRED_DOMAINS` | No | Comma-separated preferred educational domains. Defaults to OpenStax, MIT OpenCourseWare, Khan Academy, and LibreTexts math. |

Without the Nebius key or model, AI endpoints return a clear configuration error. Without a resource provider, the app explicitly reports that resource discovery is unavailable; it does not invent links.

`.env.example` is a template and is not loaded by Django. Put your values in a workspace-root `.env` file (copy `.env.example` to `.env` first), then restart `runserver` so the process loads the updated environment. To enable Tavily, set `RESOURCE_SEARCH_PROVIDER=tavily` and set `TAVILY_API_KEY` in `.env`; the default `none` disables live search even when a key is present.

## Learning flow and API

- `GET /` serves the responsive interface and MathLive/KaTeX assets.
- `POST /api/assignments/analyze/` identifies concepts, prerequisites, and a short sequence of practice steps.
- `GET /api/learning/session/` restores the active session; `?step_id=...` opens an unlocked or completed step for review.
- `POST /api/learning/validate/` validates one active/unlocked step, stores the attempt and feedback, and unlocks the next step only after a structured completion response.
- `POST /api/learning/hint/` returns exactly the next hint for an unlocked step and tracks the hint level in the session.
- `POST /api/resources/search/` accepts prerequisite `concepts`, optionally `feedback` (`too_difficult` or `not_helpful`) and `exclude_urls`, then returns up to three concept-focused provider results or a clear unavailable status.
- `POST /api/tutor/chat/` supports `hint`, `explain`, and `check_work` modes.
- `POST /api/tutor/check/` gives cautious feedback on the student's current mathematical working.

Resource search makes one focused query per prerequisite concept and asks Tavily to prefer configured educational domains. Cards include source snippets, an objective, and a conceptual checkpoint. A lightweight HEAD request reports link reachability when possible; it does not assess source quality or mathematical accuracy. Feedback buttons refine the next search query and exclude already-shown links. No-provider and provider-failure states do not fabricate resources.

The browser sends same-origin JSON requests with Django CSRF tokens. Assignment, generated steps, statuses, hint levels, and recent attempts are kept in the Django session. The server enforces step access independently of the interface. Open-ended reasoning is still assessed by the configured language model; structured response checks and conservative completion rules reduce, but cannot eliminate, incorrect judgments.

## Progressive learning loop

Assignment analysis generates between one and six checkpoints. Only the first is initially available. Each step presents its objective, instructions, prerequisite chips, a practice task, and explicit completion criteria. Students can request hints one at a time, submit or revise an attempt, revisit completed attempts, and continue only after validation. Later steps remain locked server-side until a previous checkpoint is accepted. Prerequisite resources open inline so the student's draft remains in place; resource links open in a new tab.

## Manual smoke test

1. Start Django with the configured environment variables and open the home page.
2. Paste a problem and analyze it; verify only the first step is actionable while later steps are locked.
3. With resource search disabled, verify the interface says discovery is not configured rather than showing fabricated links. Enable Tavily to test concept-focused resource cards.
4. Try each resource checkpoint, discuss an answer with the tutor, and mark a resource too difficult or unhelpful to request an alternative.
5. Open a prerequisite concept, search for resources, close the panel, and verify your draft remains intact.
6. Request hints sequentially and verify only one additional hint appears per request.
7. Submit an incomplete attempt; verify targeted feedback appears and the next step remains locked. Revise and resubmit.
8. Complete a checkpoint and verify the next step unlocks; revisit the completed step and its attempt history.
9. Try empty input, network/provider errors, and a forged request for a locked step; verify progress does not bypass validation.
10. Inspect rendered mathematical expressions and try the MathLive equation editor.
