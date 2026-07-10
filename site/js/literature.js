/* Orangutan Conservation Dashboard — literature tab
 *
 * Loads data/literature/literature.json (produced by scripts/fetch_literature.py
 * from OpenAlex, Open Library, GDELT, and the KB/Delpher SRU API) and renders
 * it as a filterable, most-recent-first list. Data is fetched over HTTP
 * relative to the repo root — see site/js/map.js for the same convention.
 */

const DATA_URL = "../../data/literature/literature.json";

const TYPE_META = {
  journal: { label: "Journal article", color: "#4fa3d1" },
  book: { label: "Book", color: "#b087d6" },
  news: { label: "News & media", color: "#6fbf73" },
  historic_newspaper: { label: "Historic newspaper (Dutch colonial era)", color: "#e8792b" },
};

const loadingEl = document.getElementById("loading");
const layoutEl = document.getElementById("lit-layout");
const listEl = document.getElementById("lit-list");
const countEl = document.getElementById("lit-count");
const typeFiltersEl = document.getElementById("type-filters");
const decadeFiltersEl = document.getElementById("decade-filters");

function setLoading(msg, isError = false) {
  if (!msg) { loadingEl.style.display = "none"; return; }
  loadingEl.style.display = "block";
  loadingEl.textContent = msg;
  loadingEl.classList.toggle("error", isError);
}

function decadeOf(year) {
  if (year == null) return "Undated";
  return `${Math.floor(year / 10) * 10}s`;
}

function decadeSortKey(label) {
  return label === "Undated" ? -Infinity : parseInt(label, 10);
}

function escapeHTML(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function renderCard(rec) {
  const meta = TYPE_META[rec.type] || { label: rec.type, color: "#888" };
  const bits = [];
  if (rec.year != null) bits.push(rec.year);
  if (rec.source) bits.push(escapeHTML(rec.source));
  if (rec.authors) bits.push(escapeHTML(rec.authors));
  if (rec.language && rec.language !== "English") bits.push(rec.language);

  const oaBadge = rec.type === "journal" && rec.open_access
    ? `<span class="lit-oa">open access</span>` : "";

  return `
    <article class="lit-card">
      <span class="lit-type" style="--type-color:${meta.color}">${meta.label}</span>
      <h3><a href="${escapeHTML(rec.url)}" target="_blank" rel="noopener">${escapeHTML(rec.title)}</a></h3>
      <div class="lit-meta">${bits.join(" · ")} ${oaBadge}</div>
    </article>
  `;
}

function buildCheckbox(container, id, label, color) {
  const wrap = document.createElement("label");
  wrap.className = "lit-checkbox";
  wrap.innerHTML = `
    <input type="checkbox" value="${escapeHTML(id)}" checked />
    ${color ? `<span class="lit-swatch" style="--type-color:${color}"></span>` : ""}
    <span>${escapeHTML(label)}</span>
  `;
  container.appendChild(wrap);
  return wrap.querySelector("input");
}

(async function init() {
  try {
    setLoading("Loading literature…");
    const resp = await fetch(DATA_URL);
    if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText} for ${DATA_URL}`);
    const records = await resp.json();

    // ── Build type filters (fixed, known set) ──────────────────────────────
    const presentTypes = [...new Set(records.map((r) => r.type))];
    const typeCheckboxes = {};
    for (const t of Object.keys(TYPE_META)) {
      if (!presentTypes.includes(t)) continue;
      typeCheckboxes[t] = buildCheckbox(typeFiltersEl, t, TYPE_META[t].label, TYPE_META[t].color);
    }

    // ── Build decade filters (derived from the data, most recent first) ───
    const decades = [...new Set(records.map((r) => decadeOf(r.year)))]
      .sort((a, b) => decadeSortKey(b) - decadeSortKey(a));
    const decadeCheckboxes = {};
    for (const d of decades) {
      decadeCheckboxes[d] = buildCheckbox(decadeFiltersEl, d, d);
    }

    function applyFilters() {
      const activeTypes = new Set(
        Object.entries(typeCheckboxes).filter(([, cb]) => cb.checked).map(([t]) => t)
      );
      const activeDecades = new Set(
        Object.entries(decadeCheckboxes).filter(([, cb]) => cb.checked).map(([d]) => d)
      );
      const filtered = records.filter(
        (r) => activeTypes.has(r.type) && activeDecades.has(decadeOf(r.year))
      );
      listEl.innerHTML = filtered.map(renderCard).join("") ||
        `<p class="muted">No references match the current filters.</p>`;
      countEl.textContent = `Showing ${filtered.length.toLocaleString()} of ${records.length.toLocaleString()} references.`;
    }

    for (const cb of [...Object.values(typeCheckboxes), ...Object.values(decadeCheckboxes)]) {
      cb.addEventListener("change", applyFilters);
    }
    document.getElementById("select-all").addEventListener("click", () => {
      for (const cb of [...Object.values(typeCheckboxes), ...Object.values(decadeCheckboxes)]) cb.checked = true;
      applyFilters();
    });
    document.getElementById("select-none").addEventListener("click", () => {
      for (const cb of [...Object.values(typeCheckboxes), ...Object.values(decadeCheckboxes)]) cb.checked = false;
      applyFilters();
    });

    applyFilters();
    setLoading(null);
    layoutEl.style.display = "grid";
  } catch (err) {
    console.error(err);
    setLoading(
      "Could not load literature data: " + err.message +
      " — is the site being served over HTTP from the repo root, and has " +
      "scripts/fetch_literature.py been run?",
      true
    );
  }
})();
