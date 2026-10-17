import json
import re
import uuid

from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from .services.nebius import ProviderError
from .services.pdf_extraction import (
    PdfExtractionError,
    analyze_pdf,
    max_page_count,
    max_upload_bytes,
)
from .services.resources import search_resources as discover_resources
from .services.tutor import (
    MAX_HISTORY_MESSAGES,
    analyze_assignment as build_analysis,
    check_work as evaluate_work,
    tutor_chat as build_chat_response,
    validate_learning_step as evaluate_learning_step,
)


MAX_BODY_BYTES = 24_000
MAX_ASSIGNMENT_LENGTH = 6_000
MAX_CONTEXT_LENGTH = 3_000
MAX_MESSAGE_LENGTH = 3_000
MAX_WORKING_LENGTH = 5_000
MAX_STEP_ATTEMPTS = 10
VALID_MODES = {"hint", "explain", "check_work"}


class RequestValidationError(Exception):
    pass


@require_GET
def index(request):
    upload_limit = max_upload_bytes()
    return render(
        request,
        "tutor/index.html",
        {
            "pdf_max_upload_bytes": upload_limit,
            "pdf_max_upload_mb": (upload_limit + 1024 * 1024 - 1) // (1024 * 1024),
            "pdf_max_pages": max_page_count(),
        },
    )


@require_POST
def reset_learning_session(request):
    request.session.pop("learning_session", None)
    request.session.pop("unstuck", None)
    request.session.pop("pdf_extraction", None)
    return redirect("index")


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


def _learning_state_for_client(state):
    steps = state.get("steps", [])
    summaries = [
        {"id": step["id"], "title": step["title"], "status": step["status"]}
        for step in steps
    ]
    selected_id = state.get("active_step_id")
    selected = next((step for step in steps if step["id"] == selected_id), None)
    if selected is None and steps:
        selected = next((step for step in steps if step["status"] != "completed"), steps[-1])
    active = None
    if selected:
        active = {
            key: value
            for key, value in selected.items()
            if key != "hints"
        }
        active["attempts"] = selected.get("attempts", [])[-MAX_STEP_ATTEMPTS:]
        active["is_actionable"] = selected["status"] != "locked"
    completed_count = sum(step["status"] == "completed" for step in steps)
    return {
        "assignment": state.get("assignment", ""),
        "summary": state.get("summary", ""),
        "source_question": state.get("source_question"),
        "concepts": state.get("concepts", []),
        "prerequisites": state.get("prerequisites", []),
        "learning_path": state.get("learning_path", []),
        "steps": summaries,
        "active_step": active,
        "progress": {"completed": completed_count, "total": len(steps)},
    }


@require_GET
def learning_session(request):
    state = request.session.get("learning_session")
    if not isinstance(state, dict):
        return JsonResponse({"available": False, "session": None})
    selected_id = request.GET.get("step_id")
    if selected_id:
        step = next(
            (item for item in state.get("steps", []) if item.get("id") == selected_id),
            None,
        )
        if step is None:
            return _error("This learning step was not found.", 404)
        if step.get("status") == "locked":
            return _error("Complete the current step before opening this one.", 403)
        if step.get("status") == "available":
            step["status"] = "in_progress"
            request.session["learning_session"] = state
        session_data = _learning_state_for_client(
            {**state, "active_step_id": selected_id}
        )
        return JsonResponse({"available": True, "session": session_data})
    session_data = _learning_state_for_client(state)
    return JsonResponse({"available": True, "session": session_data})


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

    _start_learning_session(request, assignment, context, result)
    request.session.pop("pdf_extraction", None)
    payload = _learning_state_for_client(request.session["learning_session"])
    payload["assignment"] = assignment
    return JsonResponse(payload)


def _start_learning_session(request, assignment, context, result, source_question=None):
    state = {
        "assignment": assignment,
        "context": context,
        "source_question": source_question,
        "learning_stage": "exploring",
        "hint_level": 0,
        "history": [],
        "summary": result["summary"],
        "concepts": result["concepts"],
        "prerequisites": result["prerequisites"],
        "learning_path": result["learning_path"],
        "steps": result["steps"],
        "active_step_id": result["steps"][0]["id"],
    }
    request.session["learning_session"] = state
    request.session["unstuck"] = {
        "assignment": assignment,
        "context": context,
        "learning_stage": "exploring",
        "hint_level": 0,
        "history": [],
    }


@require_POST
def extract_assignment_pdf(request):
    uploaded = request.FILES.get("file")
    if uploaded is None:
        return _error("Choose a PDF file to upload.", 400)
    if not uploaded.name.lower().endswith(".pdf"):
        return _error("Upload a file with a .pdf extension.", 400)
    try:
        limit = max_upload_bytes()
        limit_mb = (limit + 1024 * 1024 - 1) // (1024 * 1024)
        if uploaded.size > limit:
            raise PdfExtractionError(
                f"The PDF exceeds the {limit_mb} MB upload limit.", 413
            )
        data = uploaded.read(limit + 1)
        if len(data) > limit:
            raise PdfExtractionError(
                f"The PDF exceeds the {limit_mb} MB upload limit.", 413
            )
        result = analyze_pdf(data)
    except PdfExtractionError as exc:
        response = {"error": str(exc)}
        if exc.extracted_text:
            response.update(
                {
                    "page_count": exc.page_count,
                    "extracted_text": exc.extracted_text,
                }
            )
        return JsonResponse(response, status=exc.status_code)

    document_id = str(uuid.uuid4())
    request.session["pdf_extraction"] = {
        "document_id": document_id,
        "page_count": result["page_count"],
        "extracted_text": result["extracted_text"],
        "questions": result["questions"],
    }
    return JsonResponse(
        {
            "document_id": document_id,
            "page_count": result["page_count"],
            "extracted_text": result["extracted_text"],
            "questions": result["questions"],
        }
    )


@require_POST
def start_pdf_question(request):
    try:
        payload = _read_json(request)
        document_id = _text(payload, "document_id", required=True, max_length=36)
        question_id = _text(payload, "question_id", required=True, max_length=8)
        assignment = _text(
            payload, "question_text", required=True, max_length=MAX_ASSIGNMENT_LENGTH
        )
        if not re.fullmatch(r"[0-9a-f-]{36}", document_id):
            raise RequestValidationError("The extracted document is no longer available.")
        if not re.fullmatch(r"q[1-9][0-9]{0,2}", question_id):
            raise RequestValidationError("Choose a valid extracted question.")
        extraction = request.session.get("pdf_extraction")
        if (
            not isinstance(extraction, dict)
            or extraction.get("document_id") != document_id
        ):
            return _error("This extracted document has expired. Upload it again.", 409)
        question = next(
            (item for item in extraction.get("questions", []) if item.get("id") == question_id),
            None,
        )
        if question is None:
            return _error("This question was not found in the extracted document.", 404)
        result = build_analysis(assignment, "")
    except RequestValidationError as exc:
        return _error(str(exc), 400)
    except ProviderError as exc:
        return _provider_error_response(exc)

    source_question = {
        "label": question["label"],
        "page_start": question["page_start"],
        "page_end": question["page_end"],
        "text": assignment,
    }
    _start_learning_session(request, assignment, "", result, source_question)
    request.session.pop("pdf_extraction", None)
    response = _learning_state_for_client(request.session["learning_session"])
    response["assignment"] = assignment
    return JsonResponse(response)


@require_POST
def validate_step(request):
    try:
        payload = _read_json(request)
        assignment = _text(
            payload, "assignment", required=True, max_length=MAX_ASSIGNMENT_LENGTH
        )
        step_id = _text(payload, "step_id", required=True, max_length=80)
        attempt = _text(payload, "attempt", required=True, max_length=MAX_WORKING_LENGTH)
    except RequestValidationError as exc:
        return _error(str(exc), 400)

    state = request.session.get("learning_session")
    if not isinstance(state, dict) or state.get("assignment") != assignment:
        return _error("Start or restore this assignment before submitting a step.", 409)
    steps = state.get("steps", [])
    index = next((i for i, item in enumerate(steps) if item.get("id") == step_id), None)
    if index is None:
        return _error("This learning step was not found.", 404)
    step = steps[index]
    if step.get("status") == "locked":
        return _error("Complete the current step before opening this one.", 403)
    if step.get("status") not in {"available", "in_progress", "needs_revision", "completed"}:
        return _error("This learning step has an invalid status.", 409)

    try:
        result = evaluate_learning_step(assignment, step, attempt)
    except ProviderError as exc:
        return _provider_error_response(exc)

    was_completed = step["status"] == "completed"
    step["status"] = "completed" if was_completed or result["step_completed"] else "needs_revision"
    step["attempts"] = (
        step.get("attempts", [])
        + [
            {
                "attempt": attempt,
                "status": result["status"],
                "feedback": result["feedback"],
                "correct_parts": result["correct_parts"],
                "issues": result["issues"],
            }
        ]
    )[-MAX_STEP_ATTEMPTS:]
    if not was_completed and step["status"] == "completed":
        if index + 1 < len(steps):
            if steps[index + 1]["status"] == "locked":
                steps[index + 1]["status"] = "available"
            state["active_step_id"] = steps[index + 1]["id"]
        else:
            state["active_step_id"] = step["id"]
    else:
        if not was_completed:
            step["status"] = "needs_revision"
            state["active_step_id"] = step["id"]
    request.session["learning_session"] = state
    result["progress_status"] = step["status"]
    result["session"] = _learning_state_for_client(state)
    return JsonResponse(result)


@require_POST
def request_step_hint(request):
    try:
        payload = _read_json(request)
        assignment = _text(
            payload, "assignment", required=True, max_length=MAX_ASSIGNMENT_LENGTH
        )
        step_id = _text(payload, "step_id", required=True, max_length=80)
    except RequestValidationError as exc:
        return _error(str(exc), 400)

    state = request.session.get("learning_session")
    if not isinstance(state, dict) or state.get("assignment") != assignment:
        return _error("Start or restore this assignment before requesting a hint.", 409)
    step = next((item for item in state.get("steps", []) if item.get("id") == step_id), None)
    if step is None:
        return _error("This learning step was not found.", 404)
    if step.get("status") == "locked":
        return _error("Complete the current step before requesting a hint for this one.", 403)
    level = step.get("hint_level", 0)
    hints = step.get("hints", [])
    if not isinstance(level, int) or not isinstance(hints, list) or level >= len(hints):
        return _error("You have used the available hints for this step.", 409)
    level += 1
    step["hint_level"] = level
    if step.get("status") == "available":
        step["status"] = "in_progress"
    request.session["learning_session"] = state
    return JsonResponse({"hint_level": level, "hint": hints[level - 1], "status": step["status"]})


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
