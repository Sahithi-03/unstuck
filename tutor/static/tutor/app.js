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
  const resourceList = document.querySelector("#resource-list");
  let selectedMode = "hint";
  let resourceConcepts = [];
  let currentResources = [];

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
    const status = document.querySelector("#resource-message");
    clear(container);
    status.textContent = message || "";
    currentResources = resources.slice(0, 3);
    if (!resources.length) {
      const empty = document.createElement("p");
      empty.className = "resource-empty";
      empty.textContent = "No resource cards are available to display.";
      container.appendChild(empty);
      return;
    }
    currentResources.forEach((resource, index) => {
      const card = document.createElement("article");
      card.className = "resource-card";
      let link;
      try {
        const url = new URL(resource.url);
        if (url.protocol === "http:" || url.protocol === "https:") {
          link = document.createElement("a");
          link.href = url.href;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          link.textContent = resource.title;
        }
      } catch {
        link = null;
      }
      const title = link || document.createElement("span");
      title.className = "resource-title";
      title.textContent = resource.title;
      const source = document.createElement("p");
      source.className = "resource-source";
      source.textContent = `${resource.source} · ${resource.concept}`;
      const relevance = document.createElement("p");
      relevance.className = "resource-relevance";
      relevance.textContent = resource.relevance;
      const linkStatus = document.createElement("p");
      linkStatus.className = "resource-link-status";
      if (resource.link_status === "accessible") {
        linkStatus.textContent = "Reachability check succeeded; resource quality was not checked.";
      } else if (resource.link_status === "unavailable") {
        linkStatus.textContent = "Reachability check found this link unavailable.";
      } else {
        linkStatus.textContent = "Link reachability was not checked.";
      }

      const checkpoint = document.createElement("div");
      checkpoint.className = "resource-checkpoint";
      const objective = document.createElement("p");
      objective.textContent = `Learning objective: ${resource.learning_objective}`;
      const question = document.createElement("p");
      question.textContent = `Checkpoint: ${resource.checkpoint_question}`;
      const answer = document.createElement("textarea");
      answer.rows = 2;
      answer.maxLength = 1200;
      answer.setAttribute("aria-label", `Your checkpoint answer for ${resource.concept}`);
      answer.placeholder = "Write a short answer after reading…";
      const discuss = document.createElement("button");
      discuss.type = "button";
      discuss.className = "resource-action";
      discuss.dataset.resourceAction = "discuss";
      discuss.dataset.resourceIndex = String(index);
      discuss.textContent = "Discuss my answer with the tutor";
      checkpoint.append(objective, question, answer, discuss);

      const feedback = document.createElement("div");
      feedback.className = "resource-feedback";
      const feedbackLabel = document.createElement("span");
      feedbackLabel.textContent = "Was this useful?";
      feedback.appendChild(feedbackLabel);
      [
        ["too_difficult", "Too difficult"],
        ["not_helpful", "Not helpful"],
      ].forEach(([value, label]) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "resource-action";
        button.dataset.resourceAction = "feedback";
        button.dataset.feedback = value;
        button.dataset.resourceIndex = String(index);
        button.textContent = label;
        feedback.appendChild(button);
      });
      card.append(title, source, relevance, linkStatus);
      if (resource.suggested_section) {
        const section = document.createElement("p");
        section.className = "resource-relevance";
        section.textContent = `Suggested section: ${resource.suggested_section}`;
        card.appendChild(section);
      }
      card.append(checkpoint, feedback);
      container.appendChild(card);
    });
  }

  function renderLearningPath(data) {
    document.querySelector("#problem-summary").textContent = data.summary;
    renderList("#concept-list", data.concepts);
    renderList("#prerequisite-list", data.prerequisites.length ? data.prerequisites : ["No specific prerequisites identified"]);
    renderList("#path-list", data.learning_path);
    resourceConcepts = data.prerequisites.length ? data.prerequisites : data.concepts;
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

  async function loadResources(concepts = resourceConcepts, feedback = "", excludeUrls = []) {
    try {
      const data = await postJson("/api/resources/search/", {
        concepts,
        feedback,
        exclude_urls: excludeUrls,
      });
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
      await loadResources(resourceConcepts);
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

  resourceList.addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-resource-action]");
    if (!button) return;
    const resource = currentResources[Number(button.dataset.resourceIndex)];
    if (!resource) return;

    if (button.dataset.resourceAction === "discuss") {
      const answer = button.parentElement.querySelector("textarea").value.trim();
      studentMessage.value = answer
        ? `Checkpoint question: ${resource.checkpoint_question}\nMy answer: ${answer}`
        : `I read about ${resource.concept}. Can we discuss this checkpoint? ${resource.checkpoint_question}`;
      selectedMode = "explain";
      document.querySelectorAll(".mode-button").forEach((modeButton) => {
        const selected = modeButton.dataset.mode === selectedMode;
        modeButton.classList.toggle("is-selected", selected);
        modeButton.setAttribute("aria-pressed", String(selected));
      });
      discussionSection.scrollIntoView({ behavior: "smooth", block: "start" });
      studentMessage.focus();
      return;
    }

    const feedback = button.dataset.feedback;
    const excluded = currentResources.map((item) => item.url);
    button.disabled = true;
    button.textContent = "Searching for another explanation…";
    await loadResources([resource.concept], feedback, excluded);
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
