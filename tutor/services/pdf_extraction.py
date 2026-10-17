import base64
import json
import logging
import os
from io import BytesIO

import pymupdf
from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI
from pypdf import PdfReader
from pypdf.errors import PdfReadError


logger = logging.getLogger(__name__)
DEFAULT_BASE_URL = "https://api.tokenfactory.us-north1.nebius.com/v1/"
MAX_INLINE_IMAGE_BYTES = 170 * 1024
MAX_QUESTION_COUNT = 30
MAX_QUESTION_TEXT_LENGTH = 6_000
MAX_EXTRACTION_TEXT_LENGTH = 50_000
MAX_CANDIDATES_TEXT_LENGTH = 100_000


class PdfExtractionError(Exception):
    def __init__(self, message, status_code=502, *, extracted_text="", page_count=0):
        super().__init__(message)
        self.status_code = status_code
        self.extracted_text = extracted_text
        self.page_count = page_count


def _positive_int(name, default, maximum):
    value = os.getenv(name, str(default))
    try:
        parsed = int(value)
    except ValueError as exc:
        raise PdfExtractionError(f"{name} must be a positive integer.", 503) from exc
    if not 1 <= parsed <= maximum:
        raise PdfExtractionError(f"{name} must be between 1 and {maximum}.", 503)
    return parsed


def max_upload_bytes():
    return _positive_int("PDF_MAX_UPLOAD_BYTES", 12 * 1024 * 1024, 50 * 1024 * 1024)


def max_page_count():
    return _positive_int("PDF_MAX_PAGES", 12, 50)


def _new_client():
    api_key = os.getenv("NEBIUS_API_KEY")
    model = os.getenv("NEBIUS_MODEL")
    if not api_key or not model:
        raise PdfExtractionError(
            "PDF question extraction is not configured. Set NEBIUS_API_KEY and NEBIUS_MODEL.",
            503,
        )
    timeout = _positive_int("PDF_AI_TIMEOUT_SECONDS", 60, 180)
    return OpenAI(
        api_key=api_key,
        base_url=os.getenv("NEBIUS_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
        timeout=timeout,
        max_retries=1,
    )


def _read_pdf(data):
    if not isinstance(data, bytes) or not data.startswith(b"%PDF-"):
        raise PdfExtractionError("The uploaded file is not a valid PDF.")
    max_upload = max_upload_bytes()
    if len(data) > max_upload:
        limit_mb = (max_upload + 1024 * 1024 - 1) // (1024 * 1024)
        raise PdfExtractionError(
            f"The PDF exceeds the {limit_mb} MB upload limit.", 413
        )
    try:
        reader = PdfReader(BytesIO(data), strict=True)
        if reader.is_encrypted:
            raise PdfExtractionError("Password-protected PDFs are not supported.", 400)
        page_count = len(reader.pages)
        max_pages = max_page_count()
        if page_count < 1:
            raise PdfExtractionError("The PDF contains no pages.", 400)
        if page_count > max_pages:
            raise PdfExtractionError(
                f"The PDF has {page_count} pages; the limit is {max_pages}.", 413
            )
        embedded_text = []
        for page_number, page in enumerate(reader.pages, start=1):
            try:
                page_text = page.extract_text() or ""
            except (KeyError, ValueError, PdfReadError):
                page_text = ""
            embedded_text.append({"page": page_number, "text": page_text.strip()})
    except PdfExtractionError:
        raise
    except (PdfReadError, ValueError, OSError) as exc:
        raise PdfExtractionError(
            "The PDF is damaged or could not be opened.", 400
        ) from exc
    return page_count, embedded_text


def _render_page(document, page_number):
    page = document.load_page(page_number - 1)
    for scale, quality in ((2.0, 82), (1.7, 76), (1.4, 70), (1.1, 64)):
        pixmap = page.get_pixmap(
            matrix=pymupdf.Matrix(scale, scale),
            colorspace=pymupdf.csRGB,
            alpha=False,
        )
        image = pixmap.tobytes(output="jpeg", jpg_quality=quality)
        if len(image) <= MAX_INLINE_IMAGE_BYTES:
            return image
    raise PdfExtractionError(
        f"Page {page_number} cannot be safely reduced for model image upload.", 413
    )


def _needs_layout_extraction(page, embedded_text):
    if len(embedded_text.strip()) < 25 or page.get_images(full=True):
        return True
    lines = {}
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                if not span.get("text", "").strip():
                    continue
                bbox = span["bbox"]
                baseline = round(((bbox[1] + bbox[3]) / 2) / 6)
                lines.setdefault(baseline, []).append(bbox)
    for spans in lines.values():
        spans.sort(key=lambda bbox: bbox[0])
        for first, second in zip(spans, spans[1:]):
            vertical_overlap = min(first[3], second[3]) - max(first[1], second[1])
            shorter_height = min(first[3] - first[1], second[3] - second[1])
            horizontal_gap = second[0] - first[2]
            if (
                shorter_height > 0
                and vertical_overlap > shorter_height * 0.35
                and horizontal_gap > page.rect.width * 0.08
            ):
                return True
    return False


def _parse_layout_result(value):
    if isinstance(value, str):
        value = value.strip()
        if len(value) > MAX_EXTRACTION_TEXT_LENGTH * 2:
            raise PdfExtractionError("The model's page transcription is too large to process.")
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = value
        value = parsed
    if isinstance(value, str):
        text = value.strip()
        if not text or len(text) > MAX_EXTRACTION_TEXT_LENGTH:
            raise PdfExtractionError("The model returned an empty or oversized page transcription.")
        return text
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("text"), str)
        or len(value["text"]) > MAX_EXTRACTION_TEXT_LENGTH
    ):
        raise PdfExtractionError("The model returned an invalid page transcription.")
    text = value["text"].strip()
    if not text:
        raise PdfExtractionError("The model returned an empty page transcription.")
    return text


def _format_extracted_text(page_texts):
    text = "\n\n".join(
        f"Page {item['page']}:\n{item['layout_text'] or item['embedded_text']}"
        for item in page_texts
        if item["layout_text"] or item["embedded_text"]
    )
    if len(text) > MAX_EXTRACTION_TEXT_LENGTH:
        return text[:MAX_EXTRACTION_TEXT_LENGTH] + "\n\n[Extracted text clipped at the display limit.]"
    return text


def _extract_page(client, image, page_number):
    data_url = "data:image/jpeg;base64," + base64.b64encode(image).decode("ascii")
    try:
        response = client.chat.completions.create(
            model=os.getenv("NEBIUS_MODEL"),
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Transcribe all visible text on this mathematics assignment page as plain text, "
                                "preserving equations, symbols, question numbering, subparts, and reading order. "
                                "Do not solve any questions, summarize the page, or add text that is not visible."
                            ),
                        },
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
            temperature=0,
            max_tokens=4096,
        )
    except APITimeoutError as exc:
        logger.warning("Nebius PDF page transcription timed out (page=%s).", page_number)
        raise PdfExtractionError("Nebius document transcription timed out.", 504) from exc
    except APIConnectionError as exc:
        logger.warning("Nebius PDF page transcription could not connect (page=%s).", page_number)
        raise PdfExtractionError("Nebius document transcription is unavailable.", 503) from exc
    except APIStatusError as exc:
        logger.warning(
            "Nebius PDF page transcription failed (status=%s, page=%s).",
            exc.status_code,
            page_number,
        )
        if exc.status_code == 401 or exc.status_code == 403:
            raise PdfExtractionError(
                "Nebius rejected the configured credentials or model access.", 503
            ) from exc
        if exc.status_code == 429:
            raise PdfExtractionError(
                "Nebius document transcription is rate limited. Please retry shortly.", 503
            ) from exc
        raise PdfExtractionError("Nebius document transcription failed.") from exc

    try:
        content = response.choices[0].message.content
    except (AttributeError, IndexError, TypeError) as exc:
        raise PdfExtractionError("Nebius returned an incomplete page transcription.") from exc
    return _parse_layout_result(content)


def _decode_questions(content, page_count):
    if not isinstance(content, str) or not content.strip():
        raise PdfExtractionError("Nebius returned no question candidates.")
    if len(content) > MAX_CANDIDATES_TEXT_LENGTH * 4:
        raise PdfExtractionError("Nebius question data is too large to process.", 413)
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise PdfExtractionError("Nebius returned malformed question data.") from exc
    if not isinstance(data, dict) or not isinstance(data.get("questions"), list):
        raise PdfExtractionError("Nebius returned an invalid question list.")
    raw_questions = data["questions"]
    if len(raw_questions) > MAX_QUESTION_COUNT:
        raise PdfExtractionError("The PDF contains too many question candidates.", 413)
    questions = []
    total_chars = 0
    for index, candidate in enumerate(raw_questions, start=1):
        if not isinstance(candidate, dict):
            raise PdfExtractionError("Nebius returned an invalid question candidate.")
        label = candidate.get("label")
        text = candidate.get("text")
        start = candidate.get("page_start")
        end = candidate.get("page_end")
        subparts = candidate.get("subparts", [])
        warnings = candidate.get("extraction_warnings", [])
        if (
            not isinstance(label, str)
            or not label.strip()
            or len(label) > 100
            or not isinstance(text, str)
            or not text.strip()
            or len(text) > MAX_QUESTION_TEXT_LENGTH
            or not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or not 1 <= start <= end <= page_count
            or not isinstance(subparts, list)
            or len(subparts) > 20
            or any(not isinstance(item, str) or not item.strip() or len(item) > 500 for item in subparts)
            or not isinstance(warnings, list)
            or len(warnings) > 10
            or any(not isinstance(item, str) or not item.strip() or len(item) > 300 for item in warnings)
        ):
            raise PdfExtractionError("Nebius returned an invalid question candidate.")
        total_chars += (
            len(label)
            + len(text)
            + sum(len(part) for part in subparts)
            + sum(len(warning) for warning in warnings)
        )
        if total_chars > MAX_CANDIDATES_TEXT_LENGTH:
            raise PdfExtractionError("The extracted questions are too large to review.", 413)
        questions.append(
            {
                "id": f"q{index}",
                "label": label.strip(),
                "text": text.strip(),
                "page_start": start,
                "page_end": end,
                "subparts": [part.strip() for part in subparts],
                "extraction_warnings": [warning.strip() for warning in warnings],
            }
        )
    return questions


def _identify_questions(client, page_texts, page_count):
    source = "\n\n".join(
        f"--- PAGE {item['page']} ---\n"
        f"Page-image transcription:\n{item['layout_text']}\n"
        f"PDF embedded text (if present):\n{item['embedded_text']}"
        for item in page_texts
    )
    if len(source) > MAX_EXTRACTION_TEXT_LENGTH:
        raise PdfExtractionError(
            "This PDF contains too much extracted text to process at once. "
            "Please upload a shorter document or paste one question manually.",
            413,
        )
    prompt = (
        "Identify distinct mathematics assignment questions from the supplied page text. "
        "Preserve wording, equations, symbols, variables, conditions, and references to figures "
        "or tables. Group subparts with their parent question. Ignore headers, page numbers, "
        "worked examples, and general directions unless they are part of a question. Never invent "
        "missing text. If a question is incomplete or uncertain, include a brief warning. Return "
        "only a JSON object with a questions array. Each item must have label, text, integer "
        "page_start, integer page_end, subparts (string array), and extraction_warnings (string array). "
        "If no questions are identifiable, return {\"questions\": []}."
    )
    try:
        response = client.chat.completions.create(
            model=os.getenv("NEBIUS_MODEL"),
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": source},
            ],
            temperature=0,
            max_tokens=4096,
        )
    except APITimeoutError as exc:
        logger.warning("Nebius question identification timed out.")
        raise PdfExtractionError("Nebius question identification timed out.", 504) from exc
    except APIConnectionError as exc:
        logger.warning("Nebius question identification could not connect.")
        raise PdfExtractionError("Nebius question identification is unavailable.", 503) from exc
    except APIStatusError as exc:
        logger.warning("Nebius question identification failed (status=%s).", exc.status_code)
        if exc.status_code in {401, 403}:
            raise PdfExtractionError(
                "Nebius rejected the configured credentials or model access.", 503
            ) from exc
        if exc.status_code == 429:
            raise PdfExtractionError(
                "Nebius question identification is rate limited. Please retry shortly.", 503
            ) from exc
        raise PdfExtractionError("Nebius question identification failed.") from exc
    try:
        content = response.choices[0].message.content
    except (AttributeError, IndexError, TypeError) as exc:
        raise PdfExtractionError("Nebius returned an incomplete question response.") from exc
    return _decode_questions(content, page_count)


def analyze_pdf(data):
    page_count, embedded_text = _read_pdf(data)
    page_texts = [
        {"page": item["page"], "layout_text": "", "embedded_text": item["text"]}
        for item in embedded_text
    ]
    try:
        client = _new_client()
        document = pymupdf.open(stream=data, filetype="pdf")
        try:
            if document.page_count != page_count:
                raise PdfExtractionError("The PDF page structure could not be read consistently.")
            for page_number, page in enumerate(embedded_text, start=1):
                rendered_page = document.load_page(page_number - 1)
                if _needs_layout_extraction(rendered_page, page["text"]):
                    image = _render_page(document, page_number)
                    layout = _extract_page(client, image, page_number)
                    page_texts[page_number - 1]["layout_text"] = layout
        finally:
            document.close()
        questions = _identify_questions(client, page_texts, page_count)
    except PdfExtractionError as exc:
        exc.extracted_text = _format_extracted_text(page_texts)
        exc.page_count = page_count
        raise
    except (pymupdf.FileDataError, RuntimeError, ValueError, OSError) as exc:
        raise PdfExtractionError(
            "The PDF page images could not be prepared.",
            400,
            extracted_text=_format_extracted_text(page_texts),
            page_count=page_count,
        ) from exc
    return {
        "page_count": page_count,
        "extracted_text": _format_extracted_text(page_texts),
        "questions": questions,
    }
