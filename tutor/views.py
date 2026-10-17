import json

from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from .services.nebius import ProviderError
from .services.resources import search_resources as discover_resources
from .services.tutor import (
    MAX_HISTORY_MESSAGES,
    analyze_assignment as build_analysis,
    check_work as evaluate_work,
    tutor_chat as build_chat_response,
)


MAX_BODY_BYTES = 24_000
MAX_ASSIGNMENT_LENGTH = 6_000
MAX_CONTEXT_LENGTH = 3_000
MAX_MESSAGE_LENGTH = 3_000
MAX_WORKING_LENGTH = 5_000
VALID_MODES = {"hint", "explain", "check_work"}


class RequestValidationError(Exception):
    pass


@require_GET
def index(request):
    return render(request, "tutor/index.html")


def _read_json(request):
    if len(request.body) > MAX_BODY_BYTES:
        raise RequestValidationError("Request body is too large.")
    try:
        payload = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RequestValidationError("Send a valid JSON request.") from exc
    if not isinstance(payload, dict):
        raise RequestValidationError("The JSON request must be an object.")
    return payload


def _text(payload, key, *, required=False, max_length):
    value = payload.get(key, "")
    if not isinstance(value, str):
        raise RequestValidationError(f"{key} must be text.")
    value = value.strip()
    if required and not value:
        raise RequestValidationError(f"{key} is required.")
    if len(value) > max_length:
        raise RequestValidationError(f"{key} must be {max_length} characters or fewer.")
    return value


def _error(message, status):
    return JsonResponse({"error": message}, status=status)


def _provider_error_response(exc):
    return _error(str(exc), exc.status_code)


@require_POST
def analyze_assignment(request):
    try:
        payload = _read_json(request)
        assignment = _text(
            payload, "assignment", required=True, max_length=MAX_ASSIGNMENT_LENGTH
        )
        context = _text(payload, "context", max_length=MAX_CONTEXT_LENGTH)
        result = build_analysis(assignment, context)
    except RequestValidationError as exc:
        return _error(str(exc), 400)
    except ProviderError as exc:
        return _provider_error_response(exc)

    request.session["unstuck"] = {
        "assignment": assignment,
        "context": context,
        "learning_stage": "exploring",
        "hint_level": 0,
        "history": [],
    }
    return JsonResponse(result)


@require_POST
def search_resources(request):
    try:
        payload = _read_json(request)
        concepts = payload.get("concepts")
        if concepts is None:
            concepts = payload.get("queries")
        if not isinstance(concepts, list) or not 1 <= len(concepts) <= 5:
            raise RequestValidationError("concepts must be a list containing 1 to 5 concepts.")
        if any(
            not isinstance(concept, str) or not concept.strip() or len(concept) > 200
            for concept in concepts
        ):
            raise RequestValidationError("Each concept must be text of at most 200 characters.")
        feedback = payload.get("feedback")
        if feedback not in (None, "", "too_difficult", "not_helpful"):
            raise RequestValidationError("feedback must be too_difficult or not_helpful.")
        exclude_urls = payload.get("exclude_urls", [])
        if (
            not isinstance(exclude_urls, list)
            or len(exclude_urls) > 10
            or any(not isinstance(url, str) or len(url) > 2048 for url in exclude_urls)
        ):
            raise RequestValidationError("exclude_urls must contain at most 10 URL strings.")
        result = discover_resources(
            [concept.strip() for concept in concepts],
            feedback=feedback or None,
            exclude_urls=exclude_urls,
        )
    except RequestValidationError as exc:
        return _error(str(exc), 400)
    except ProviderError as exc:
        return _provider_error_response(exc)
    return JsonResponse(result)


@require_POST
def chat(request):
    try:
        payload = _read_json(request)
        assignment = _text(
            payload, "assignment", required=True, max_length=MAX_ASSIGNMENT_LENGTH
        )
        context = _text(payload, "context", max_length=MAX_CONTEXT_LENGTH)
        message = _text(payload, "student_message", required=True, max_length=MAX_MESSAGE_LENGTH)
        working = _text(payload, "current_working", max_length=MAX_WORKING_LENGTH)
        mode = payload.get("mode", "hint")
        if not isinstance(mode, str) or mode not in VALID_MODES:
            raise RequestValidationError("mode must be hint, explain, or check_work.")

        state = request.session.get("unstuck", {})
        if state.get("assignment") != assignment:
            state = {
                "assignment": assignment,
                "context": context,
                "learning_stage": "discussing",
                "hint_level": 0,
                "history": [],
            }
        else:
            context = context or state.get("context", "")
        previous_level = state.get("hint_level", 0)
        if not isinstance(previous_level, int) or not 0 <= previous_level <= 3:
            previous_level = 0
        hint_level = min(3, previous_level + 1) if mode == "hint" else previous_level
        history = state.get("history", [])
        if not isinstance(history, list):
            history = []
        history = history[-MAX_HISTORY_MESSAGES:]
        result = build_chat_response(
            assignment, context, message, working, mode, hint_level, history
        )
    except RequestValidationError as exc:
        return _error(str(exc), 400)
    except ProviderError as exc:
        return _provider_error_response(exc)

    state["assignment"] = assignment
    state["learning_stage"] = "practicing"
    state["hint_level"] = result["hint_level"]
    state["history"] = (
        history
        + [
            {"role": "student", "content": message[:MAX_MESSAGE_LENGTH]},
            {"role": "tutor", "content": result["guidance"][:1_200]},
        ]
    )[-MAX_HISTORY_MESSAGES:]
    request.session["unstuck"] = state
    return JsonResponse(result)


@require_POST
def check_work(request):
    try:
        payload = _read_json(request)
        assignment = _text(
            payload, "assignment", required=True, max_length=MAX_ASSIGNMENT_LENGTH
        )
        context = _text(payload, "context", max_length=MAX_CONTEXT_LENGTH)
        working = _text(payload, "current_working", required=True, max_length=MAX_WORKING_LENGTH)
        state = request.session.get("unstuck", {})
        if state.get("assignment") == assignment:
            context = context or state.get("context", "")
        result = evaluate_work(assignment, working, context)
    except RequestValidationError as exc:
        return _error(str(exc), 400)
    except ProviderError as exc:
        return _provider_error_response(exc)

    state = request.session.get("unstuck", {})
    if state.get("assignment") == assignment:
        state["learning_stage"] = "reviewing"
        state["history"] = (
            state.get("history", [])
            + [
                {"role": "student", "content": f"Submitted working: {working[:1_000]}"},
                {"role": "tutor", "content": result["feedback"][:1_200]},
            ]
        )[-MAX_HISTORY_MESSAGES:]
        request.session["unstuck"] = state
    return JsonResponse(result)
