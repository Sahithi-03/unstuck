import json
from types import SimpleNamespace
from unittest.mock import patch

from django.test import Client, TestCase
from openai import APIConnectionError

from .services.nebius import ProviderError, complete_json
from .services.tutor import contains_solution_disclosure


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
            "summary": "This problem uses inverse operations.",
            "concepts": ["linear equations"],
            "prerequisites": ["integer arithmetic"],
            "learning_path": ["Review equality", "Practice inverse operations"],
            "search_queries": ["solving linear equations"],
        },
    )
    def test_valid_analysis_returns_learning_path_and_saves_assignment(self, _complete):
        response = self.post(
            "/api/assignments/analyze/",
            {"assignment": "Solve x + 2 = 5", "context": "I am new to equations."},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["learning_path"][0], "Review equality")
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
