/* Claude Code assistant rail: streaming chat over the local Claude Code CLI. */

const $ = (id) => document.getElementById(id);

const escapeHtml = (value) =>
  String(value).replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));

function inline(text) {
  return text
    .replace(/`([^`\n]+)`/g, (_, code) => `<code>${code}</code>`)
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer noopener">$1</a>');
}

function renderMarkdown(source) {
  const lines = String(source).replace(/\r\n/g, "\n").split("\n");
  const out = [];
  let index = 0;
  let list = null;
  const closeList = () => {
    if (list) {
      out.push(`</${list}>`);
      list = null;
    }
  };
  while (index < lines.length) {
    const line = lines[index];
    if (/^\s*```/.test(line)) {
      closeList();
      const buffer = [];
      index += 1;
      while (index < lines.length && !/^\s*```/.test(lines[index])) {
        buffer.push(lines[index]);
        index += 1;
      }
      index += 1;
      out.push(`<pre><code>${escapeHtml(buffer.join("\n"))}</code></pre>`);
      continue;
    }
    if (/^\s*$/.test(line)) {
      closeList();
      index += 1;
      continue;
    }
    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    if (heading) {
      closeList();
      const level = Math.min(4, heading[1].length) + 2;
      out.push(`<h${level}>${inline(escapeHtml(heading[2]))}</h${level}>`);
      index += 1;
      continue;
    }
    if (/^\s*([-*_])\1{2,}\s*$/.test(line)) {
      closeList();
      out.push("<hr>");
      index += 1;
      continue;
    }
    const unordered = line.match(/^\s*[-*+]\s+(.*)$/);
    const ordered = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (unordered || ordered) {
      const kind = unordered ? "ul" : "ol";
      if (list !== kind) {
        closeList();
        out.push(`<${kind}>`);
        list = kind;
      }
      out.push(`<li>${inline(escapeHtml((unordered || ordered)[1]))}</li>`);
      index += 1;
      continue;
    }
    const quote = line.match(/^\s*>\s?(.*)$/);
    if (quote) {
      closeList();
      out.push(`<blockquote>${inline(escapeHtml(quote[1]))}</blockquote>`);
      index += 1;
      continue;
    }
    if (/^\s*\|.*\|\s*$/.test(line) && index + 1 < lines.length && /^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$/.test(lines[index + 1])) {
      closeList();
      const cells = (row) => row.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());
      const header = cells(line);
      const rows = [];
      index += 2;
      while (index < lines.length && /^\s*\|.*\|\s*$/.test(lines[index])) {
        rows.push(cells(lines[index]));
        index += 1;
      }
      const head = header.map((cell) => `<th>${inline(escapeHtml(cell))}</th>`).join("");
      const body = rows.map((row) => `<tr>${row.map((cell) => `<td>${inline(escapeHtml(cell))}</td>`).join("")}</tr>`).join("");
      out.push(`<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`);
      continue;
    }
    const paragraph = [line];
    index += 1;
    while (
      index < lines.length &&
      !/^\s*$/.test(lines[index]) &&
      !/^(#{1,4})\s|^\s*```|^\s*[-*+]\s|^\s*\d+[.)]\s|^\s*>/.test(lines[index])
    ) {
      paragraph.push(lines[index]);
      index += 1;
    }
    out.push(`<p>${inline(escapeHtml(paragraph.join("\n"))).replace(/\n/g, "<br>")}</p>`);
  }
  closeList();
  return out.join("");
}

async function readJson(response) {
  let payload = {};
  try {
    payload = await response.json();
  } catch (_error) {
    payload = {};
  }
  if (!response.ok) throw new Error(payload.error || `请求失败（HTTP ${response.status}）`);
  return payload;
}

async function postJson(url, payload) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Paper-Translator": "1" },
    body: JSON.stringify(payload || {}),
  });
  return readJson(response);
}

export function createAgentPanel({ getJob, getPage }) {
  const nodes = {
    panel: $("agent-panel"),
    body: $("agent-transcript"),
    empty: $("agent-empty"),
    messages: $("agent-messages"),
    input: $("agent-input"),
    send: $("agent-send"),
    context: $("agent-context"),
    hint: $("agent-hint"),
    dot: $("agent-status-dot"),
    suggestions: Array.from(document.querySelectorAll("[data-agent-prompt]")),
  };
  const state = {
    available: false,
    busy: false,
    controller: null,
    jobId: null,
    jobTitle: "",
    page: 1,
    currentKey: undefined,
    current: null,
  };

  function setStatus(kind, text) {
    if (!nodes.dot) return;
    nodes.dot.className = `agent-dot ${kind}`;
    nodes.dot.title = text || "";
  }

  function updateContext() {
    if (!nodes.context) return;
    if (!state.jobId) {
      nodes.context.hidden = false;
      nodes.context.innerHTML = '<span class="ctx-paper">整个文献库</span><span class="ctx-dot"></span><span class="ctx-page">未打开论文</span>';
      return;
    }
    nodes.context.hidden = false;
    nodes.context.replaceChildren();
    const paper = document.createElement("span");
    paper.className = "ctx-paper";
    paper.textContent = state.jobTitle || "当前论文";
    paper.title = state.jobTitle || "";
    const dot = document.createElement("span");
    dot.className = "ctx-dot";
    const page = document.createElement("span");
    page.className = "ctx-page";
    page.textContent = `第 ${state.page} 页`;
    nodes.context.append(paper, dot, page);
  }

  function syncControls() {
    const text = nodes.input.value.trim();
    nodes.send.disabled = state.busy ? false : !state.available || !text;
    nodes.input.disabled = !state.available;
    if (!state.available) {
      nodes.hint.textContent = "未检测到 Claude Code CLI";
    } else if (state.busy) {
      nodes.hint.textContent = "正在阅读与思考…";
    } else {
      nodes.hint.textContent = "Enter 发送 · Shift+Enter 换行";
    }
    nodes.suggestions.forEach((button) => {
      button.disabled = !state.available || state.busy;
    });
  }

  function growInput() {
    nodes.input.style.height = "auto";
    nodes.input.style.height = `${Math.min(170, Math.max(34, nodes.input.scrollHeight))}px`;
  }

  function nearBottom() {
    return nodes.body.scrollHeight - nodes.body.scrollTop - nodes.body.clientHeight < 90;
  }

  function scrollToEnd(force = false) {
    if (force || nearBottom()) nodes.body.scrollTop = nodes.body.scrollHeight + 40;
  }

  function showEmpty(show) {
    nodes.empty.hidden = !show;
    nodes.messages.hidden = show;
  }

  function clearTranscript() {
    nodes.messages.replaceChildren();
    showEmpty(true);
    state.current = null;
  }

  function addUserMessage(text) {
    const bubble = document.createElement("div");
    bubble.className = "msg-user";
    bubble.textContent = text;
    nodes.messages.append(bubble);
    showEmpty(false);
  }

  function addAssistantMessage() {
    const wrapper = document.createElement("div");
    wrapper.className = "msg-assistant is-streaming";
    const tools = document.createElement("div");
    tools.className = "msg-tools";
    const markdown = document.createElement("div");
    markdown.className = "md";
    wrapper.append(tools, markdown);
    nodes.messages.append(wrapper);
    const record = { wrapper, tools, markdown, raw: "", frame: 0, lastChip: null, note: null };
    state.current = record;
    scrollToEnd(true);
    return record;
  }

  function scheduleRender(record) {
    if (record.frame) return;
    record.frame = requestAnimationFrame(() => {
      record.frame = 0;
      const shouldStick = nearBottom();
      record.markdown.innerHTML = renderMarkdown(record.raw);
      if (shouldStick) scrollToEnd(true);
    });
  }

  function addToolChip(name, detail = "") {
    const record = state.current;
    if (!record) return;
    const chip = document.createElement("span");
    chip.className = "tool-chip";
    const spinner = document.createElement("span");
    spinner.className = "tool-spinner";
    const label = document.createElement("span");
    label.className = "tool-name";
    label.textContent = name;
    chip.append(spinner, label);
    if (detail) {
      const detailNode = document.createElement("span");
      detailNode.className = "tool-detail";
      detailNode.textContent = detail;
      chip.append(detailNode);
    }
    record.tools.append(chip);
    record.lastChip = chip;
    scrollToEnd();
  }

  function updateToolChip(name, detail) {
    const record = state.current;
    const chip = record?.lastChip;
    if (!chip) return;
    let detailNode = chip.querySelector(".tool-detail");
    if (detail) {
      if (!detailNode) {
        detailNode = document.createElement("span");
        detailNode.className = "tool-detail";
        chip.append(detailNode);
      }
      detailNode.textContent = detail;
    }
    if (name) chip.querySelector(".tool-name").textContent = name;
  }

  function finishToolChip() {
    const chip = state.current?.lastChip;
    if (!chip) return;
    chip.classList.add("done");
    const spinner = chip.querySelector(".tool-spinner");
    if (spinner) {
      const check = document.createElement("span");
      check.className = "tool-check";
      check.textContent = "✓";
      spinner.replaceWith(check);
    }
  }

  function addError(message) {
    const record = state.current;
    const target = record ? record.wrapper : nodes.messages;
    const box = document.createElement("div");
    box.className = "agent-error";
    box.textContent = message;
    target.append(box);
    showEmpty(false);
    scrollToEnd(true);
  }

  function addNote(message) {
    const record = state.current;
    const note = document.createElement("div");
    note.className = "agent-note";
    note.textContent = message;
    (record ? record.wrapper : nodes.messages).append(note);
  }

  function setBusy(busy) {
    state.busy = busy;
    nodes.send.classList.toggle("stop", busy);
    nodes.send.innerHTML = busy
      ? '<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="4.5" y="4.5" width="7" height="7" rx="1.2"/></svg>'
      : '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 13V3M4 6.8 8 3l4 3.8"/></svg>';
    nodes.send.title = busy ? "停止" : "发送";
    nodes.send.setAttribute("aria-label", busy ? "停止" : "发送");
    syncControls();
  }

  function handleEvent(event) {
    switch (event.type) {
      case "ready":
        break;
      case "init":
        if (event.model) setStatus("online", `Claude Code · ${event.model}`);
        break;
      case "text": {
        const record = state.current;
        if (!record) break;
        record.raw += event.text || "";
        scheduleRender(record);
        break;
      }
      case "tool":
        addToolChip(event.name || "工具");
        break;
      case "tool_detail":
        updateToolChip(event.name, event.detail);
        break;
      case "tool_done":
        finishToolChip();
        break;
      case "notice":
        addNote(event.message || "");
        break;
      case "done":
        if (event.is_error) addError("本轮回答未完成，请重试。");
        break;
      case "error":
        addError(event.message || "助手运行失败。");
        break;
      default:
        break;
    }
  }

  async function consume(response, record) {
    if (!response.body) {
      const text = await response.text();
      if (text) handleEvent({ type: "error", message: text.slice(0, 300) });
      return;
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let boundary = buffer.indexOf("\n\n");
      while (boundary >= 0) {
        const chunk = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        for (const line of chunk.split("\n")) {
          if (!line.startsWith("data: ")) continue;
          try {
            handleEvent(JSON.parse(line.slice(6)));
          } catch (_error) {
            /* ignore malformed frames */
          }
        }
        boundary = buffer.indexOf("\n\n");
      }
      if (record.raw) scrollToEnd();
    }
  }

  async function send(text) {
    const message = String(text || "").trim();
    if (!message || state.busy || !state.available) return;
    addUserMessage(message);
    nodes.input.value = "";
    growInput();
    const record = addAssistantMessage();
    const note = document.createElement("div");
    note.className = "agent-note";
    note.textContent = "正在阅读…";
    record.wrapper.append(note);
    setBusy(true);
    scrollToEnd(true);
    state.controller = new AbortController();
    try {
      const response = await fetch("/api/agent/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Paper-Translator": "1" },
        body: JSON.stringify({ job_id: state.jobId, page: state.page, message }),
        signal: state.controller.signal,
      });
      if (!response.ok) {
        const payload = await readJson(response).catch((error) => ({ error: error.message }));
        throw new Error(payload.error || `请求失败（HTTP ${response.status}）`);
      }
      await consume(response, record);
    } catch (error) {
      if (error.name === "AbortError") {
        addNote("已停止。");
        fetch("/api/agent/cancel", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-Paper-Translator": "1" },
          body: JSON.stringify({ job_id: state.jobId }),
        }).catch(() => {});
      } else {
        addError(error.message || "助手请求失败。");
      }
    } finally {
      if (note.isConnected) note.remove();
      if (state.current) {
        state.current.wrapper.classList.remove("is-streaming");
        state.current.tools.querySelectorAll(".tool-chip:not(.done)").forEach((chip) => {
          chip.classList.add("done");
          const spinner = chip.querySelector(".tool-spinner");
          if (spinner) {
            const check = document.createElement("span");
            check.className = "tool-check";
            check.textContent = "✓";
            spinner.replaceWith(check);
          }
        });
        if (state.current.frame) {
          cancelAnimationFrame(state.current.frame);
          state.current.frame = 0;
          state.current.markdown.innerHTML = renderMarkdown(state.current.raw);
        }
        state.current = null;
      }
      state.controller = null;
      setBusy(false);
      nodes.input.focus();
      scrollToEnd();
    }
  }

  async function resetConversation() {
    if (state.controller) {
      try {
        state.controller.abort();
      } catch (_error) {
        /* already closed */
      }
    }
    const jobId = state.jobId;
    clearTranscript();
    try {
      await postJson("/api/agent/reset", { job_id: jobId });
    } catch (_error) {
      /* resetting is best-effort */
    }
  }

  function setPaper(job) {
    const jobId = job?.id != null ? String(job.id) : null;
    const title = job ? String(job.translated_title || job.display_title || job.filename || "未命名论文") : "";
    const changed = state.currentKey !== jobId;
    state.currentKey = jobId;
    state.jobId = jobId;
    state.jobTitle = title;
    if (!changed) {
      updateContext();
      return;
    }
    if (state.controller) {
      try {
        state.controller.abort();
      } catch (_error) {
        /* ignore */
      }
    }
    state.page = Number(getPage()) || 1;
    clearTranscript();
    updateContext();
    syncControls();
    fetch("/api/agent/reset", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Paper-Translator": "1" },
      body: JSON.stringify({ job_id: jobId }),
    }).catch(() => {});
  }

  function setPage(page) {
    state.page = Number(page) || 1;
    updateContext();
  }

  async function init() {
    try {
      const response = await fetch("/api/agent/info", { headers: { "Accept": "application/json" } });
      const info = await readJson(response);
      state.available = Boolean(info.available);
      if (state.available) {
        setStatus("online", `Claude Code ${info.version || ""} · 只读文献访问`);
      } else {
        setStatus("offline", "未检测到 Claude Code CLI");
        const note = document.createElement("p");
        note.className = "agent-empty-hint";
        note.textContent = "未检测到本机的 Claude Code。安装并登录 CLI 后刷新页面即可使用。";
        nodes.empty.append(note);
      }
    } catch (_error) {
      state.available = false;
      setStatus("offline", "无法连接本地助手服务");
    }
    syncControls();
    updateContext();
  }

  nodes.input.addEventListener("input", () => {
    growInput();
    syncControls();
  });
  nodes.input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      void send(nodes.input.value);
    }
  });
  nodes.send.addEventListener("click", () => {
    if (state.busy) {
      try {
        state.controller?.abort();
      } catch (_error) {
        /* ignore */
      }
      return;
    }
    void send(nodes.input.value);
  });
  nodes.suggestions.forEach((button) => {
    button.addEventListener("click", () => {
      const prompt = button.dataset.agentPrompt || button.textContent;
      void send(prompt);
    });
  });
  const newButton = $("agent-new");
  if (newButton) newButton.addEventListener("click", () => void resetConversation());

  void init();

  return { setPaper, setPage, reset: resetConversation };
}
