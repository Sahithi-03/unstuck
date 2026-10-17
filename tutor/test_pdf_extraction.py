import io
import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pymupdf
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from openai import APIStatusError, APITimeoutError
from pypdf import PdfWriter

from .services.nebius import ProviderError
from .services import pdf_extraction


def make_pdf(*page_texts, scanned_page=False):
    document = pymupdf.open()
    for text in page_texts:
        page = document.new_page()
        if not scanned_page:
            page.insert_text((72, 72), text)
    data = document.tobytes()
    document.close()
    return data


def sample_questions():
    return [
        {
            "id": "q1",
            "label": "Question 1",
            "text": "Find the determinant of A.",
            "page_start": 1,
            "page_end": 1,
            "subparts": ["(a) Expand by the first row.", "(b) Explain your method."],
            "extraction_warnings": [],
        },
        {
            "id": "q2",
            "label": "Question 2",
            "text": "State the condition for invertibility.",
            "page_start": 2,
            "page_end": 2,
            "subparts": [],
            "extraction_warnings": ["The final line may be clipped."],
        },
    ]


class PdfExtractionServiceTests(TestCase):
    def test_rejects_non_pdf_and_corrupt_pdf(self):
        for value in (b"not a pdf", b"%PDF-1.7\ncorrupt"):
            with self.subTest(value=value):
                with self.assertRaises(pdf_extraction.PdfExtractionError):
                    pdf_extraction._read_pdf(value)

    def test_checks_password_protection(self):
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        writer.encrypt("secret")
        stream = io.BytesIO()
        writer.write(stream)
        with self.assertRaisesRegex(pdf_extraction.PdfExtractionError, "Password-protected"):
            pdf_extraction._read_pdf(stream.getvalue())

    def test_enforces_configured_size_and_page_limits(self):
        with patch.dict(os.environ, {"PDF_MAX_UPLOAD_BYTES": "20"}):
            with self.assertRaisesRegex(pdf_extraction.PdfExtractionError, "upload limit"):
                pdf_extraction._read_pdf(make_pdf("Question 1"))
        with patch.dict(os.environ, {"PDF_MAX_PAGES": "1"}):
            with self.assertRaisesRegex(pdf_extraction.PdfExtractionError, "limit is 1"):
                pdf_extraction._read_pdf(make_pdf("Question 1", "Question 2"))

    def test_reads_embedded_text_and_skips_visual_transcription_when_layout_is_simple(self):
        document = make_pdf("Question 1: Find the determinant of A.")
        expected_question = sample_questions()[0]
        with (
            patch.dict(os.environ, {"NEBIUS_API_KEY": "test-key", "NEBIUS_MODEL": "vision-test"}),
            patch("tutor.services.pdf_extraction._new_client", return_value=MagicMock()),
            patch("tutor.services.pdf_extraction._extract_page") as parse_page,
            patch(
                "tutor.services.pdf_extraction._identify_questions",
                return_value=[expected_question],
            ) as identify,
        ):
            result = pdf_extraction.analyze_pdf(document)
        self.assertEqual(result["questions"], [expected_question])
        self.assertIn("Find the determinant", result["extracted_text"])
        parse_page.assert_not_called()
        identify.assert_called_once()
        self.assertIn("Find the determinant", identify.call_args.args[1][0]["embedded_text"])

    def test_layout_extraction_is_used_for_multi_column_text_pages(self):
        document = pymupdf.open()
        page = document.new_page()
        page.insert_text((45, 72), "Question 1: Explain the first property.")
        page.insert_text((330, 72), "Question 2: Explain the second property.")
        data = document.tobytes()
        document.close()
        reopened = pymupdf.open(stream=data, filetype="pdf")
        self.assertTrue(
            pdf_extraction._needs_layout_extraction(
                reopened.load_page(0),
                "Question 1: Explain the first property. Question 2: Explain the second property.",
            )
        )
        reopened.close()
        with (
            patch.dict(os.environ, {"NEBIUS_API_KEY": "test-key", "NEBIUS_MODEL": "vision-test"}),
            patch("tutor.services.pdf_extraction._new_client", return_value=MagicMock()),
            patch(
                "tutor.services.pdf_extraction._extract_page",
                return_value="Question 1 ... Question 2 ...",
            ) as parse_page,
            patch(
                "tutor.services.pdf_extraction._identify_questions",
                return_value=[sample_questions()[0]],
            ),
        ):
            pdf_extraction.analyze_pdf(data)
        parse_page.assert_called_once()

    def test_scanned_pdf_text_can_be_identified_from_model_image_transcription(self):
        document = make_pdf(" ", scanned_page=True)
        with (
            patch.dict(os.environ, {"NEBIUS_API_KEY": "test-key", "NEBIUS_MODEL": "vision-test"}),
            patch("tutor.services.pdf_extraction._new_client", return_value=MagicMock()),
            patch(
                "tutor.services.pdf_extraction._extract_page",
                return_value="Question 1: Differentiate the function.",
            ),
            patch(
                "tutor.services.pdf_extraction._identify_questions",
                return_value=[sample_questions()[0]],
            ) as identify,
        ):
            result = pdf_extraction.analyze_pdf(document)
        self.assertEqual(len(result["questions"]), 1)
        self.assertIn("Differentiate", identify.call_args.args[1][0]["layout_text"])

    def test_question_provider_failure_preserves_text_extracted_from_all_pages(self):
        document = make_pdf("Question 1: Define a matrix.")
        with (
            patch.dict(os.environ, {"NEBIUS_API_KEY": "test-key", "NEBIUS_MODEL": "vision-test"}),
            patch("tutor.services.pdf_extraction._new_client", return_value=MagicMock()),
            patch(
                "tutor.services.pdf_extraction._extract_page",
                return_value="Question 1: Define a matrix.",
            ),
            patch(
                "tutor.services.pdf_extraction._identify_questions",
                side_effect=pdf_extraction.PdfExtractionError("Nebius returned malformed question data."),
            ),
        ):
            with self.assertRaises(pdf_extraction.PdfExtractionError) as raised:
                pdf_extraction.analyze_pdf(document)
        self.assertIn("Define a matrix", raised.exception.extracted_text)
        self.assertEqual(raised.exception.page_count, 1)

    def test_multiple_candidates_subparts_and_zero_candidates_are_valid(self):
        data = {"questions": sample_questions()}
        result = pdf_extraction._decode_questions(json.dumps(data), page_count=2)
        self.assertEqual([item["id"] for item in result], ["q1", "q2"])
        self.assertEqual(result[0]["subparts"][0], "(a) Expand by the first row.")
        self.assertEqual(result[1]["page_start"], 2)
        self.assertEqual(pdf_extraction._decode_questions('{"questions":[]}', 1), [])

    def test_layout_response_text_is_preserved(self):
        layout = {"text": "Question 1\nFind det(A)."}
        result = pdf_extraction._parse_layout_result(json.dumps(layout))
        self.assertEqual(result, "Question 1\nFind det(A).")

    def test_plain_text_page_transcription_is_accepted(self):
        result = pdf_extraction._parse_layout_result(
            "Question 1\nFind det(A) using the first row."
        )
        self.assertEqual(result, "Question 1\nFind det(A) using the first row.")

    def test_rejects_empty_or_oversized_page_transcription(self):
        for content in (" ", json.dumps({"text": ""}), "x" * 100_001):
            with self.subTest(content_length=len(content)):
                with self.assertRaises(pdf_extraction.PdfExtractionError):
                    pdf_extraction._parse_layout_result(content)

    def test_page_transcription_uses_configured_nebius_model_and_image_input(self):
        client = MagicMock()
        client.chat.completions.create.return_value = MagicMock(
            choices=[
                MagicMock(message=SimpleNamespace(content="Question 1\nFind det(A)."))
            ]
        )
        model = "nvidia/Nemotron-3.5-Super-VL"
        with patch.dict(os.environ, {"NEBIUS_MODEL": model}):
            result = pdf_extraction._extract_page(client, b"jpeg-page", 1)
        request = client.chat.completions.create.call_args.kwargs
        self.assertEqual(request["model"], model)
        self.assertEqual(result, "Question 1\nFind det(A).")
        content = request["messages"][0]["content"]
        self.assertEqual(content[0]["type"], "text")
        self.assertIn("plain text", content[0]["text"])
        image = content[1]["image_url"]["url"]
        self.assertTrue(image.startswith("data:image/jpeg;base64,"))

    def test_plain_page_transcription_flows_into_question_identification(self):
        document = make_pdf(" ", scanned_page=True)
        client = MagicMock()
        with (
            patch.dict(os.environ, {"NEBIUS_API_KEY": "test-key", "NEBIUS_MODEL": "vision-test"}),
            patch("tutor.services.pdf_extraction._new_client", return_value=client),
            patch("tutor.services.pdf_extraction._extract_page", return_value="Question 1: Find det(A)."),
            patch(
                "tutor.services.pdf_extraction._identify_questions",
                return_value=[sample_questions()[0]],
            ) as identify,
        ):
            result = pdf_extraction.analyze_pdf(document)
        self.assertEqual(result["questions"], [sample_questions()[0]])
        self.assertIn("Question 1: Find det(A).", result["extracted_text"])
        self.assertIn(
            "Question 1: Find det(A).",
            identify.call_args.args[1][0]["layout_text"],
        )

    def test_question_identification_uses_same_configured_model(self):
        model = "nvidia/Nemotron-3.5-Super-VL"
        client = MagicMock()
        client.chat.completions.create.return_value = MagicMock(
            choices=[
                MagicMock(
                    message=SimpleNamespace(
                        content=json.dumps({"questions": [sample_questions()[0]]})
                    )
                )
            ]
        )
        with patch.dict(os.environ, {"NEBIUS_MODEL": model}):
            result = pdf_extraction._identify_questions(
                client,
                [{"page": 1, "layout_text": "", "embedded_text": "Question 1"}],
                1,
            )
        self.assertEqual(result[0]["text"], sample_questions()[0]["text"])
        request = client.chat.completions.create.call_args.kwargs
        self.assertEqual(request["model"], model)
        self.assertEqual(request["response_format"], {"type": "json_object"})

    def test_rejects_malformed_and_out_of_range_question_data(self):
        with self.assertRaisesRegex(pdf_extraction.PdfExtractionError, "malformed"):
            pdf_extraction._decode_questions("not json", page_count=1)
        with self.assertRaisesRegex(pdf_extraction.PdfExtractionError, "invalid question"):
            pdf_extraction._decode_questions(
                json.dumps(
                    {
                        "questions": [
                            {
                                **sample_questions()[0],
                                "page_end": 2,
                            }
                        ]
                    }
                ),
                page_count=1,
            )

    def test_rejects_missing_nebius_configuration(self):
        with patch.dict(os.environ, {"NEBIUS_API_KEY": "", "NEBIUS_MODEL": ""}):
            with self.assertRaisesRegex(pdf_extraction.PdfExtractionError, "NEBIUS_API_KEY"):
                pdf_extraction._new_client()

    @patch("tutor.services.pdf_extraction.OpenAI")
    def test_nebius_client_uses_shared_credentials_and_bounded_timeout(self, openai_client):
        with patch.dict(
            os.environ,
            {
                "NEBIUS_API_KEY": "test-key",
                "NEBIUS_MODEL": "nvidia/Nemotron-3.5-Super-VL",
                "PDF_AI_TIMEOUT_SECONDS": "30",
                "NEBIUS_BASE_URL": "https://nebius.example/v1/",
            },
        ):
            pdf_extraction._new_client()
        openai_client.assert_called_once_with(
            api_key="test-key",
            base_url="https://nebius.example/v1",
            timeout=30,
            max_retries=1,
        )

    def test_maps_nebius_timeout_and_rate_limit_failures(self):
        request = httpx.Request("POST", "https://api.studio.nebius.com/v1/chat/completions")
        client = MagicMock()
        client.chat.completions.create.side_effect = APITimeoutError(request=request)
        with self.assertRaisesRegex(pdf_extraction.PdfExtractionError, "timed out"):
            pdf_extraction._extract_page(client, b"image", 1)

        client.chat.completions.create.side_effect = APIStatusError(
            "rate limited",
            response=httpx.Response(429, request=request),
            body={},
        )
        with self.assertRaisesRegex(pdf_extraction.PdfExtractionError, "rate limited"):
            pdf_extraction._extract_page(client, b"image", 1)

        client.chat.completions.create.side_effect = APIStatusError(
            "unauthorized",
            response=httpx.Response(401, request=request),
            body={},
        )
        with self.assertRaisesRegex(pdf_extraction.PdfExtractionError, "rejected the configured credentials"):
            pdf_extraction._extract_page(client, b"image", 1)


class PdfUploadViewTests(TestCase):
    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.client.get("/")
        self.csrf = self.client.cookies["csrftoken"].value
        self.headers = {"HTTP_X_CSRFTOKEN": self.csrf}
        self.pdf = make_pdf("Question 1: Find the determinant of A.")

    def test_homepage_exposes_configured_pdf_upload_and_question_dialog(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="pdf-file"')
        self.assertContains(response, 'id="question-dialog"')
        self.assertContains(response, 'id="selected-question-text"')
        self.assertContains(response, 'id="source-question-review"')
        self.assertContains(response, "12 MB and 12 pages")

    def upload(self, content=None, name="worksheet.pdf", **headers):
        return self.client.post(
            "/api/assignments/pdf/extract/",
            {"file": SimpleUploadedFile(name, content or self.pdf, content_type="application/pdf")},
            **{**self.headers, **headers},
        )

    @patch(
        "tutor.views.analyze_pdf",
        return_value={
            "page_count": 1,
            "extracted_text": "Question 1: Find the determinant of A.",
            "questions": [sample_questions()[0]],
        },
    )
    def test_upload_returns_candidates_and_stores_session_bound_document(self, _analyze):
        response = self.upload()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["questions"][0]["id"], "q1")
        self.assertEqual(response.json()["page_count"], 1)
        state = self.client.session["pdf_extraction"]
        self.assertEqual(state["document_id"], response.json()["document_id"])
        self.assertNotIn("data", state)

    @patch(
        "tutor.views.analyze_pdf",
        return_value={"page_count": 1, "extracted_text": "Unnumbered prompt text.", "questions": []},
    )
    def test_zero_questions_preserves_extracted_text(self, _analyze):
        response = self.upload()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["questions"], [])
        self.assertEqual(response.json()["extracted_text"], "Unnumbered prompt text.")

    def test_upload_rejects_missing_file_wrong_extension_and_oversize(self):
        missing = self.client.post(
            "/api/assignments/pdf/extract/", {}, **self.headers
        )
        wrong_extension = self.upload(name="worksheet.txt")
        with patch("tutor.views.max_upload_bytes", return_value=10):
            oversized = self.upload()
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(wrong_extension.status_code, 400)
        self.assertEqual(oversized.status_code, 413)

    def test_upload_requires_csrf(self):
        client = Client(enforce_csrf_checks=True)
        response = client.post(
            "/api/assignments/pdf/extract/",
            {"file": SimpleUploadedFile("worksheet.pdf", self.pdf)},
        )
        self.assertEqual(response.status_code, 403)

    @patch(
        "tutor.views.analyze_pdf",
        side_effect=pdf_extraction.PdfExtractionError(
            "Nebius is unavailable.",
            503,
            extracted_text="Question 1: Explain the method.",
            page_count=1,
        ),
    )
    def test_provider_failure_preserves_recovered_text_for_manual_entry(self, _analyze):
        response = self.upload()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["extracted_text"], "Question 1: Explain the method.")

    @patch("tutor.views.build_analysis", return_value={
        "summary": "Review determinants.",
        "concepts": ["determinants"],
        "prerequisites": ["matrix operations"],
        "learning_path": ["Study determinant properties"],
        "steps": [
            {
                "id": "1-determinants",
                "title": "Study determinants",
                "objective": "Explain a determinant property.",
                "instructions": "Review the property.",
                "prerequisite_concepts": ["matrix operations"],
                "practice_prompt": "Explain the property.",
                "validation_criteria": "Describe the property accurately.",
                "hints": ["Hint 1", "Hint 2", "Hint 3", "Hint 4"],
                "status": "available",
                "attempts": [],
                "hint_level": 0,
            }
        ],
    })
    @patch(
        "tutor.views.analyze_pdf",
        return_value={
            "page_count": 1,
            "extracted_text": "Question 1: Find the determinant of A.",
            "questions": [sample_questions()[0]],
        },
    )
    def test_edited_question_hands_off_to_nebius_and_resets_old_progress(
        self, _analyze_pdf, build_analysis
    ):
        old_session = self.client.session
        old_session["learning_session"] = {"assignment": "old", "steps": []}
        old_session.save()
        extracted = self.upload().json()
        edited = "Question 1: Explain the determinant method for matrix A."
        response = self.client.post(
            "/api/assignments/pdf/start/",
            data=json.dumps(
                {
                    "document_id": extracted["document_id"],
                    "question_id": "q1",
                    "question_text": edited,
                }
            ),
            content_type="application/json",
            **self.headers,
        )
        self.assertEqual(response.status_code, 200)
        build_analysis.assert_called_once_with(edited, "")
        session = self.client.session
        self.assertEqual(session["learning_session"]["assignment"], edited)
        self.assertEqual(session["learning_session"]["source_question"]["text"], edited)
        self.assertEqual(session["learning_session"]["steps"][0]["hint_level"], 0)
        self.assertNotIn("pdf_extraction", session)
        self.assertNotEqual(session["learning_session"]["assignment"], "old")

    @patch("tutor.views.analyze_pdf", return_value={
        "page_count": 1,
        "extracted_text": "Question 1.",
        "questions": [sample_questions()[0]],
    })
    @patch(
        "tutor.views.build_analysis",
        side_effect=ProviderError("Nebius is unavailable.", 503),
    )
    def test_nebius_failure_keeps_extracted_question_for_retry(self, _analysis, _pdf):
        extracted = self.upload().json()
        response = self.client.post(
            "/api/assignments/pdf/start/",
            data=json.dumps(
                {
                    "document_id": extracted["document_id"],
                    "question_id": "q1",
                    "question_text": "My corrected question.",
                }
            ),
            content_type="application/json",
            **self.headers,
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.client.session["pdf_extraction"]["document_id"], extracted["document_id"])
        self.assertEqual(self.client.session["pdf_extraction"]["questions"][0]["text"], sample_questions()[0]["text"])

    def test_forged_or_expired_question_selection_is_rejected(self):
        response = self.client.post(
            "/api/assignments/pdf/start/",
            data=json.dumps(
                {
                    "document_id": "00000000-0000-0000-0000-000000000000",
                    "question_id": "q99",
                    "question_text": "Forged prompt",
                }
            ),
            content_type="application/json",
            **self.headers,
        )
        self.assertEqual(response.status_code, 409)

    @patch(
        "tutor.views.analyze_pdf",
        return_value={
            "page_count": 1,
            "extracted_text": "Question 1.",
            "questions": [sample_questions()[0]],
        },
    )
    def test_question_id_must_belong_to_the_uploaded_document(self, _analyze):
        extracted = self.upload().json()
        response = self.client.post(
            "/api/assignments/pdf/start/",
            data=json.dumps(
                {
                    "document_id": extracted["document_id"],
                    "question_id": "q9",
                    "question_text": "Forged question",
                }
            ),
            content_type="application/json",
            **self.headers,
        )
        self.assertEqual(response.status_code, 404)
