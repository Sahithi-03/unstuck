(() => {
  const assignmentInput = document.querySelector("#assignment");
  const setupPanel = document.querySelector("#setup-panel");
  const workspace = document.querySelector("#learning-workspace");
  const assignmentError = document.querySelector("#assignment-error");
  const pdfFileInput = document.querySelector("#pdf-file");
  const pdfDropZone = document.querySelector("#pdf-drop-zone");
  const maxPdfUploadBytes = Number(pdfDropZone.dataset.maxUploadBytes) || 12 * 1024 * 1024;
  const maxPdfUploadMb = Number(pdfDropZone.dataset.maxUploadMb) || 12;
  const pdfStatus = document.querySelector("#pdf-status");
  const pdfProgress = document.querySelector("#pdf-progress");
  const questionDialog = document.querySelector("#question-dialog");
  const questionCandidates = document.querySelector("#question-candidates");
  const selectedQuestionText = document.querySelector("#selected-question-text");
  const stepError = document.querySelector("#step-error");
  const nav = document.querySelector("#step-nav");
  const mathAttempt = document.querySelector("#math-attempt");
  const textAttempt = document.querySelector("#text-attempt");
  const feedbackArea = document.querySelector("#feedback-area");
  const hintArea = document.querySelector("#hint-area");
  const resourcePanel = document.querySelector("#resource-panel");
  const drafts = new Map();

  let learningSession = null;
  let activeStep = null;
  let selectedConcept = "";
  let feedbackResult = null;
  let submissionPending = false;
  let resourceRequestId = 0;
  let resourceQuestionText = "";
  let selectedPdf = null;
  let pdfExtraction = null;
  let selectedQuestionId = "";
  let uploadController = null;
  let pdfUploadPending = false;
  let questionStartPending = false;
  const questionEdits = new Map();

  function csrfToken() {
    return document.querySelector("[name=csrfmiddlewaretoken]").value;
  }

  function renderSelectedQuestionPreview() {
    const preview = document.querySelector("#selected-question-preview");
    preview.hidden = !selectedQuestionText.value.trim();
    renderMathText(preview, selectedQuestionText.value);
  }

  async function requestJson(url, { method = "GET", data } = {}) {
    const options = { method, credentials: "same-origin", headers: {} };
    if (method === "POST") {
      options.headers["Content-Type"] = "application/json";
      options.headers["X-CSRFToken"] = csrfToken();
      options.body = JSON.stringify(data);
    }
    const response = await fetch(url, options);
    let body;
    try {
      body = await response.json();
    } catch {
      throw new Error("The server returned an unreadable response. Please try again.");
    }
    if (!response.ok) throw new Error(body.error || "Something went wrong. Please try again.");
    return body;
  }

  function setBusy(button, busy, busyText) {
    if (!button.dataset.defaultText) button.dataset.defaultText = button.textContent.trim();
    button.disabled = busy;
    button.textContent = busy ? busyText : button.dataset.defaultText;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function mathText(parent, text) {
    const source = String(text || "")
      .replace(/\\{2,}([a-zA-Z])/g, (_match, commandStart) => `\\${commandStart}`)
      .replace(/\\{2,}(\(|\)|\[|\])/g, (_match, delimiter) => `\\${delimiter}`);
    const pattern = /\\\[([\s\S]+?)\\\]|\\\(([\s\S]+?)\\\)|\$\$([\s\S]+?)\$\$|\$([^$\n]+?)\$|\|\|([^|\n]{1,80})\|\|_(infinity|inf|[0-9]+)|\\\|([^\\\n]{1,80})\\\|_(infinity|inf|[0-9]+)/g;
    let offset = 0;
    let match;
    while ((match = pattern.exec(source)) !== null) {
      parent.appendChild(document.createTextNode(source.slice(offset, match.index)));
      const norm = match[5] || match[7];
      const subscript = match[6] || match[8];
      const expression = norm
        ? `\\left\\|${norm.trim()}\\right\\|_{${subscript === "infinity" || subscript === "inf" ? "\\infty" : subscript}}`
        : match[1] || match[2] || match[3] || match[4];
      if (window.katex) {
        const rendered = document.createElement("span");
        try {
          rendered.innerHTML = window.katex.renderToString(
            expression,
            {
              throwOnError: false,
              trust: false,
              strict: "ignore",
            },
          );
          parent.appendChild(rendered);
        } catch {
          parent.appendChild(document.createTextNode(match[0]));
        }
      } else {
        parent.appendChild(document.createTextNode(match[0]));
      }
      offset = pattern.lastIndex;
    }
    parent.appendChild(document.createTextNode(source.slice(offset)));
  }

  function renderMathText(element, text) {
    clear(element);
    mathText(element, text);
  }

  function saveDraft() {
    if (!activeStep) return;
    drafts.set(activeStep.id, {
      math: mathAttempt.value || "",
      text: textAttempt.value,
    });
  }

  function currentAttempt() {
    const math = (mathAttempt.value || "").trim();
    const explanation = textAttempt.value.trim();
    return [math ? `Mathematical working: ${math}` : "", explanation].filter(Boolean).join("\n\n");
  }

  function updateProgress(session) {
    const progress = session.progress;
    document.querySelector("#header-assignment").textContent = session.assignment || "";
    document.querySelector("#header-assignment").hidden = false;
    document.querySelector("#overall-progress").hidden = false;
    document.querySelector("#progress-label").textContent =
      `${progress.completed} of ${progress.total} steps complete`;
    const percentage = progress.total ? Math.round((progress.completed / progress.total) * 100) : 0;
    document.querySelector("#progress-bar").style.width = `${percentage}%`;
    document.querySelector(".progress-track").setAttribute("aria-valuenow", String(percentage));
    document.querySelector("#completion-banner").hidden = !progress.total || progress.completed !== progress.total;
  }

  function renderNavigator() {
    clear(nav);
    learningSession.steps.forEach((step, index) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = `step-nav-item status-${step.status}`;
      button.dataset.stepId = step.id;
      button.disabled = step.status === "locked" || submissionPending;
      button.setAttribute("aria-current", activeStep && step.id === activeStep.id ? "step" : "false");
      const marker = document.createElement("span");
      marker.className = "step-marker";
      marker.textContent = step.status === "completed" ? "✓" : String(index + 1);
      const title = document.createElement("span");
      title.className = "step-nav-title";
      title.textContent = step.title;
      const status = document.createElement("span");
      status.className = "step-nav-status";
      status.textContent = step.status.replace("_", " ");
      button.append(marker, title, status);
      nav.appendChild(button);
    });
  }

  function renderStatus(step) {
    const status = document.querySelector("#step-status");
    status.textContent = step.status.replace("_", " ");
    status.className = `status-badge status-${step.status}`;
  }

  function renderAttempts(step) {
    const details = document.querySelector("#attempt-history");
    const list = document.querySelector("#attempt-list");
    clear(list);
    if (!step.attempts || !step.attempts.length) {
      details.hidden = true;
      return;
    }
    details.hidden = false;
    [...step.attempts].reverse().forEach((attempt, index) => {
      const article = document.createElement("article");
      article.className = "attempt-review";
      const title = document.createElement("h3");
      title.textContent = `Attempt ${step.attempts.length - index} · ${attempt.status.replace("_", " ")}`;
      const answer = document.createElement("p");
      mathText(answer, attempt.attempt);
      const feedback = document.createElement("p");
      mathText(feedback, attempt.feedback);
      article.append(title, answer, feedback);
      if (attempt.correct_parts && attempt.correct_parts.length) {
        const positives = document.createElement("p");
        mathText(positives, `What was sound: ${attempt.correct_parts.join(" · ")}`);
        article.appendChild(positives);
      }
      if (attempt.issues && attempt.issues.length) {
        const gaps = document.createElement("p");
        mathText(gaps, `What to revisit: ${attempt.issues.join(" · ")}`);
        article.appendChild(gaps);
      }
      list.appendChild(article);
    });
  }

  function showFeedback(result) {
    feedbackResult = result;
    clear(feedbackArea);
    const heading = document.createElement("h2");
    const complete = result.status === "completed";
    heading.textContent = complete ? "Checkpoint complete" : "A useful start — keep going";
    feedbackArea.className = `feedback-area ${complete ? "is-complete" : "needs-revision"}`;
    feedbackArea.appendChild(heading);

    if (result.correct_parts.length) {
      const correct = document.createElement("p");
      const label = document.createElement("strong");
      label.textContent = "What you did well: ";
      correct.appendChild(label);
      mathText(correct, result.correct_parts.join(" · "));
      feedbackArea.appendChild(correct);
    }
    if (result.issues.length) {
      const issues = document.createElement("p");
      const label = document.createElement("strong");
      label.textContent = "What needs attention: ";
      issues.appendChild(label);
      mathText(issues, result.issues.join(" · "));
      feedbackArea.appendChild(issues);
    }
    const explanation = document.createElement("p");
    mathText(explanation, result.feedback);
    feedbackArea.appendChild(explanation);
    const next = document.createElement("p");
    const label = document.createElement("strong");
    label.textContent = "Next: ";
    next.appendChild(label);
    mathText(next, result.next_prompt);
    feedbackArea.appendChild(next);
    feedbackArea.hidden = false;
    document.querySelector("#continue-step").disabled =
      submissionPending ||
      !(complete && learningSession.steps.some((step) => step.status === "available"));
  }

  function renderStep(step) {
    activeStep = step;
    feedbackArea.hidden = true;
    hintArea.hidden = true;
    clear(hintArea);
    stepError.hidden = true;
    document.querySelector("#step-position").textContent =
      `STEP ${learningSession.steps.findIndex((item) => item.id === step.id) + 1} OF ${learningSession.steps.length}`;
    renderStatus(step);
    document.querySelector("#step-title").textContent = step.title;
    renderMathText(document.querySelector("#step-objective"), step.objective);
    renderMathText(document.querySelector("#step-instructions"), step.instructions);
    renderMathText(document.querySelector("#practice-prompt"), step.practice_prompt);
    const criteria = document.querySelector("#validation-criteria");
    clear(criteria);
    criteria.appendChild(document.createTextNode("A complete response should: "));
    mathText(criteria, step.validation_criteria);

    const concepts = step.prerequisite_concepts.length
      ? step.prerequisite_concepts
      : learningSession.prerequisites;
    const chips = document.querySelector("#concept-chips");
    clear(chips);
    concepts.forEach((concept) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "concept-chip";
      button.dataset.concept = concept;
      button.textContent = concept;
      chips.appendChild(button);
    });

    const draft = drafts.get(step.id);
    mathAttempt.value = draft ? draft.math : "";
    textAttempt.value = draft ? draft.text : "";
    renderAttempts(step);
    document.querySelector("#request-hint").disabled = step.hint_level >= 4 || submissionPending;
    document.querySelector("#submit-attempt").disabled = step.status === "completed" || submissionPending;
    document.querySelector("#continue-step").disabled =
      !(step.status === "completed" && learningSession.steps.some((item) => item.status === "available"));
    const index = learningSession.steps.findIndex((item) => item.id === step.id);
    document.querySelector("#previous-step").hidden = index <= 0;
    resourcePanel.hidden = true;
    renderNavigator();
  }

  function showWorkspace(session) {
    learningSession = session;
    setupPanel.hidden = true;
    workspace.hidden = false;
    const sourceReview = document.querySelector("#source-question-review");
    sourceReview.hidden = !session.source_question;
    if (session.source_question) {
      const question = session.source_question;
      document.querySelector("#source-question-summary").textContent =
        `${question.label} · page${question.page_start === question.page_end ? "" : "s"} ${question.page_start}${question.page_end === question.page_start ? "" : `–${question.page_end}`}`;
      renderMathText(document.querySelector("#source-question-text"), question.text);
    }
    updateProgress(session);
    if (session.active_step) renderStep(session.active_step);
  }

  function formatFileSize(bytes) {
    return bytes < 1024 * 1024
      ? `${Math.max(1, Math.round(bytes / 1024))} KB`
      : `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }

  function clearPdfState(message = "") {
    selectedPdf = null;
    pdfExtraction = null;
    selectedQuestionId = "";
    questionEdits.clear();
    pdfFileInput.value = "";
    document.querySelector("#pdf-file-status").hidden = true;
    document.querySelector("#upload-pdf").disabled = true;
    document.querySelector("#review-extracted-questions").hidden = true;
    document.querySelector("#pdf-recovered-text-details").hidden = true;
    document.querySelector("#pdf-recovered-text").value = "";
    pdfStatus.textContent = message;
  }

  function choosePdf(file) {
    if (!file) return;
    pdfExtraction = null;
    selectedQuestionId = "";
    questionEdits.clear();
    selectedPdf = file;
    document.querySelector("#pdf-file-name").textContent =
      `${file.name} · ${formatFileSize(file.size)}`;
    document.querySelector("#pdf-file-status").hidden = false;
    document.querySelector("#upload-pdf").disabled = false;
    document.querySelector("#review-extracted-questions").hidden = true;
    document.querySelector("#pdf-recovered-text-details").hidden = true;
    pdfStatus.textContent = "";
    assignmentError.hidden = true;
  }

  function selectQuestion(question) {
    selectedQuestionId = question.id;
    selectedQuestionText.disabled = false;
    selectedQuestionText.value = questionEdits.get(question.id) ?? question.text;
    renderSelectedQuestionPreview();
    document.querySelector("#start-selected-question").disabled = !selectedQuestionText.value.trim();
    questionDialog.querySelectorAll("[data-question-id]").forEach((button) => {
      const selected = button.dataset.questionId === question.id;
      button.setAttribute("aria-pressed", String(selected));
      button.closest(".question-candidate").classList.toggle("is-selected", selected);
    });
    document.querySelector("#question-dialog-error").hidden = true;
  }

  function openQuestionSelection(result) {
    pdfExtraction = result;
    clear(questionCandidates);
    const questions = result.questions;
    questions.forEach((question) => {
      const card = document.createElement("article");
      card.className = "question-candidate";
      const button = document.createElement("button");
      button.type = "button";
      button.className = "question-candidate-select";
      button.dataset.questionId = question.id;
      button.setAttribute("aria-pressed", "false");
      const title = document.createElement("strong");
      title.textContent = question.label;
      const pages = document.createElement("span");
      pages.className = "question-page-range";
      pages.textContent = `Page${question.page_start === question.page_end ? "" : "s"} ${question.page_start}${question.page_end === question.page_start ? "" : `–${question.page_end}`}`;
      const preview = document.createElement("span");
      preview.className = "question-preview";
      mathText(preview, question.text);
      button.append(title, pages, preview);
      card.appendChild(button);
      if (question.subparts.length) {
        const subparts = document.createElement("p");
        subparts.className = "question-subparts";
        mathText(subparts, `Subparts: ${question.subparts.join(", ")}`);
        card.appendChild(subparts);
      }
      if (question.extraction_warnings.length) {
        const warning = document.createElement("p");
        warning.className = "question-warning";
        warning.textContent = `Check extraction: ${question.extraction_warnings.join(" ")}`;
        card.appendChild(warning);
      }
      questionCandidates.appendChild(card);
    });
    document.querySelector("#question-dialog-title").textContent =
      questions.length === 1 ? "Review this extracted question" : "Choose a question to work on";
    document.querySelector("#question-dialog-help").textContent =
      `${questions.length} question${questions.length === 1 ? "" : "s"} detected across ${result.page_count} page${result.page_count === 1 ? "" : "s"}. Review and correct the selected text before starting.`;
    selectedQuestionId = "";
    selectedQuestionText.value = "";
    selectedQuestionText.disabled = true;
    renderSelectedQuestionPreview();
    document.querySelector("#start-selected-question").disabled = true;
    if (!questionDialog.open) questionDialog.showModal();
    if (questions.length === 1) selectQuestion(questions[0]);
    document.querySelector("#question-candidates [data-question-id]")?.focus();
  }

  async function extractPdf() {
    if (!selectedPdf || pdfUploadPending) return;
    if (!selectedPdf.name.toLowerCase().endsWith(".pdf")) {
      pdfStatus.textContent = "Choose a PDF file. You can still paste the assignment text below.";
      return;
    }
    if (selectedPdf.size > maxPdfUploadBytes) {
      pdfStatus.textContent = `This file exceeds the ${maxPdfUploadMb} MB upload limit.`;
      return;
    }
    const button = document.querySelector("#upload-pdf");
    const formData = new FormData();
    formData.append("file", selectedPdf);
    uploadController = new AbortController();
    pdfUploadPending = true;
    setBusy(button, true, "Extracting questions…");
    document.querySelector("#analyze-button").disabled = true;
    document.querySelector("#cancel-pdf-upload").hidden = false;
    document.querySelector("#remove-pdf").disabled = true;
    pdfProgress.hidden = false;
    pdfProgress.removeAttribute("value");
    pdfStatus.textContent = "Uploading the PDF and reading its pages with Nebius…";
    try {
      const response = await fetch("/api/assignments/pdf/extract/", {
        method: "POST",
        credentials: "same-origin",
        headers: { "X-CSRFToken": csrfToken() },
        body: formData,
        signal: uploadController.signal,
      });
      let body;
      try {
        body = await response.json();
      } catch {
        throw new Error("The server returned an unreadable response. Please try again.");
      }
      if (!response.ok) {
        if (body.extracted_text) {
          showRecoveredText(body.extracted_text);
          pdfStatus.textContent =
            `${body.error || "Question identification failed."} Extracted text is available below for manual correction. Retry this file or start a manual text session.`;
        }
        throw new Error(body.error || "PDF extraction failed. Please try again.");
      }
      if (!body.questions.length) {
        showRecoveredText(body.extracted_text || "");
        pdfStatus.textContent = body.extracted_text
          ? "No questions were confidently identified. Review the extracted text below, then copy a question into the assignment box."
          : "No questions or readable text were found. Try a clearer PDF or paste the question manually.";
        assignmentInput.focus();
        return;
      }
      pdfStatus.textContent = `${body.questions.length} question${body.questions.length === 1 ? "" : "s"} detected. Review before starting.`;
      document.querySelector("#review-extracted-questions").hidden = false;
      openQuestionSelection(body);
    } catch (error) {
      if (error.name === "AbortError") {
        pdfStatus.textContent = "PDF upload cancelled. You can choose the file again or paste the text manually.";
      } else {
        pdfStatus.textContent = `${error.message} You can retry this file or paste the question manually.`;
      }
    } finally {
      pdfUploadPending = false;
      uploadController = null;
      setBusy(button, false);
      button.disabled = !selectedPdf;
      document.querySelector("#cancel-pdf-upload").hidden = true;
      document.querySelector("#remove-pdf").disabled = false;
      document.querySelector("#analyze-button").disabled = false;
      pdfProgress.hidden = true;
      pdfProgress.setAttribute("value", "0");
    }
  }

  function showRecoveredText(text) {
    document.querySelector("#pdf-recovered-text").value = text.slice(0, 50000);
    document.querySelector("#pdf-recovered-text-details").hidden = !text;
    assignmentInput.value = text.slice(0, 6000);
  }

  async function openStep(stepId) {
    if (!learningSession || submissionPending) return;
    const target = learningSession.steps.find((step) => step.id === stepId);
    if (!target || target.status === "locked") return;
    saveDraft();
    feedbackResult = null;
    try {
      const url = `/api/learning/session/?step_id=${encodeURIComponent(stepId)}`;
      const result = await requestJson(url);
      learningSession = result.session;
      updateProgress(learningSession);
      renderStep(learningSession.active_step);
    } catch (error) {
      stepError.textContent = error.message;
      stepError.hidden = false;
    }
  }

  async function loadResources(concept) {
    const requestId = ++resourceRequestId;
    selectedConcept = concept;
    resourcePanel.hidden = false;
    document.querySelector("#resource-concept-title").textContent = concept;
    renderMathText(
      document.querySelector("#resource-why"),
      `This idea supports “${activeStep.title}” and may help with: ${activeStep.objective}`,
    );
    resourceQuestionText = `In your own words, what does ${concept} help you understand?`;
    renderMathText(document.querySelector("#resource-question"), resourceQuestionText);
    document.querySelector("#resource-answer").value = "";
    const list = document.querySelector("#resource-list");
    clear(list);
    const loading = document.createElement("p");
    loading.className = "resource-empty";
    loading.textContent = "Looking for learning resources…";
    list.appendChild(loading);
    document.querySelector("#resource-message").textContent = "";
    try {
      const result = await requestJson("/api/resources/search/", {
        method: "POST",
        data: { concepts: [concept] },
      });
      if (requestId !== resourceRequestId) return;
      document.querySelector("#resource-message").textContent = result.message || "";
      clear(list);
      if (!result.resources.length) {
        const empty = document.createElement("p");
        empty.className = "resource-empty";
        empty.textContent = "No resource results are available right now. You can continue with the step or ask the tutor about this concept.";
        list.appendChild(empty);
        return;
      }
      result.resources.slice(0, 3).forEach((resource) => {
        const card = document.createElement("article");
        card.className = "resource-card";
        const url = document.createElement("a");
        try {
          const parsed = new URL(resource.url);
          if (!["http:", "https:"].includes(parsed.protocol)) throw new Error("Invalid link");
          url.href = parsed.href;
          url.target = "_blank";
          url.rel = "noopener noreferrer";
          url.textContent = resource.title;
        } catch {
          url.textContent = resource.title;
          url.removeAttribute("href");
        }
        const source = document.createElement("p");
        source.className = "resource-source";
        source.textContent = resource.source;
        const relevance = document.createElement("p");
        relevance.className = "resource-relevance";
        mathText(relevance, resource.relevance);
        const objective = document.createElement("p");
        objective.className = "resource-relevance";
        mathText(objective, `Learn: ${resource.learning_objective}`);
        const linkState = document.createElement("p");
        linkState.className = "resource-link-status";
        linkState.textContent = resource.link_status === "accessible"
          ? "Reachability check succeeded; quality was not checked."
          : resource.link_status === "unavailable"
            ? "A reachability check found this link unavailable."
            : "Link reachability was not checked.";
        card.append(url, source, relevance, objective, linkState);
        if (resource.suggested_section) {
          const section = document.createElement("p");
          section.className = "resource-relevance";
          mathText(section, `Suggested section: ${resource.suggested_section}`);
          card.appendChild(section);
        }
        list.appendChild(card);
      });
    } catch (error) {
      if (requestId !== resourceRequestId) return;
      document.querySelector("#resource-message").textContent = error.message;
      clear(list);
      const fallback = document.createElement("p");
      fallback.className = "resource-empty";
      fallback.textContent = "Resource discovery is unavailable. Continue with the task or ask the tutor about this idea.";
      list.appendChild(fallback);
    }
  }

  pdfFileInput.addEventListener("change", () => choosePdf(pdfFileInput.files[0]));
  pdfDropZone.addEventListener("click", (event) => {
    if (event.target !== pdfFileInput) pdfFileInput.click();
  });
  pdfDropZone.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      pdfFileInput.click();
    }
  });
  ["dragenter", "dragover"].forEach((eventName) => {
    pdfDropZone.addEventListener(eventName, (event) => {
      event.preventDefault();
      pdfDropZone.classList.add("is-dragging");
    });
  });
  ["dragleave", "drop"].forEach((eventName) => {
    pdfDropZone.addEventListener(eventName, (event) => {
      event.preventDefault();
      pdfDropZone.classList.remove("is-dragging");
    });
  });
  pdfDropZone.addEventListener("drop", (event) => choosePdf(event.dataTransfer.files[0]));
  document.querySelector("#upload-pdf").addEventListener("click", extractPdf);
  document.querySelector("#remove-pdf").addEventListener("click", () => {
    if (uploadController) uploadController.abort();
    clearPdfState();
  });
  document.querySelector("#cancel-pdf-upload").addEventListener("click", () => {
    if (uploadController) uploadController.abort();
  });
  document.querySelector("#review-extracted-questions").addEventListener("click", () => {
    if (pdfExtraction?.questions?.length) openQuestionSelection(pdfExtraction);
  });
  document.querySelector("#use-recovered-text").addEventListener("click", () => {
    const extracted = document.querySelector("#pdf-recovered-text").value.trim();
    assignmentInput.value = extracted.slice(0, 6000);
    assignmentInput.focus();
    pdfStatus.textContent = extracted.length > 6000
      ? "The first 6,000 characters were copied. Select and copy a shorter portion from the full extracted text if needed."
      : "Extracted text copied to the assignment box. Review it before starting.";
  });
  questionCandidates.addEventListener("click", (event) => {
    const button = event.target.closest("[data-question-id]");
    if (!button || !pdfExtraction) return;
    const question = pdfExtraction.questions.find((item) => item.id === button.dataset.questionId);
    if (question) selectQuestion(question);
  });
  selectedQuestionText.addEventListener("input", () => {
    if (!selectedQuestionId) return;
    questionEdits.set(selectedQuestionId, selectedQuestionText.value);
    renderSelectedQuestionPreview();
    document.querySelector("#start-selected-question").disabled = !selectedQuestionText.value.trim();
  });
  document.querySelector("#cancel-question-selection").addEventListener("click", () => {
    questionDialog.close();
  });
  document.querySelector("#start-selected-question").addEventListener("click", async (event) => {
    if (!pdfExtraction || !selectedQuestionId || questionStartPending) return;
    const questionText = selectedQuestionText.value.trim();
    if (!questionText) {
      document.querySelector("#question-dialog-error").textContent = "The selected question cannot be empty.";
      document.querySelector("#question-dialog-error").hidden = false;
      selectedQuestionText.focus();
      return;
    }
    questionEdits.set(selectedQuestionId, questionText);
    questionStartPending = true;
    const button = event.currentTarget;
    setBusy(button, true, "Starting your learning path…");
    document.querySelector("#question-dialog-error").hidden = true;
    try {
      const session = await requestJson("/api/assignments/pdf/start/", {
        method: "POST",
        data: {
          document_id: pdfExtraction.document_id,
          question_id: selectedQuestionId,
          question_text: questionText,
        },
      });
      assignmentInput.value = session.assignment;
      questionDialog.close();
      showWorkspace(session);
    } catch (error) {
      document.querySelector("#question-dialog-error").textContent =
        `${error.message} Your selection and edits are preserved; you can retry or cancel.`;
      document.querySelector("#question-dialog-error").hidden = false;
    } finally {
      questionStartPending = false;
      setBusy(button, false);
      button.disabled = !selectedQuestionText.value.trim();
    }
  });

  document.querySelector("#analyze-button").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    assignmentError.hidden = true;
    const assignment = assignmentInput.value.trim();
    if (!assignment) {
      assignmentError.textContent = "Paste an assignment to get started.";
      assignmentError.hidden = false;
      assignmentInput.focus();
      return;
    }
    setBusy(button, true, "Building your learning path…");
    try {
      const session = await requestJson("/api/assignments/analyze/", {
        method: "POST",
        data: { assignment },
      });
      assignmentInput.value = session.assignment;
      clearPdfState();
      showWorkspace(session);
    } catch (error) {
      assignmentError.textContent = error.message;
      assignmentError.hidden = false;
    } finally {
      setBusy(button, false);
    }
  });

  nav.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-step-id]");
    if (button && !button.disabled) openStep(button.dataset.stepId);
  });

  document.querySelector("#concept-chips").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-concept]");
    if (button) loadResources(button.dataset.concept);
  });

  document.querySelector("#close-resource").addEventListener("click", () => {
    resourcePanel.hidden = true;
    selectedConcept = "";
  });

  document.querySelector("#discuss-resource").addEventListener("click", () => {
    const answer = document.querySelector("#resource-answer").value.trim();
    const question = `I am reviewing ${selectedConcept}. ${resourceQuestionText}${answer ? ` My answer: ${answer}` : ""}`;
    textAttempt.value = textAttempt.value
      ? `${textAttempt.value}\n\n${question}`
      : question;
    resourcePanel.hidden = true;
    textAttempt.focus();
  });

  document.querySelector("#request-hint").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    setBusy(button, true, "Getting one hint…");
    stepError.hidden = true;
    try {
      const result = await requestJson("/api/learning/hint/", {
        method: "POST",
        data: { assignment: learningSession.assignment, step_id: activeStep.id },
      });
      activeStep.hint_level = result.hint_level;
      activeStep.status = result.status;
      renderStatus(activeStep);
      const summary = learningSession.steps.find((step) => step.id === activeStep.id);
      if (summary) summary.status = result.status;
      const hint = document.createElement("article");
      hint.className = "hint-item";
      const title = document.createElement("h3");
      title.textContent = `Hint ${result.hint_level}`;
      const body = document.createElement("p");
      mathText(body, result.hint);
      hint.append(title, body);
      hintArea.appendChild(hint);
      hintArea.hidden = false;
      renderNavigator();
    } catch (error) {
      stepError.textContent = error.message;
      stepError.hidden = false;
    } finally {
      setBusy(button, false);
      button.disabled = activeStep.hint_level >= 4;
    }
  });

  document.querySelector("#submit-attempt").addEventListener("click", async (event) => {
    const attempt = currentAttempt();
    if (!attempt) {
      stepError.textContent = "Write a little about your reasoning before submitting.";
      stepError.hidden = false;
      textAttempt.focus();
      return;
    }
    saveDraft();
    submissionPending = true;
    const button = event.currentTarget;
    setBusy(button, true, "Checking your reasoning…");
    document.querySelector("#request-hint").disabled = true;
    renderNavigator();
    stepError.hidden = true;
    try {
      const result = await requestJson("/api/learning/validate/", {
        method: "POST",
        data: {
          assignment: learningSession.assignment,
          step_id: activeStep.id,
          attempt,
        },
      });
      learningSession = result.session;
      activeStep.status = result.progress_status;
      renderStatus(activeStep);
      activeStep.attempts = [
        ...(activeStep.attempts || []),
        {
          attempt,
          status: result.status,
          feedback: result.feedback,
          correct_parts: result.correct_parts,
          issues: result.issues,
        },
      ].slice(-10);
      showFeedback(result);
      renderAttempts(activeStep);
      updateProgress(learningSession);
      renderNavigator();
      document.querySelector("#submit-attempt").disabled = result.progress_status === "completed";
      document.querySelector("#continue-step").disabled =
        !(result.progress_status === "completed" && learningSession.steps.some((step) => step.status === "available"));
      document.querySelector("#request-hint").disabled = activeStep.hint_level >= 4;
    } catch (error) {
      stepError.textContent = `${error.message} Your attempt is still here; you can retry.`;
      stepError.hidden = false;
      document.querySelector("#submit-attempt").disabled = false;
      document.querySelector("#request-hint").disabled = activeStep.hint_level >= 4;
    } finally {
      submissionPending = false;
      button.textContent = button.dataset.defaultText;
      document.querySelector("#continue-step").disabled =
        !(activeStep.status === "completed" && learningSession.steps.some((step) => step.status === "available"));
      renderNavigator();
    }
  });

  document.querySelector("#continue-step").addEventListener("click", () => {
    const index = learningSession.steps.findIndex((step) => step.id === activeStep.id);
    const next = learningSession.steps[index + 1];
    if (activeStep.status === "completed" && next && next.status !== "locked") {
      openStep(next.id);
    }
  });

  document.querySelector("#previous-step").addEventListener("click", () => {
    const index = learningSession.steps.findIndex((step) => step.id === activeStep.id);
    const previous = learningSession.steps[index - 1];
    if (previous && previous.status !== "locked") openStep(previous.id);
  });

  Promise.resolve()
    .then(() => requestJson("/api/learning/session/"))
    .then((result) => {
      if (result.available && result.session) {
        learningSession = result.session;
        assignmentInput.value = result.session.assignment || "";
        showWorkspace(result.session);
      }
    })
    .catch(() => {
      // The setup form remains usable if no saved session is available.
    });
})();
