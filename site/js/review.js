/* Orangutan Conservation Dashboard — local review queue (NOT public)
 *
 * Loads data/social/drafts.json, shows unreviewed LLM-drafted incident
 * records one at a time with editable fields, and POSTs approve/reject
 * decisions to scripts/review_server.py (must be running — the plain
 * `python -m http.server` has no backend to receive these).
 */

const DRAFTS_URL = "../../data/social/drafts.json";

const CATEGORY_LABELS = {
  trade: "Trade / trafficking / seizure",
  translocation_rescue: "Translocation / rescue / rehab",
  killing: "Killing",
  other_not_relevant: "Not relevant",
};

const loadingEl = document.getElementById("loading");
const countEl = document.getElementById("queue-count");
const listEl = document.getElementById("queue-list");

function setLoading(msg, isError = false) {
  if (!msg) { loadingEl.style.display = "none"; return; }
  loadingEl.style.display = "block";
  loadingEl.textContent = msg;
  loadingEl.classList.toggle("error", isError);
}

function escapeHTML(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function renderDraft(d) {
  const wrap = document.createElement("article");
  wrap.className = "lit-card";
  wrap.dataset.id = d.id;
  wrap.innerHTML = `
    <span class="lit-type" style="--type-color:#e8792b">${escapeHTML(d.org)} · ${escapeHTML(d.source_kind)}</span>
    <h3><a href="${escapeHTML(d.url)}" target="_blank" rel="noopener">${escapeHTML(d.title)}</a></h3>
    <div class="review-fields">
      <label>Category
        <select data-field="category">
          ${Object.entries(CATEGORY_LABELS).map(([v, label]) =>
            `<option value="${v}" ${v === d.category ? "selected" : ""}>${label}</option>`
          ).join("")}
        </select>
      </label>
      <label>Summary
        <textarea data-field="summary" rows="2">${escapeHTML(d.summary)}</textarea>
      </label>
      <div class="review-row">
        <label>Date <input data-field="date" value="${escapeHTML(d.date)}" /></label>
        <label>Location <input data-field="location" value="${escapeHTML(d.location)}" /></label>
        <label>Species <input data-field="species_mentioned" value="${escapeHTML(d.species_mentioned)}" /></label>
      </div>
      <div class="lit-meta">Model confidence: ${escapeHTML(d.confidence)}</div>
    </div>
    <div class="review-actions">
      <button class="btn-approve">Approve</button>
      <button class="btn-reject">Reject</button>
      <span class="review-status"></span>
    </div>
  `;

  const statusEl = wrap.querySelector(".review-status");

  async function submit(decision) {
    const edits = {};
    wrap.querySelectorAll("[data-field]").forEach((el) => {
      edits[el.dataset.field] = el.value;
    });
    wrap.querySelectorAll("button").forEach((b) => (b.disabled = true));
    statusEl.textContent = "Saving…";
    try {
      const resp = await fetch("/api/review", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: d.id, decision, edits }),
      });
      if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText}`);
      wrap.remove();
      updateCount(-1);
    } catch (err) {
      statusEl.textContent = "Failed: " + err.message +
        " — is scripts/review_server.py running (not the plain http.server)?";
      wrap.querySelectorAll("button").forEach((b) => (b.disabled = false));
    }
  }

  wrap.querySelector(".btn-approve").addEventListener("click", () => submit("approve"));
  wrap.querySelector(".btn-reject").addEventListener("click", () => submit("reject"));
  return wrap;
}

let remaining = 0;
function updateCount(delta) {
  remaining += delta;
  countEl.textContent = `${remaining} draft${remaining === 1 ? "" : "s"} awaiting review.`;
  countEl.style.display = "block";
  if (remaining <= 0) {
    listEl.innerHTML = `<p class="muted">Nothing left to review — the queue is empty.</p>`;
  }
}

(async function init() {
  try {
    setLoading("Loading review queue…");
    const resp = await fetch(DRAFTS_URL);
    if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText} for ${DRAFTS_URL}`);
    const drafts = await resp.json();
    const pending = drafts.filter((d) => !d.reviewed);

    for (const d of pending) listEl.appendChild(renderDraft(d));
    remaining = pending.length;
    updateCount(0);
    setLoading(null);
  } catch (err) {
    console.error(err);
    setLoading(
      "Could not load the review queue: " + err.message +
      " — has scripts/extract_incidents.py been run yet?",
      true
    );
  }
})();
