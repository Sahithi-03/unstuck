import json
import os
from types import SimpleNamespace
from unittest.mock import patch

from django.test import Client, TestCase
from openai import APIConnectionError

from .services.nebius import ProviderError, complete_json
from .services import resources as resource_service
from .services.tutor import contains_solution_disclosure, validate_learning_step


def generated_step(title="Review the key concept", status="available"):
    return {
        "id": title.lower().replace(" ", "-"),
        "title": title,
        "objective": "Explain the relevant mathematical property.",
        "instructions": "Review the stated idea, then show how it applies.",
        "prerequisite_concepts": ["linear equations"],
        "practice_prompt": "Explain why this step is valid.",
        "validation_criteria": "Your reasoning identifies the property and justifies its use.",
        "hints": [
            "Recall the main idea.",
            "Review the definition of the property.",
            "Which property applies to this step?",
            "Try stating how the property justifies the transformation.",
        ],
        "status": status,
        "attempts": [],
        "hint_level": 0,
    }


def analysis_response():
    return {
        "summary": "This problem uses inverse operations.",
        "concepts": ["linear equations"],
        "prerequisites": ["integer arithmetic"],
        "learning_path": ["Review equality", "Practice inverse operations"],
        "steps": [
            generated_step("Review equality"),
            generated_step("Practice inverse operations", "locked"),
        ],
    }


def completed_validation():
    return {
        "status": "completed",
        "correct_parts": ["Explains why applying the same operation preserves equality."],
        "issues": [],
        "feedback": "Your justification meets this checkpoint's objective.",
        "next_prompt": "Continue to the next learning step.",
        "hint_available": True,
        "step_completed": True,
    }


class UnstuckViewsTests(TestCase):
    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.client.get("/")
        self.csrf_token = self.client.cookies["csrftoken"].value
        self.headers = {"HTTP_X_CSRFTOKEN": self.csrf_token}

    def post(self, url, payload):
        return self.client.post(
            url,
            data=json.dumps(payload),
            content_type="application/json",
            **self.headers,
        )

    def seed_learning_session(self):
        first = generated_step()
        second = generated_step("Use the property", "locked")
        session = self.client.session
        session["learning_session"] = {
            "assignment": "Prove the inequality",
            "context": "",
            "summary": "Review a property.",
            "concepts": ["norms"],
            "prerequisites": ["linear algebra"],
            "learning_path": [first["title"], second["title"]],
            "steps": [first, second],
            "active_step_id": first["id"],
        }
        session.save()
        self.client.cookies["sessionid"] = session.session_key
        return first, second

    def test_homepage_renders_interface_and_csrf_token(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Let's get you")
        self.assertContains(response, "math-field")
        self.assertContains(response, "csrfmiddlewaretoken")

    def test_analysis_rejects_empty_assignment(self):
        response = self.post("/api/assignments/analyze/", {"assignment": "  "})
        self.assertEqual(response.status_code, 400)
        self.assertIn("assignment", response.json()["error"])

    @patch(
        "tutor.services.tutor.complete_json",
        return_value={
            "summary": "Review equality and inverse operations.",
            "concepts": ["linear equations", "inverse operations"],
            "prerequisites": ["integer arithmetic"],
            "learning_path": ["Review equality", "Practice inverse operations"],
            "steps": [
                {
                    "title": "Review equality",
                    "objective": "Explain what keeps an equation balanced.",
                    "instructions": "Describe why the same operation must be applied to each side.",
                    "prerequisite_concepts": ["integer arithmetic"],
                    "practice_prompt": "Explain why subtracting the same value from each side preserves equality.",
                    "validation_criteria": "State the balance property and apply it to both sides.",
                    "hints": [
                        "Think of equality as a balance.",
                        "Recall what happens when both sides change equally.",
                        "Which operation must happen to both sides?",
                        "Name the balance property and apply it to each side.",
                    ],
                },
                {
                    "title": "Practice inverse operations",
                    "objective": "Identify an inverse operation.",
                    "instructions": "Explain how inverse operations help isolate a variable.",
                    "prerequisite_concepts": ["linear equations"],
                    "practice_prompt": "Describe an inverse operation that could be useful.",
                    "validation_criteria": "Identify the inverse relationship without solving the assignment.",
                    "hints": [
                        "Think about operations that undo one another.",
                        "Addition and subtraction are inverse operations.",
                        "What operation would undo the operation currently applied?",
                        "Name the inverse pair and describe how it helps.",
                    ],
                },
            ],
        },
    )
    def test_valid_analysis_returns_learning_path_and_saves_assignment(self, _complete):
        response = self.post(
            "/api/assignments/analyze/",
            {"assignment": "Solve x + 2 = 5", "context": "I am new to equations."},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["steps"][0]["status"], "available")
        self.assertEqual(response.json()["steps"][1]["status"], "locked")
        self.assertNotIn("hints", response.json()["active_step"])
        self.assertNotIn("instructions", response.json()["steps"][1])
        self.assertEqual(self.client.session["unstuck"]["context"], "I am new to equations.")
        self.assertEqual(self.client.session["unstuck"]["learning_stage"], "exploring")

    @patch(
        "tutor.services.tutor.complete_json",
        side_effect=ProviderError("The AI provider returned malformed JSON.", 502),
    )
    def test_analysis_reports_malformed_ai_response(self, _complete):
        response = self.post("/api/assignments/analyze/", {"assignment": "Solve x + 2 = 5"})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json(), {"error": "The AI provider returned malformed JSON."})

    @patch(
        "tutor.services.tutor.complete_json",
        return_value={
            "summary": "The answer is x = 3.",
            "concepts": ["equations"],
            "prerequisites": ["arithmetic"],
            "learning_path": ["Review inverse operations"],
            "steps": [
                {
                    "title": "Review inverse operations",
                    "objective": "Use inverse operations",
                    "instructions": "Review the property.",
                    "prerequisite_concepts": ["arithmetic"],
                    "practice_prompt": "Explain the operation.",
                    "validation_criteria": "State the property.",
                    "hints": ["Remember inverses.", "Use the definition.", "Which operation?", "Explain the next move."],
                }
            ],
            "search_queries": ["inverse operations"],
        },
    )
    def test_analysis_guard_rejects_solution_disclosure(self, _complete):
        response = self.post("/api/assignments/analyze/", {"assignment": "Solve x + 2 = 5"})
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("x = 3", response.content.decode())

    @patch("tutor.services.tutor.complete_json", side_effect=ProviderError("The AI provider could not complete the request. Please try again."))
    def test_provider_failure_is_safe_and_does_not_expose_traceback(self, _complete):
        response = self.post("/api/assignments/analyze/", {"assignment": "Find the area"})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("Traceback", response.content.decode())

    @patch(
        "tutor.services.tutor.complete_json",
        return_value={
            "diagnosis": "You have identified the relevant equation.",
            "guidance": "Try isolating the variable by undoing the addition first.",
            "reasoning_question": "What operation would undo adding 2?",
            "hint_level": 1,
            "next_action": "Write that operation on both sides.",
        },
    )
    def test_valid_tutoring_response_advances_session(self, _complete):
        response = self.post(
            "/api/tutor/chat/",
            {
                "assignment": "Solve x + 2 = 5",
                "student_message": "I don't know what to do next.",
                "current_working": "x + 2 = 5",
                "mode": "hint",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["hint_level"], 1)
        self.assertEqual(len(self.client.session["unstuck"]["history"]), 2)
        self.assertEqual(self.client.session["unstuck"]["learning_stage"], "practicing")

    def test_chat_rejects_disallowed_mode(self):
        response = self.post(
            "/api/tutor/chat/",
            {"assignment": "A problem", "student_message": "Help", "mode": "solve_it"},
        )
        self.assertEqual(response.status_code, 400)

    @patch.dict("os.environ", {"RESOURCE_SEARCH_PROVIDER": "none"})
    def test_unconfigured_resources_are_clearly_reported(self):
        response = self.post(
            "/api/resources/search/",
            {"queries": ["linear equations"], "concept": "inverse operations"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["available"])
        self.assertEqual(response.json()["resources"], [])
        self.assertIn("not configured", response.json()["message"])

    @patch("tutor.services.resources._tavily_search", return_value=[])
    def test_resource_endpoint_returns_empty_search_without_fabricating_cards(self, search):
        with patch.dict(os.environ, {"RESOURCE_SEARCH_PROVIDER": "tavily", "TAVILY_API_KEY": "test"}):
            response = self.post(
                "/api/resources/search/",
                {"concepts": ["quadratic equations"]},
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["available"])
        self.assertEqual(response.json()["resources"], [])
        self.assertIn("No matching resources", response.json()["message"])
        search.assert_called_once()

    def test_resource_endpoint_validates_feedback(self):
        response = self.post(
            "/api/resources/search/",
            {"concepts": ["fractions"], "feedback": "show_solution"},
        )
        self.assertEqual(response.status_code, 400)

    def test_learning_session_returns_only_active_step_details(self):
        self.seed_learning_session()
        response = self.client.get("/api/learning/session/")
        data = response.json()["session"]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["active_step"]["status"], "available")
        self.assertNotIn("hints", data["active_step"])
        self.assertNotIn("instructions", data["steps"][1])

    def test_opening_available_step_marks_it_in_progress(self):
        first, _ = self.seed_learning_session()
        response = self.client.get(
            f"/api/learning/session/?step_id={first['id']}"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["session"]["active_step"]["status"], "in_progress")
        self.assertEqual(
            self.client.session["learning_session"]["steps"][0]["status"],
            "in_progress",
        )

    @patch("tutor.views.evaluate_learning_step")
    def test_locked_step_cannot_be_validated_or_unlocked_by_forged_request(self, evaluate):
        first, second = self.seed_learning_session()
        response = self.post(
            "/api/learning/validate/",
            {
                "assignment": "Prove the inequality",
                "step_id": second["id"],
                "attempt": "I completed this.",
                "step_completed": True,
            },
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            self.client.session["learning_session"]["steps"][1]["status"], "locked"
        )
        evaluate.assert_not_called()

    @patch(
        "tutor.views.evaluate_learning_step",
        return_value={
            "status": "needs_revision",
            "correct_parts": ["Names the relevant inequality."],
            "issues": ["Does not yet justify the direction."],
            "feedback": "State why the inequality applies.",
            "next_prompt": "Which property gives the needed bound?",
            "hint_available": True,
            "step_completed": False,
        },
    )
    def test_incorrect_attempt_is_saved_but_does_not_unlock_next(self, _evaluate):
        first, second = self.seed_learning_session()
        response = self.post(
            "/api/learning/validate/",
            {
                "assignment": "Prove the inequality",
                "step_id": first["id"],
                "attempt": "A partial explanation.",
            },
        )
        state = self.client.session["learning_session"]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(state["steps"][0]["status"], "needs_revision")
        self.assertEqual(state["steps"][1]["status"], "locked")
        self.assertEqual(len(state["steps"][0]["attempts"]), 1)

    @patch(
        "tutor.views.evaluate_learning_step",
        side_effect=[
            {
                "status": "needs_revision",
                "correct_parts": ["Names the concept."],
                "issues": ["Needs a justification."],
                "feedback": "Connect the concept to the step.",
                "next_prompt": "Which property supports this?",
                "hint_available": True,
                "step_completed": False,
            },
            completed_validation(),
        ],
    )
    def test_student_can_retry_and_complete_after_revision(self, _evaluate):
        first, second = self.seed_learning_session()
        path = "/api/learning/validate/"
        incomplete = self.post(
            path,
            {
                "assignment": "Prove the inequality",
                "step_id": first["id"],
                "attempt": "I identify the property.",
            },
        )
        completed = self.post(
            path,
            {
                "assignment": "Prove the inequality",
                "step_id": first["id"],
                "attempt": "I identify and justify the property.",
            },
        )
        state = self.client.session["learning_session"]
        self.assertEqual(incomplete.json()["status"], "needs_revision")
        self.assertEqual(completed.json()["status"], "completed")
        self.assertEqual(len(state["steps"][0]["attempts"]), 2)
        self.assertEqual(state["steps"][1]["status"], "available")

    @patch("tutor.views.evaluate_learning_step", return_value=completed_validation())
    def test_successful_validation_unlocks_next_and_persists_history(self, _evaluate):
        first, second = self.seed_learning_session()
        response = self.post(
            "/api/learning/validate/",
            {
                "assignment": "Prove the inequality",
                "step_id": first["id"],
                "attempt": "The property holds by applying it to both sides.",
            },
        )
        state = self.client.session["learning_session"]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(state["steps"][0]["status"], "completed")
        self.assertEqual(state["steps"][1]["status"], "available")
        self.assertEqual(response.json()["session"]["progress"]["completed"], 1)
        self.assertEqual(state["steps"][0]["attempts"][0]["status"], "completed")

    @patch("tutor.views.evaluate_learning_step", return_value=completed_validation())
    def test_completed_step_can_be_reopened_without_moving_progress_back(self, _evaluate):
        first, second = self.seed_learning_session()
        session = self.client.session["learning_session"]
        session["steps"][0]["status"] = "completed"
        session["steps"][1]["status"] = "available"
        session["active_step_id"] = second["id"]
        self.client.session["learning_session"] = session
        self.client.session.save()

        reopened = self.client.get(f"/api/learning/session/?step_id={first['id']}").json()["session"]
        self.assertEqual(reopened["active_step"]["id"], first["id"])
        response = self.post(
            "/api/learning/validate/",
            {
                "assignment": "Prove the inequality",
                "step_id": first["id"],
                "attempt": "Reviewing my previous reasoning.",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.session["learning_session"]["active_step_id"], second["id"])

    @patch("tutor.views.evaluate_learning_step", return_value=completed_validation())
    def test_completing_last_step_enters_complete_progress_state(self, _evaluate):
        final = generated_step("Final checkpoint", "in_progress")
        session = self.client.session
        session["learning_session"] = {
            "assignment": "Prove the inequality",
            "context": "",
            "summary": "Review a property.",
            "concepts": ["norms"],
            "prerequisites": ["linear algebra"],
            "learning_path": [final["title"]],
            "steps": [final],
            "active_step_id": final["id"],
        }
        session.save()
        self.client.cookies["sessionid"] = session.session_key
        response = self.post(
            "/api/learning/validate/",
            {
                "assignment": "Prove the inequality",
                "step_id": final["id"],
                "attempt": "I justify the property.",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["progress_status"], "completed")
        self.assertEqual(response.json()["session"]["progress"], {"completed": 1, "total": 1})

    def test_hints_are_revealed_one_at_a_time_and_stop_at_available_count(self):
        first, second = self.seed_learning_session()
        url = "/api/learning/hint/"
        payload = {"assignment": "Prove the inequality", "step_id": first["id"]}
        one = self.post(url, payload)
        two = self.post(url, payload)
        self.assertEqual(one.json()["hint_level"], 1)
        self.assertEqual(one.json()["hint"], first["hints"][0])
        self.assertEqual(two.json()["hint_level"], 2)
        self.assertNotIn("hints", one.json())
        self.assertEqual(self.client.session["learning_session"]["steps"][1]["status"], "locked")

    @patch("tutor.services.tutor.complete_json")
    def test_analysis_provider_failure_does_not_create_progress(self, complete):
        complete.side_effect = ProviderError("AI unavailable")
        response = self.post(
            "/api/assignments/analyze/",
            {"assignment": "Prove a matrix inequality"},
        )
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("learning_session", self.client.session)

    @patch(
        "tutor.services.tutor.complete_json",
        return_value={
            "diagnosis": "You made a good start.",
            "guidance": "The answer is x = 3. Now check it.",
            "reasoning_question": "Can you substitute your result?",
            "hint_level": 1,
            "next_action": "Try substituting.",
        },
    )
    def test_chat_guard_replaces_explicit_solution_disclosure(self, _complete):
        response = self.post(
            "/api/tutor/chat/",
            {"assignment": "Solve x + 2 = 5", "student_message": "Help", "mode": "hint"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("x = 3", response.json()["guidance"])
        self.assertTrue(contains_solution_disclosure("The answer is x = 3"))
        self.assertTrue(contains_solution_disclosure("x = 3"))

    @patch(
        "tutor.services.tutor.complete_json",
        return_value={
            "diagnosis": "You have the right equation.",
            "guidance": "Try undoing the addition on both sides.",
            "reasoning_question": "What operation would undo adding 2?",
            "hint_level": 1,
            "next_action": "Write that operation on each side.",
        },
    )
    def test_hint_level_progresses_even_if_model_repeats_old_level(self, _complete):
        request = {
            "assignment": "Solve x + 2 = 5",
            "student_message": "What next?",
            "mode": "hint",
        }
        first = self.post("/api/tutor/chat/", request)
        second = self.post("/api/tutor/chat/", request)
        self.assertEqual(first.json()["hint_level"], 1)
        self.assertEqual(second.json()["hint_level"], 2)


    @patch(
        "tutor.services.tutor.complete_json",
        return_value={
            "diagnosis": "The submitted work has one step.",
            "feedback": "Your equality-preserving operation needs a reason.",
            "likely_errors": [],
            "missing_justifications": ["Explain why both sides can be changed in the same way."],
            "next_step": "Describe why your operation keeps the equation balanced.",
            "uncertainty": "Only one line was provided, so later steps cannot be checked.",
        },
    )
    def test_check_work_returns_cautious_feedback(self, _complete):
        response = self.post(
            "/api/tutor/check/",
            {"assignment": "Solve x + 2 = 5", "current_working": "x = 5 - 2"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("uncertainty", response.json())
        self.assertIn("missing_justifications", response.json())


class NebiusServiceTests(TestCase):
    @patch.dict("os.environ", {"NEBIUS_API_KEY": "test-key", "NEBIUS_MODEL": "test-model"})
    @patch("tutor.services.nebius.OpenAI")
    def test_malformed_json_from_sdk_becomes_provider_error(self, openai):
        openai.return_value.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="not JSON"))]
        )
        with self.assertRaisesRegex(ProviderError, "malformed JSON"):
            complete_json("system", "user")

    @patch.dict("os.environ", {"NEBIUS_API_KEY": "test-key", "NEBIUS_MODEL": "test-model"})
    @patch("tutor.services.nebius.OpenAI")
    def test_sdk_failure_becomes_safe_provider_error(self, openai):
        openai.return_value.chat.completions.create.side_effect = APIConnectionError(
            request=SimpleNamespace()
        )
        with self.assertRaisesRegex(ProviderError, "could not complete"):
            complete_json("system", "user")


class StepValidationServiceTests(TestCase):
    @patch(
        "tutor.services.tutor.complete_json",
        return_value={
            "status": "completed",
            "correct_parts": ["The attempt explains the equality property."],
            "issues": [],
            "feedback": "This justification meets the objective.",
            "next_prompt": "Continue to the next idea.",
            "hint_available": True,
            "step_completed": True,
        },
    )
    def test_step_attempt_returns_structured_completed_feedback(self, _complete):
        result = validate_learning_step(
            "A proof",
            generated_step(),
            "Applying the same operation to both sides preserves equality.",
        )
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["step_completed"])
        self.assertEqual(result["correct_parts"], ["The attempt explains the equality property."])

    @patch(
        "tutor.services.tutor.complete_json",
        return_value={
            "status": "completed",
            "correct_parts": [],
            "issues": ["The stated criterion was not addressed."],
            "feedback": "Add a justification.",
            "next_prompt": "Which property can you cite?",
            "hint_available": True,
            "step_completed": True,
        },
    )
    def test_completion_flag_without_evidence_is_conservatively_rejected(self, _complete):
        result = validate_learning_step("A proof", generated_step(), "I am done.")
        self.assertEqual(result["status"], "needs_revision")
        self.assertFalse(result["step_completed"])

    @patch(
        "tutor.services.tutor.complete_json",
        return_value={
            "status": "completed",
            "correct_parts": ["The equality property is identified."],
            "issues": ["The application is not yet justified."],
            "feedback": "Explain the application.",
            "next_prompt": "Why does the property apply?",
            "hint_available": True,
            "step_completed": True,
        },
    )
    def test_completed_flag_with_remaining_issues_is_rejected(self, _complete):
        result = validate_learning_step("A proof", generated_step(), "I use equality.")
        self.assertEqual(result["status"], "needs_revision")
        self.assertFalse(result["step_completed"])

    @patch(
        "tutor.services.tutor.complete_json",
        return_value={
            "status": "completed",
            "correct_parts": ["The criterion is demonstrated."],
            "issues": [],
            "feedback": "Good.",
            "next_prompt": "Continue.",
            "hint_available": "yes",
            "step_completed": True,
        },
    )
    def test_malformed_validation_flags_raise_provider_error(self, _complete):
        with self.assertRaisesRegex(ProviderError, "invalid validation flags"):
            validate_learning_step("A proof", generated_step(), "Reasoning.")


class ResourceSearchTests(TestCase):
    @patch("tutor.services.resources._tavily_search")
    def test_missing_provider_configuration_is_honest_and_skips_external_search(self, search):
        with patch.dict(os.environ, {"RESOURCE_SEARCH_PROVIDER": "none"}, clear=False):
            result = resource_service.search_resources(["linear equations"])
        self.assertFalse(result["available"])
        self.assertEqual(result["resources"], [])
        self.assertIn("not configured", result["message"])
        search.assert_not_called()

    @patch("tutor.services.resources._tavily_search")
    def test_missing_tavily_key_is_honest_and_skips_external_search(self, search):
        env = {"RESOURCE_SEARCH_PROVIDER": "tavily"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("TAVILY_API_KEY", None)
            result = resource_service.search_resources(["linear equations"])
        self.assertFalse(result["available"])
        self.assertIn("TAVILY_API_KEY is missing", result["message"])
        search.assert_not_called()

    @patch("tutor.services.resources._tavily_search", return_value=[])
    def test_empty_provider_results_return_empty_resources(self, _search):
        with patch.dict(os.environ, {"RESOURCE_SEARCH_PROVIDER": "tavily", "TAVILY_API_KEY": "test"}):
            result = resource_service.search_resources(["linear equations"])
        self.assertTrue(result["available"])
        self.assertEqual(result["resources"], [])
        self.assertIn("No matching resources", result["message"])

    @patch(
        "tutor.services.resources.check_accessibility",
        return_value={
            "link_status": "accessible",
            "link_check_detail": "Link check received HTTP 200; this does not assess educational or mathematical quality.",
        },
    )
    @patch("tutor.services.resources._tavily_search")
    def test_concept_queries_reject_invalid_urls_deduplicate_and_prefer_sources(
        self, search, _accessibility
    ):
        search.side_effect = [
            [
                {"title": "Bad URL", "url": "javascript:alert(1)"},
                {
                    "title": "OpenStax algebra",
                    "url": "https://openstax.org/books/algebra/",
                    "content": "An introduction to equations.",
                    "section": "Linear equations",
                },
            ],
            [
                {"title": "Duplicate", "url": "https://openstax.org/books/algebra"},
                {
                    "title": "Khan Academy fractions",
                    "url": "https://www.khanacademy.org/math/fractions",
                    "content": "Understand fraction operations.",
                },
            ],
        ]
        with patch.dict(
            os.environ,
            {
                "RESOURCE_SEARCH_PROVIDER": "tavily",
                "TAVILY_API_KEY": "test",
                "RESOURCE_PREFERRED_DOMAINS": "openstax.org,khanacademy.org",
            },
        ):
            result = resource_service.search_resources(
                ["linear equations", "fractions"],
                feedback="too_difficult",
            )

        self.assertEqual(len(result["resources"]), 2)
        self.assertEqual(result["resources"][0]["source"], "OpenStax")
        self.assertEqual(result["resources"][0]["concept"], "linear equations")
        self.assertEqual(result["resources"][0]["suggested_section"], "Linear equations")
        self.assertEqual(result["resources"][1]["source"], "Khan Academy")
        self.assertTrue(all(item["url"].startswith("https://") for item in result["resources"]))
        first_query, first_domains, _ = search.call_args_list[0].args
        second_query, second_domains, _ = search.call_args_list[1].args
        self.assertIn("linear equations", first_query)
        self.assertIn("beginner-friendly", first_query)
        self.assertIn("fractions", second_query)
        self.assertEqual(first_domains, ["openstax.org", "khanacademy.org"])
        self.assertEqual(second_domains, first_domains)

    @patch("tutor.services.resources._tavily_search", return_value=[{"title": "bad", "url": "https://:bad"}])
    def test_malformed_and_invalid_urls_are_rejected(self, _search):
        with patch.dict(os.environ, {"RESOURCE_SEARCH_PROVIDER": "tavily", "TAVILY_API_KEY": "test"}):
            result = resource_service.search_resources(["geometry"])
        self.assertTrue(result["available"])
        self.assertEqual(result["resources"], [])

    @patch(
        "tutor.services.resources._tavily_search",
        side_effect=ProviderError("Resource search is temporarily unavailable."),
    )
    def test_provider_failure_returns_unavailable_state(self, _search):
        with patch.dict(os.environ, {"RESOURCE_SEARCH_PROVIDER": "tavily", "TAVILY_API_KEY": "test"}):
            result = resource_service.search_resources(["geometry"])
        self.assertFalse(result["available"])
        self.assertEqual(result["resources"], [])
        self.assertIn("temporarily unavailable", result["message"])

    @patch(
        "tutor.services.resources.check_accessibility",
        return_value={
            "link_status": "not_checked",
            "link_check_detail": "Public host could not be confirmed.",
        },
    )
    @patch(
        "tutor.services.resources._tavily_search",
        return_value=[
            {
                "title": "University notes",
                "url": "https://math.example.edu/notes",
                "content": "Notes on derivatives.",
            }
        ],
    )
    def test_valid_resource_has_checkpoint_and_no_unearned_verification_claim(
        self, _search, _accessibility
    ):
        with patch.dict(os.environ, {"RESOURCE_SEARCH_PROVIDER": "tavily", "TAVILY_API_KEY": "test"}):
            result = resource_service.search_resources(["derivatives"])
        resource = result["resources"][0]
        self.assertEqual(
            set(resource),
            {
                "title",
                "url",
                "source",
                "concept",
                "relevance",
                "suggested_section",
                "learning_objective",
                "checkpoint_question",
                "link_status",
                "link_check_detail",
            },
        )
        self.assertIn("derivatives", resource["learning_objective"])
        self.assertIn("derivatives", resource["checkpoint_question"])
        self.assertEqual(resource["link_status"], "not_checked")
        self.assertNotIn("verified", resource)
        self.assertNotIn("verified", resource["link_check_detail"].lower())

    @patch("tutor.services.resources._tavily_search", return_value=[])
    def test_excluded_urls_are_not_returned_on_follow_up(self, search):
        with patch.dict(os.environ, {"RESOURCE_SEARCH_PROVIDER": "tavily", "TAVILY_API_KEY": "test"}):
            result = resource_service.search_resources(
                ["geometry"],
                feedback="not_helpful",
                exclude_urls=["https://openstax.org/previous"],
            )
        query = search.call_args.args[0]
        self.assertIn("alternative explanation", query)

    @patch(
        "tutor.services.resources.check_accessibility",
        return_value={
            "link_status": "accessible",
            "link_check_detail": "Link check received HTTP 200; this does not assess educational or mathematical quality.",
        },
    )
    @patch("tutor.services.resources._tavily_search")
    def test_search_returns_at_most_three_resources(self, search, _accessibility):
        search.side_effect = [
            [{"title": f"Lesson {index}", "url": f"https://math.example.edu/{index}"}]
            for index in range(5)
        ]
        with patch.dict(os.environ, {"RESOURCE_SEARCH_PROVIDER": "tavily", "TAVILY_API_KEY": "test"}):
            result = resource_service.search_resources(
                ["algebra", "geometry", "fractions", "calculus", "statistics"]
            )
        self.assertEqual(len(result["resources"]), 3)
        self.assertEqual(search.call_count, 3)

    @patch("tutor.services.resources._public_host", return_value=True)
    @patch("tutor.services.resources.build_opener")
    def test_accessibility_status_only_reports_successful_http_reachability(
        self, build_opener, _public_host
    ):
        build_opener.return_value.open.return_value.__enter__.return_value.status = 204
        result = resource_service.check_accessibility("https://math.example.edu/notes")
        self.assertEqual(result["link_status"], "accessible")
        self.assertIn("HTTP 204", result["link_check_detail"])
        self.assertIn("does not assess educational or mathematical quality", result["link_check_detail"])
