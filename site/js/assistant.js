/*
 * Literature Assistant — floating chat widget, included on every public page.
 *
 * Talks to the Cloudflare Worker in worker/ (GitHub Pages can't run server
 * code). Set ASSISTANT_ENDPOINT to "" to hide the widget everywhere.
 *
 * Everything the model returns is built with DOM text nodes, never innerHTML
 * — the answer is LLM output grounded in third-party text, so treat it as
 * untrusted. renderMarkdown() understands only the little the model uses:
 * paragraphs, "-"/"*"/"1." lists, **bold**, *italics*, and [n] citations.
 */
(function () {
  const ASSISTANT_ENDPOINT = "https://orangutan-assistant.orangutan-dashboard.workers.dev/api/chat";
  if (!ASSISTANT_ENDPOINT) return;

  // Soft launch: on the public site the widget stays hidden unless a page is
  // opened once with ?assistant=1 (remembered in this browser; ?assistant=0
  // forgets it). Always shown on localhost. Set PUBLIC = true to go live.
  const PUBLIC = false;
  if (!PUBLIC && !/^(localhost|127\.0\.0\.1)$/.test(location.hostname)) {
    const flag = new URLSearchParams(location.search).get("assistant");
    try {
      if (flag === "1") localStorage.setItem("assistant-preview", "1");
      if (flag === "0") localStorage.removeItem("assistant-preview");
      if (localStorage.getItem("assistant-preview") !== "1") return;
    } catch {
      if (flag !== "1") return; // storage blocked: show only with the flag present
    }
  }

  const TYPE_LABELS = {
    journal: "Journal article",
    preprint: "Preprint",
    book: "Book",
    historic_newspaper: "Historic newspaper",
    dashboard_data: "Dashboard data",
    thesis: "Thesis",
    report: "Report",
  };
  const KIND_LABELS = { fulltext: "full text", abstract: "abstract only" };
  const EXAMPLES = [
    "When was the Tapanuli orangutan described as a new species?",
    "How many orangutans are left in the wild?",
    "What are the main threats to Bornean orangutans?",
    "Do orangutan rehabilitation and release programmes work?",
  ];
  // The Worker doesn't stream, so show what it's doing while it works.
  const PROGRESS = [
    [0, "Searching the literature…"],
    [2500, "Reading the most relevant passages…"],
    [6000, "Writing the answer…"],
    [15000, "Still writing — longer questions take a little more time…"],
  ];
  const history = []; // last few turns, sent back for follow-up questions

  const el = (tag, cls, text) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  };
  const link = (href, cls, text, title) => {
    const a = el(href ? "a" : "span", cls, text);
    if (href) {
      a.href = href;
      a.target = "_blank";
      a.rel = "noopener";
    }
    if (title) a.title = title;
    return a;
  };

  // --- layout ---------------------------------------------------------------
  const toggle = el("button", "assistant-toggle", "Ask the literature");
  toggle.type = "button";
  toggle.setAttribute("aria-expanded", "false");
  toggle.setAttribute("aria-controls", "assistant-panel");

  const panel = el("section", "assistant-panel");
  panel.id = "assistant-panel";
  panel.hidden = true;
  panel.setAttribute("aria-label", "Literature Assistant");

  const head = el("div", "assistant-head");
  head.append(el("h2", null, "Literature Assistant"));
  const headBtns = el("div", "assistant-head-btns");
  const reset = el("button", "assistant-reset", "New chat");
  reset.type = "button";
  const close = el("button", "assistant-close", "×");
  close.type = "button";
  close.setAttribute("aria-label", "Close assistant");
  headBtns.append(reset, close);
  head.append(headBtns);

  const note = el("p", "assistant-note",
    "AI-generated answers drawn only from ~6,000 indexed orangutan papers, books, theses, " +
    "historic newspapers and the map's alert data. Always check the cited sources.");

  const log = el("div", "assistant-log");
  log.setAttribute("aria-live", "polite");

  const form = el("form", "assistant-form");
  const input = el("textarea");
  input.rows = 2;
  input.maxLength = 500;
  input.placeholder = "Ask about orangutan research…";
  input.setAttribute("aria-label", "Your question");
  const send = el("button", null, "Send");
  send.type = "submit";
  form.append(input, send);

  panel.append(head, note, log, form);
  document.body.append(toggle, panel);

  function showWelcome() {
    log.textContent = "";
    const box = el("div", "assistant-welcome");
    box.append(el("p", null, "Ask a question about orangutans. Try one of these:"));
    EXAMPLES.forEach((q) => {
      const b = el("button", "assistant-example", q);
      b.type = "button";
      b.addEventListener("click", () => ask(q));
      box.append(b);
    });
    log.append(box);
  }
  showWelcome();

  function setOpen(open) {
    panel.hidden = !open;
    toggle.hidden = open;
    toggle.setAttribute("aria-expanded", String(open));
    if (open) input.focus();
    else toggle.focus();
  }
  toggle.addEventListener("click", () => setOpen(true));
  close.addEventListener("click", () => setOpen(false));
  panel.addEventListener("keydown", (e) => { if (e.key === "Escape") setOpen(false); });
  reset.addEventListener("click", () => {
    history.length = 0;
    showWelcome();
    input.focus();
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
  });

  // On the map page, pass whichever species the alert switcher shows.
  function currentSpecies() {
    const sel = document.getElementById("species-select");
    return sel ? sel.value : null;
  }

  function addMessage(role, text) {
    log.querySelector(".assistant-welcome")?.remove();
    const msg = el("div", `assistant-msg ${role}`);
    msg.append(el("div", "assistant-bubble", text));
    log.append(msg);
    log.scrollTop = log.scrollHeight;
    return msg;
  }

  // --- answer rendering -----------------------------------------------------
  function renderInline(parent, text, byN) {
    // **bold**, *italics* (species names) and [n] citations; the rest plain text.
    text.split(/(\*\*[^*]+\*\*|\*[^*\s][^*]*\*|\[\d+(?:\s*,\s*\d+)*\])/).forEach((part) => {
      if (!part) return;
      const bold = part.match(/^\*\*([^*]+)\*\*$/);
      const italic = !bold && part.match(/^\*([^*]+)\*$/);
      const cite = part.match(/^\[(\d+(?:\s*,\s*\d+)*)\]$/);
      if (bold) {
        renderInline(parent.appendChild(el("strong")), bold[1], byN);
      } else if (italic) {
        renderInline(parent.appendChild(el("em")), italic[1], byN);
      } else if (cite) {
        cite[1].split(/\s*,\s*/).forEach((n) => {
          const src = byN[n];
          if (!src) return;
          parent.append(link(src.url, "assistant-cite", `[${n}]`, `${src.title}${src.year ? ` (${src.year})` : ""}`));
        });
      } else {
        parent.append(document.createTextNode(part));
      }
    });
  }

  function renderMarkdown(container, text, byN) {
    let list = null;
    text.split(/\n/).forEach((raw) => {
      const line = raw.trimEnd();
      const bullet = line.match(/^\s*(?:[-*•]|\d+[.)])\s+(.*)$/);
      if (bullet) {
        if (!list) list = container.appendChild(el(/^\s*\d/.test(line) ? "ol" : "ul"));
        renderInline(list.appendChild(el("li")), bullet[1], byN);
        return;
      }
      list = null;
      if (!line.trim()) return;
      const heading = line.match(/^#{1,4}\s+(.*)$/);
      renderInline(container.appendChild(el(heading ? "h4" : "p")), heading ? heading[1] : line, byN);
    });
  }

  function renderAnswer(msg, data) {
    const bubble = msg.querySelector(".assistant-bubble");
    bubble.textContent = "";
    const byN = Object.fromEntries((data.sources || []).map((s) => [String(s.n), s]));
    renderMarkdown(bubble, data.answer, byN);

    const cited = (data.sources || []).filter((s) => s.cited);
    if (!cited.length) return;
    const box = el("details", "assistant-sources");
    box.open = true;
    box.append(el("summary", "assistant-sources-head", `Sources (${cited.length})`));
    const ol = el("ol");
    cited.forEach((s) => {
      const li = el("li");
      li.value = s.n;
      li.append(link(s.url, null, s.title));
      const meta = [s.authors, s.year, s.venue || TYPE_LABELS[s.type] || s.type].filter(Boolean).join(" · ");
      const metaEl = el("div", "assistant-src-meta", meta);
      if (KIND_LABELS[s.kind]) metaEl.append(" ", el("span", `assistant-badge ${s.kind}`, KIND_LABELS[s.kind]));
      li.append(metaEl);
      ol.append(li);
    });
    box.append(ol);
    msg.append(box);
  }

  // --- asking ---------------------------------------------------------------
  async function ask(question) {
    question = question.trim();
    if (question.length < 3 || send.disabled) return;
    input.value = "";
    addMessage("user", question);
    const reply = addMessage("bot", PROGRESS[0][1]);
    reply.classList.add("loading-msg");
    const bubble = reply.querySelector(".assistant-bubble");
    const timers = PROGRESS.slice(1).map(([ms, text]) => setTimeout(() => { bubble.textContent = text; }, ms));
    send.disabled = input.disabled = true;

    try {
      const resp = await fetch(ASSISTANT_ENDPOINT, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question, species: currentSpecies(), history }),
      });
      const data = await resp.json().catch(() => ({}));
      reply.classList.remove("loading-msg");
      if (!resp.ok || !data.answer) {
        reply.classList.add("error");
        bubble.textContent = data.error || "Something went wrong. Please try again.";
        return;
      }
      renderAnswer(reply, data);
      history.push({ role: "user", content: question }, { role: "assistant", content: data.answer });
      history.splice(0, history.length - 4);
    } catch {
      reply.classList.remove("loading-msg");
      reply.classList.add("error");
      bubble.textContent = "Couldn't reach the assistant. Check your connection and try again.";
    } finally {
      timers.forEach(clearTimeout);
      send.disabled = input.disabled = false;
      input.focus();
      reply.scrollIntoView({ block: "start", behavior: "smooth" });
    }
  }
  form.addEventListener("submit", (e) => { e.preventDefault(); ask(input.value); });
})();
