(() => {
  const assignmentInput = document.querySelector("#assignment");
  const analyzeButton = document.querySelector("#analyze-button");
  const learningSection = document.querySelector("#learning-section");
  const discussionSection = document.querySelector("#discussion-section");
  const assignmentError = document.querySelector("#assignment-error");
  const discussionError = document.querySelector("#discussion-error");
  const studentMessage = document.querySelector("#student-message");
  const workingInput = document.querySelector("#working");
  const responseArea = document.querySelector("#response-area");
  let selectedMode = "hint";
  let learningQueries = [];

  function setProgress(stage) {
    const stages = ["start", "exploring", "practicing", "reviewing"];
    const activeIndex = stages.indexOf(stage);
    document.querySelectorAll(".progress-step").forEach((step, index) => {
      step.classList.toggle("is-active", index === activeIndex);
      step.classList.toggle("is-complete", index < activeIndex);
    });
  }

  function csrfToken() {
    return document.querySelector("[name=csrfmiddlewaretoken]").value;
  }

  async function postJson(url, data) {
    const response = await fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": csrfToken(),
      },
      body: JSON.stringify(data),
    });
    let result;
    try {
      result = await response.json();
    } catch {
      throw new Error("The server returned an unreadable response. Please try again.");
    }
    if (!response.ok) throw new Error(result.error || "Something went wrong. Please try again.");
    return result;
  }

  function setLoading(button, loading, busyText) {
    if (!button.dataset.idleText) button.dataset.idleText = button.textContent.trim();
    button.disabled = loading;
    button.textContent = loading ? busyText : button.dataset.idleText;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function appendMathText(parent, text) {
    const mathPattern = /\\\[([\s\S]+?)\\\]|\\\(([\s\S]+?)\\\)|\$([^$\n]+?)\$/g;
    let cursor = 0;
    let match;
    while ((match = mathPattern.exec(text)) !== null) {
      parent.appendChild(document.createTextNode(text.slice(cursor, match.index)));
      const latex = match[1] || match[2] || match[3];
      if (window.katex) {
        const rendered = document.createElement("span");
        try {
          // KaTeX output is generated with trust disabled; untrusted prose stays in text nodes.
          rendered.innerHTML = window.katex.renderToString(latex, {
            throwOnError: false,
            trust: false,
            strict: "ignore",
          });
          parent.appendChild(rendered);
        } catch {
          parent.appendChild(document.createTextNode(match[0]));
        }
      } else {
        parent.appendChild(document.createTextNode(match[0]));
      }
      cursor = mathPattern.lastIndex;
    }
    parent.appendChild(document.createTextNode(text.slice(cursor)));
  }

  function renderList(id, values, className) {
    const list = document.querySelector(id);
    clear(list);
    values.forEach((value) => {
      const item = document.createElement("li");
      item.className = className || "";
      appendMathText(item, value);
      list.appendChild(item);
    });
  }

  function renderResources(resources, message) {
    const container = document.querySelector("#resource-list");
    clear(container);
    if (!resources.length) {
      const empty = document.createElement("p");
      empty.className = "resource-empty";
      empty.textContent = message || "No resources were found for this topic.";
      container.appendChild(empty);
      return;
    }
    resources.slice(0, 3).forEach((resource) => {
      const card = document.createElement("article");
      card.className = "resource-card";
      const link = document.createElement("a");
      link.href = resource.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = resource.title;
      const source = document.createElement("p");
      source.className = "resource-source";
      source.textContent = resource.source;
      const relevance = document.createElement("p");
      relevance.className = "resource-relevance";
      relevance.textContent = resource.relevance;
      card.append(link, source, relevance);
      container.appendChild(card);
    });
  }

  function renderLearningPath(data) {
    document.querySelector("#problem-summary").textContent = data.summary;
    renderList("#concept-list", data.concepts);
    renderList("#prerequisite-list", data.prerequisites.length ? data.prerequisites : ["No specific prerequisites identified"]);
    renderList("#path-list", data.learning_path);
    learningQueries = data.search_queries;
    learningSection.hidden = false;
    discussionSection.hidden = false;
    setProgress("exploring");
    learningSection.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function responseCard(title, entries) {
    const card = document.createElement("article");
    card.className = "response-card";
    const heading = document.createElement("h3");
    heading.textContent = title;
    card.appendChild(heading);
    entries.forEach(([label, text]) => {
      if (!text) return;
      const paragraph = document.createElement("p");
      if (label) {
        const labelNode = document.createElement("span");
        labelNode.className = "response-label";
        labelNode.textContent = label;
        paragraph.appendChild(labelNode);
        paragraph.appendChild(document.createElement("br"));
      }
      appendMathText(paragraph, text);
      card.appendChild(paragraph);
    });
    responseArea.prepend(card);
  }

  async function loadResources() {
    try {
      const data = await postJson("/api/resources/search/", { queries: learningQueries });
      renderResources(data.resources, data.message);
    } catch (error) {
      renderResources([], error.message);
    }
  }

  analyzeButton.addEventListener("click", async () => {
    assignmentError.hidden = true;
    const assignment = assignmentInput.value.trim();
    if (!assignment) {
      assignmentError.textContent = "Add the problem you're working on first.";
      assignmentError.hidden = false;
      assignmentInput.focus();
      return;
    }
    setLoading(analyzeButton, true, "Finding your starting point…");
    try {
      const data = await postJson("/api/assignments/analyze/", { assignment });
      renderLearningPath(data);
      await loadResources();
    } catch (error) {
      assignmentError.textContent = error.message;
      assignmentError.hidden = false;
    } finally {
      setLoading(analyzeButton, false);
    }
  });

  document.querySelectorAll(".mode-button").forEach((button) => {
    button.addEventListener("click", () => {
      selectedMode = button.dataset.mode;
      document.querySelectorAll(".mode-button").forEach((modeButton) => {
        const selected = modeButton === button;
        modeButton.classList.toggle("is-selected", selected);
        modeButton.setAttribute("aria-pressed", String(selected));
      });
    });
  });

  document.querySelector("#send-button").addEventListener("click", async (event) => {
    discussionError.hidden = true;
    const message = studentMessage.value.trim();
    if (!message) {
      discussionError.textContent = "Add a question or tell us what's unclear.";
      discussionError.hidden = false;
      studentMessage.focus();
      return;
    }
    const button = event.currentTarget;
    const assignment = assignmentInput.value.trim();
    const working = workingInput.value || "";
    setLoading(button, true, "Thinking with you…");
    try {
      const data = await postJson("/api/tutor/chat/", {
        assignment,
        student_message: message,
        current_working: working,
        mode: selectedMode,
      });
      responseCard("A next step to try", [
        ["What I notice", data.diagnosis],
        ["", data.guidance],
        ["Your turn", data.reasoning_question],
        ["Suggested next action", data.next_action],
        ["Hint level", `Level ${data.hint_level} of 3`],
      ]);
      studentMessage.value = "";
      setProgress("practicing");
    } catch (error) {
      discussionError.textContent = error.message;
      discussionError.hidden = false;
    } finally {
      setLoading(button, false);
    }
  });

  document.querySelector("#check-button").addEventListener("click", async (event) => {
    discussionError.hidden = true;
    const assignment = assignmentInput.value.trim();
    const working = workingInput.value || "";
    if (!working.trim()) {
      discussionError.textContent = "Add some working in the equation editor so I can respond to your reasoning.";
      discussionError.hidden = false;
      workingInput.focus();
      return;
    }
    const button = event.currentTarget;
    setLoading(button, true, "Reviewing your steps…");
    try {
      const data = await postJson("/api/tutor/check/", {
        assignment,
        current_working: working,
      });
      responseCard("Let's review your reasoning", [
        ["What I notice", data.diagnosis],
        ["Feedback", data.feedback],
        ["Likely errors to revisit", data.likely_errors.join(" · ")],
        ["Justification to add", data.missing_justifications.join(" · ")],
        ["Next step", data.next_step],
        ["What I can't verify", data.uncertainty],
      ]);
      setProgress("reviewing");
    } catch (error) {
      discussionError.textContent = error.message;
      discussionError.hidden = false;
    } finally {
      setLoading(button, false);
    }
  });
})();
