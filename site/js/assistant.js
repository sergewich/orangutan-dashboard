/*
 * Literature Assistant — floating chat widget, included on every public page.
 *
 * Talks to the Cloudflare Worker in worker/ (GitHub Pages can't run server
 * code). Until ASSISTANT_ENDPOINT is set the widget doesn't render at all,
 * so this file is safe to ship before the Worker is deployed.
 *
 * Everything the model returns is inserted as text nodes, never innerHTML —
 * the answer is LLM output grounded in third-party abstracts, so treat it as
 * untrusted.
 */
(function () {
  // Set to "" to hide the widget everywhere (e.g. to switch it off quickly).
  const ASSISTANT_ENDPOINT = "https://orangutan-assistant.orangutan-dashboard.workers.dev/api/chat";
  if (!ASSISTANT_ENDPOINT) return;

  const TYPE_LABELS = {
    journal: "Journal article",
    book: "Book",
    historic_newspaper: "Historic newspaper",
    dashboard_data: "Dashboard data",
    thesis: "Thesis",
    report: "Report",
  };
  const history = []; // last few turns, sent back for follow-up questions

  const el = (tag, cls, text) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  };

  const toggle = el("button", "assistant-toggle", "Ask the literature");
  toggle.setAttribute("aria-expanded", "false");
  toggle.setAttribute("aria-controls", "assistant-panel");

  const panel = el("section", "assistant-panel");
  panel.id = "assistant-panel";
  panel.hidden = true;
  panel.setAttribute("aria-label", "Literature Assistant");

  const head = el("div", "assistant-head");
  head.append(el("h2", null, "Literature Assistant"));
  const close = el("button", "assistant-close", "×");
  close.setAttribute("aria-label", "Close");
  head.append(close);

  const note = el("p", "assistant-note",
    "Answers are AI-generated summaries drawn only from indexed papers, reports, theses, " +
    "books, and historic newspapers (full text where openly licensed, otherwise abstracts), " +
    "plus the map's alert data. Check the cited sources before relying on any answer.");

  const log = el("div", "assistant-log");
  log.setAttribute("aria-live", "polite");

  const form = el("form", "assistant-form");
  const input = el("textarea");
  input.rows = 2;
  input.maxLength = 500;
  input.placeholder = "e.g. What threatens the Tapanuli orangutan?";
  input.setAttribute("aria-label", "Your question");
  const send = el("button", null, "Send");
  send.type = "submit";
  form.append(input, send);

  panel.append(head, note, log, form);
  document.body.append(toggle, panel);

  function setOpen(open) {
    panel.hidden = !open;
    toggle.hidden = open;
    toggle.setAttribute("aria-expanded", String(open));
    if (open) input.focus();
  }
  toggle.addEventListener("click", () => setOpen(true));
  close.addEventListener("click", () => { setOpen(false); toggle.focus(); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
  });

  // On the map page, pass whichever species the alert switcher shows.
  function currentSpecies() {
    const sel = document.getElementById("species-select");
    return sel ? sel.value : null;
  }

  function addMessage(role, text) {
    const msg = el("div", `assistant-msg ${role}`);
    msg.append(el("div", "assistant-bubble", text));
    log.append(msg);
    log.scrollTop = log.scrollHeight;
    return msg;
  }

  function renderAnswer(msg, data) {
    const bubble = msg.querySelector(".assistant-bubble");
    bubble.textContent = "";
    const byN = Object.fromEntries((data.sources || []).map((s) => [s.n, s]));
    // Turn [n] markers into links to the cited source.
    data.answer.split(/(\[\d+\])/).forEach((part) => {
      const m = part.match(/^\[(\d+)\]$/);
      const src = m && byN[m[1]];
      if (src) {
        const a = el(src.url ? "a" : "span", "assistant-cite", part);
        if (src.url) {
          a.href = src.url;
          a.target = "_blank";
          a.rel = "noopener";
        }
        a.title = src.title;
        bubble.append(a);
      } else {
        bubble.append(document.createTextNode(part));
      }
    });

    const cited = (data.sources || []).filter((s) => s.cited);
    const shown = cited.length ? cited : data.sources || [];
    if (!shown.length) return;
    const box = el("div", "assistant-sources");
    box.append(el("div", "assistant-sources-head", cited.length ? "Sources" : "Related passages (not cited)"));
    const ol = el("ol");
    shown.forEach((s) => {
      const li = el("li");
      li.value = s.n;
      // Reports and theses from the library may have no public link.
      const a = el(s.url ? "a" : "span", null, s.title);
      if (s.url) {
        a.href = s.url;
        a.target = "_blank";
        a.rel = "noopener";
      }
      li.append(a);
      const meta = [s.authors, s.year, TYPE_LABELS[s.type] || s.type].filter(Boolean).join(" · ");
      if (meta) li.append(el("div", "assistant-src-meta", meta));
      ol.append(li);
    });
    box.append(ol);
    msg.append(box);
    log.scrollTop = log.scrollHeight;
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const question = input.value.trim();
    if (question.length < 3) return;
    input.value = "";
    addMessage("user", question);
    const reply = addMessage("bot", "Searching the literature…");
    reply.classList.add("loading-msg");
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
        reply.querySelector(".assistant-bubble").textContent =
          data.error || "Something went wrong. Please try again.";
        return;
      }
      renderAnswer(reply, data);
      history.push({ role: "user", content: question }, { role: "assistant", content: data.answer });
      history.splice(0, history.length - 4);
    } catch {
      reply.classList.remove("loading-msg");
      reply.classList.add("error");
      reply.querySelector(".assistant-bubble").textContent =
        "Couldn't reach the assistant. Check your connection and try again.";
    } finally {
      send.disabled = input.disabled = false;
      input.focus();
    }
  });
})();
