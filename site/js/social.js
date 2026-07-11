/* Orangutan Conservation Dashboard — social/incident monitoring tab
 *
 * Loads data/social/incidents.json — human-approved records only, produced by
 * scripts/scrape_social.py -> extract_incidents.py -> review_server.py. Never
 * reads drafts.json (unreviewed records must never reach the public site).
 */

const DATA_URL = "../../data/social/incidents.json";

const CATEGORY_META = {
  trade: { label: "Trade / trafficking / seizure", color: "#e2402c" },
  translocation_rescue: { label: "Translocation / rescue / rehab", color: "#6fbf73" },
  killing: { label: "Killing", color: "#8b8b8b" },
};

const loadingEl = document.getElementById("loading");
const layoutEl = document.getElementById("lit-layout");
const listEl = document.getElementById("lit-list");
const countEl = document.getElementById("lit-count");
const categoryFiltersEl = document.getElementById("category-filters");

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

// Best-effort sort key: prefer the scraper's structured date_hint
// (YYYY-MM-DD), fall back to parsing the LLM-extracted free-text date,
// else sink to the bottom rather than break the sort.
function sortKey(rec) {
  if (rec.date_hint && /^\d{4}-\d{2}-\d{2}/.test(rec.date_hint)) return rec.date_hint;
  const parsed = rec.date ? Date.parse(rec.date) : NaN;
  return Number.isNaN(parsed) ? "0000-00-00" : new Date(parsed).toISOString().slice(0, 10);
}

function buildCheckbox(container, id, label, color) {
  const wrap = document.createElement("label");
  wrap.className = "lit-checkbox";
  wrap.innerHTML = `
    <input type="checkbox" value="${escapeHTML(id)}" checked />
    <span class="lit-swatch" style="--type-color:${color}"></span>
    <span>${escapeHTML(label)}</span>
  `;
  container.appendChild(wrap);
  return wrap.querySelector("input");
}

function renderCard(rec) {
  const meta = CATEGORY_META[rec.category] || { label: rec.category, color: "#888" };
  const bits = [];
  if (rec.date) bits.push(escapeHTML(rec.date));
  if (rec.location) bits.push(escapeHTML(rec.location));
  if (rec.species_mentioned) bits.push(escapeHTML(rec.species_mentioned));
  bits.push(escapeHTML(rec.org));

  return `
    <article class="lit-card">
      <span class="lit-type" style="--type-color:${meta.color}">${meta.label}</span>
      <span class="lit-oa" style="margin-left:0.4rem">human-reviewed</span>
      <h3><a href="${escapeHTML(rec.url)}" target="_blank" rel="noopener">${escapeHTML(rec.title)}</a></h3>
      <p>${escapeHTML(rec.summary)}</p>
      <div class="lit-meta">${bits.join(" · ")}</div>
    </article>
  `;
}

(async function init() {
  try {
    setLoading("Loading incidents…");
    const resp = await fetch(DATA_URL);
    if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText} for ${DATA_URL}`);
    const records = (await resp.json()).filter((r) => r.decision === "approved");
    records.sort((a, b) => (sortKey(b) > sortKey(a) ? 1 : -1));

    const presentCategories = [...new Set(records.map((r) => r.category))];
    const checkboxes = {};
    for (const cat of Object.keys(CATEGORY_META)) {
      if (!presentCategories.includes(cat)) continue;
      checkboxes[cat] = buildCheckbox(categoryFiltersEl, cat, CATEGORY_META[cat].label, CATEGORY_META[cat].color);
    }

    function applyFilters() {
      const active = new Set(Object.entries(checkboxes).filter(([, cb]) => cb.checked).map(([c]) => c));
      const filtered = records.filter((r) => active.has(r.category));
      listEl.innerHTML = filtered.map(renderCard).join("") ||
        `<p class="muted">No records match the current filters.</p>`;
      countEl.textContent = `Showing ${filtered.length.toLocaleString()} of ${records.length.toLocaleString()} reviewed incidents.`;
    }

    for (const cb of Object.values(checkboxes)) cb.addEventListener("change", applyFilters);
    document.getElementById("select-all").addEventListener("click", () => {
      for (const cb of Object.values(checkboxes)) cb.checked = true;
      applyFilters();
    });
    document.getElementById("select-none").addEventListener("click", () => {
      for (const cb of Object.values(checkboxes)) cb.checked = false;
      applyFilters();
    });

    applyFilters();
    setLoading(records.length ? null : "No incidents have been reviewed and approved yet.");
    layoutEl.style.display = records.length ? "grid" : "none";
  } catch (err) {
    console.error(err);
    setLoading(
      "Could not load incident data: " + err.message +
      " — is the site being served over HTTP from the repo root?",
      true
    );
  }
})();
