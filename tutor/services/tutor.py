import json
import re

from .nebius import ProviderError, complete_json


MAX_HISTORY_MESSAGES = 8
_DISCLOSURE_PATTERNS = (
    re.compile(r"\b(?:the final answer|the answer is|the solution is)\b", re.IGNORECASE),
    re.compile(r"\bsolving (?:for|this equation) gives\b", re.IGNORECASE),
    re.compile(r"\btherefore\s+[a-z]\s*=", re.IGNORECASE),
    re.compile(r"(?<![A-Za-z])(?:[A-Za-z])\s*=\s*-?\d+(?:\.\d+)?\b"),
)


def _required_string(data, key, max_length=1200):
    value = data.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise ProviderError("The AI provider returned an incomplete response.", 502)
    return value.strip()


def _string_list(data, key, max_items=8):
    value = data.get(key)
    if not isinstance(value, list) or len(value) > max_items:
        raise ProviderError("The AI provider returned an incomplete response.", 502)
    if any(not isinstance(item, str) or not item.strip() or len(item) > 400 for item in value):
        raise ProviderError("The AI provider returned an incomplete response.", 502)
    return [item.strip() for item in value]


def _safe_response_text(*values):
    return "\n".join(values)


def contains_solution_disclosure(*values):
    text = _safe_response_text(*values)
    return any(pattern.search(text) for pattern in _DISCLOSURE_PATTERNS)


def _learning_prompt():
    return (
        "You are Unstuck, a patient mathematics learning coach. Help students reason, do not "
        "complete their homework. Never provide a full worked solution, final numeric result, or "
        "submission-ready answer. Give at most one meaningful next step, calibrated to the stated "
        "hint level. Respond to the student's actual attempt. Preserve LaTeX notation. If context "
        "is unclear, ask a clarifying question. Acknowledge uncertainty. Return only JSON matching "
        "the requested fields. The application has a separate answer-disclosure guard; do not "
        "try to bypass it."
    )


def analyze_assignment(assignment, context=""):
    data = complete_json(
        _learning_prompt()
        + " Identify what to learn, but do not solve the assignment.",
        json.dumps(
            {
                "task": "Analyze the assignment for learning needs.",
                "required_shape": {
                    "summary": "concise string",
                    "concepts": ["string"],
                    "prerequisites": ["string"],
                    "learning_path": ["ordered string steps"],
                },
                "assignment": assignment,
                "context": context,
            },
        ),
    )
    summary = _required_string(data, "summary", 500)
    concepts = _string_list(data, "concepts", 8)
    prerequisites = _string_list(data, "prerequisites", 8)
    learning_path = _string_list(data, "learning_path", 8)
    if not concepts or not learning_path:
        raise ProviderError("The AI provider returned an incomplete response.", 502)
    if contains_solution_disclosure(
        summary, *concepts, *prerequisites, *learning_path
    ):
        raise ProviderError(
            "The AI response went beyond learning guidance. Please retry the analysis.", 502
        )
    return {
        "summary": summary,
        "concepts": concepts,
        "prerequisites": prerequisites,
        "learning_path": learning_path,
    }


def tutor_chat(assignment, context, student_message, working, mode, hint_level, history):
    data = complete_json(
        _learning_prompt()
        + " For hint mode, use progressive hint levels 1 (orientation), 2 (strategy), and 3 "
        "(a specific next step without finishing). For explain mode, explain only the relevant "
        "concept. For check_work mode, respond to the submitted attempt. Never finish the problem.",
        json.dumps(
            {
                "task": "Guide the student on their current question.",
                "required_shape": {
                    "diagnosis": "string",
                    "guidance": "targeted hint or conceptual explanation",
                    "reasoning_question": "question prompting independent reasoning",
                    "hint_level": "integer from 0 to 3",
                    "next_action": "string",
                },
                "mode": mode,
                "hint_level": hint_level,
                "assignment": assignment,
                "assignment_context": context,
                "student_message": student_message,
                "current_working": working,
                "recent_conversation": history,
            },
        ),
    )
    diagnosis = _required_string(data, "diagnosis")
    guidance = _required_string(data, "guidance")
    reasoning_question = _required_string(data, "reasoning_question", 500)
    next_action = _required_string(data, "next_action", 500)
    returned_level = data.get("hint_level")
    if not isinstance(returned_level, int) or isinstance(returned_level, bool) or not 0 <= returned_level <= 3:
        raise ProviderError("The AI provider returned an invalid hint level.", 502)
    if mode == "hint":
        returned_level = hint_level

    if contains_solution_disclosure(diagnosis, guidance, reasoning_question, next_action):
        guidance = (
            "I may have jumped too far ahead. Let's work through just one step together: "
            "what information in the problem seems most useful to you?"
        )
        reasoning_question = "What is the first relationship or rule you think applies here?"
        next_action = "Share one step of your reasoning, and we will build from it."

    return {
        "diagnosis": diagnosis,
        "guidance": guidance,
        "reasoning_question": reasoning_question,
        "hint_level": returned_level,
        "next_action": next_action,
    }


def check_work(assignment, working, context=""):
    data = complete_json(
        _learning_prompt()
        + " Evaluate reasoning cautiously. Do not solve missing steps or give a final answer. "
        "Flag likely issues without asserting certainty unless directly supported by the work.",
        json.dumps(
            {
                "task": "Give feedback on the student's work.",
                "required_shape": {
                    "diagnosis": "string",
                    "feedback": "string",
                    "likely_errors": ["string"],
                    "missing_justifications": ["string"],
                    "next_step": "string",
                    "uncertainty": "string stating what cannot be verified",
                },
                "assignment": assignment,
                "assignment_context": context,
                "student_work": working,
            },
        ),
    )
    result = {
        "diagnosis": _required_string(data, "diagnosis"),
        "feedback": _required_string(data, "feedback"),
        "likely_errors": _string_list(data, "likely_errors"),
        "missing_justifications": _string_list(data, "missing_justifications"),
        "next_step": _required_string(data, "next_step", 500),
        "uncertainty": _required_string(data, "uncertainty", 500),
    }
    if contains_solution_disclosure(
        result["diagnosis"],
        result["feedback"],
        result["next_step"],
        result["uncertainty"],
        *result["likely_errors"],
        *result["missing_justifications"],
    ):
        result["feedback"] = (
            "I may have gone beyond feedback into solving. Let's focus on your reasoning: "
            "check the operation you used in the last step and explain why it preserves equality."
        )
        result["next_step"] = "Explain why your most recent transformation is valid."
    return result
