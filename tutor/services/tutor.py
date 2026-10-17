import json
import hashlib
import re

from .nebius import ProviderError, complete_json


MAX_HISTORY_MESSAGES = 8
MAX_LEARNING_STEPS = 6
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
        "You are Lazyn't, a patient mathematics learning coach. Help students reason, do not "
        "complete their homework. Never provide a full worked solution, final numeric result, or "
        "submission-ready answer. Give at most one meaningful next step, calibrated to the stated "
        "hint level. Respond to the student's actual attempt. Write inline mathematics using "
        "LaTeX delimiters such as \\(\\|B\\|_1\\) and use \\(\\infty\\) for infinity subscripts; "
        "do not write mathematical subscripts as plain-text underscores. If context "
        "is unclear, ask a clarifying question. Acknowledge uncertainty. Return only JSON matching "
        "the requested fields. The application has a separate answer-disclosure guard; do not "
        "try to bypass it."
    )


def analyze_assignment(assignment, context="", _retry_disclosure=False):
    retry_instruction = (
        " Your previous draft was rejected because it contained answer-like text. "
        "Regenerate the analysis with learning guidance only: do not state any numeric "
        "answer or include a variable set equal to a number."
        if _retry_disclosure
        else ""
    )
    data = complete_json(
        _learning_prompt()
        + " Identify what to learn, but do not solve the assignment. Create a short, ordered "
        "sequence of one to six meaningful checkpoints; put prerequisite understanding before "
        "advanced reasoning and do not split every arithmetic operation into its own step. "
        "Each practice prompt must test the step objective, not request the assignment's final answer."
        + retry_instruction,
        json.dumps(
            {
                "task": "Analyze the assignment for learning needs.",
                "required_shape": {
                    "summary": "concise string",
                    "concepts": ["string"],
                    "prerequisites": ["string"],
                    "learning_path": ["ordered string steps"],
                    "steps": [
                        {
                            "title": "short student-facing title",
                            "objective": "one specific learning objective",
                            "instructions": "brief task instructions without solving the assignment",
                            "prerequisite_concepts": ["concept names"],
                            "practice_prompt": "a focused practice checkpoint",
                            "validation_criteria": "observable reasoning needed to demonstrate this objective",
                            "hints": [
                                "conceptual reminder",
                                "relevant definition or property",
                                "guiding question about the next move",
                                "specific partial hint without giving the answer",
                            ],
                        }
                    ],
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
    raw_steps = data.get("steps")
    if (
        not concepts
        or not learning_path
        or not isinstance(raw_steps, list)
        or not 1 <= len(raw_steps) <= MAX_LEARNING_STEPS
    ):
        raise ProviderError("The AI provider returned an incomplete response.", 502)
    steps = []
    used_ids = set()
    all_step_text = [summary, *concepts, *prerequisites, *learning_path]
    for index, raw_step in enumerate(raw_steps):
        if not isinstance(raw_step, dict):
            raise ProviderError("The AI provider returned an invalid learning step.", 502)
        title = _required_string(raw_step, "title", 120)
        objective = _required_string(raw_step, "objective", 400)
        instructions = _required_string(raw_step, "instructions", 800)
        practice_prompt = _required_string(raw_step, "practice_prompt", 500)
        validation_criteria = _required_string(raw_step, "validation_criteria", 800)
        step_concepts = _string_list(raw_step, "prerequisite_concepts", 5)
        hints = _string_list(raw_step, "hints", 4)
        if len(hints) != 4:
            raise ProviderError("The AI provider returned incomplete step hints.", 502)
        identifier = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:36] or "learning-step"
        identifier = f"{index + 1}-{identifier}"
        if identifier in used_ids:
            suffix = hashlib.sha1(f"{index}:{title}".encode("utf-8")).hexdigest()[:6]
            identifier = f"{identifier}-{suffix}"
        used_ids.add(identifier)
        step_text = [
            title,
            objective,
            instructions,
            practice_prompt,
            validation_criteria,
            *step_concepts,
            *hints,
        ]
        all_step_text.extend(step_text)
        steps.append(
            {
                "id": identifier,
                "title": title,
                "objective": objective,
                "instructions": instructions,
                "prerequisite_concepts": step_concepts,
                "practice_prompt": practice_prompt,
                "validation_criteria": validation_criteria,
                "hints": hints,
                "status": "available" if index == 0 else "locked",
                "attempts": [],
                "hint_level": 0,
            }
        )
    if contains_solution_disclosure(*all_step_text):
        if not _retry_disclosure:
            return analyze_assignment(assignment, context, _retry_disclosure=True)
        raise ProviderError(
            "The AI response went beyond learning guidance. Please retry the analysis.", 502
        )
    return {
        "summary": summary,
        "concepts": concepts,
        "prerequisites": prerequisites,
        "learning_path": learning_path,
        "steps": steps,
    }


def validate_learning_step(assignment, step, attempt):
    data = complete_json(
        _learning_prompt()
        + " Assess only the current step and criteria. Do not solve the overall assignment. "
        "Be conservative: mark completed only when the attempt explicitly demonstrates every "
        "criterion. Otherwise return needs_revision. Do not use a completion flag as a substitute "
        "for evidence in correct_parts. Return exactly the requested JSON fields.",
        json.dumps(
            {
                "task": "Evaluate the student's current learning checkpoint.",
                "required_shape": {
                    "status": "completed or needs_revision",
                    "correct_parts": ["specific reasoning evidenced by the attempt"],
                    "issues": ["specific gap, error, or missing justification"],
                    "feedback": "brief targeted explanation",
                    "next_prompt": "one actionable next step or an invitation to continue",
                    "hint_available": "boolean",
                    "step_completed": "boolean",
                },
                "assignment": assignment,
                "current_step": {
                    "title": step["title"],
                    "objective": step["objective"],
                    "instructions": step["instructions"],
                    "practice_prompt": step["practice_prompt"],
                    "validation_criteria": step["validation_criteria"],
                },
                "student_attempt": attempt,
            },
        ),
    )
    status = data.get("status")
    if status not in {"completed", "needs_revision"}:
        raise ProviderError("The AI provider returned an invalid validation status.", 502)
    correct_parts = _string_list(data, "correct_parts", 5)
    issues = _string_list(data, "issues", 5)
    feedback = _required_string(data, "feedback", 1000)
    next_prompt = _required_string(data, "next_prompt", 500)
    hint_available = data.get("hint_available")
    step_completed = data.get("step_completed")
    if not isinstance(hint_available, bool) or not isinstance(step_completed, bool):
        raise ProviderError("The AI provider returned invalid validation flags.", 502)

    complete = (
        status == "completed"
        and step_completed
        and bool(correct_parts)
        and not issues
    )
    if contains_solution_disclosure(feedback, next_prompt, *correct_parts, *issues):
        complete = False
        feedback = (
            "I should keep the focus on your reasoning rather than reveal an answer. "
            "Show how your attempt meets the stated learning objective."
        )
        next_prompt = "Which part of the step's validation criteria can you justify next?"
    if not complete and status == "completed":
        status = "needs_revision"
        step_completed = False
        if not issues:
            issues = ["The evidence does not yet demonstrate all completion criteria."]

    return {
        "status": "completed" if complete else "needs_revision",
        "correct_parts": correct_parts,
        "issues": issues,
        "feedback": feedback,
        "next_prompt": next_prompt,
        "hint_available": hint_available,
        "step_completed": complete,
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
