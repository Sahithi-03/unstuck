import json
import os
from types import SimpleNamespace
from unittest.mock import patch

from django.test import Client, TestCase
from openai import APIConnectionError

from .services.nebius import ProviderError, complete_json
from .services import resources as resource_service
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
