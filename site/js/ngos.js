/* Orangutan Conservation Dashboard — NGOs tab (list view)
 *
 * Reads the NGO data from data/ngos/ngos.json and renders them as a filterable list.
 */

const LABEL_MAP = {
  rescue_rehabilitation: "Rescue & rehabilitation",
  habitat_protection: "Habitat protection",
  anti_wildlife_crime: "Anti-wildlife-crime",
  research: "Research",
  advocacy_policy: "Advocacy & policy",
  wildlife_trade_monitoring: "Wildlife trade monitoring",
};

const COMMON_NAME_MAP = {
  "Pongo abelii": "Sumatran orangutan",
  "Pongo pygmaeus": "Bornean orangutan",
  "Pongo tapanuliensis": "Tapanuli orangutan",
};

const loadingEl = document.getElementById("loading");
const layoutEl = document.getElementById("lit-layout");
const listEl = document.getElementById("lit-list");
const countEl = document.getElementById("lit-count");
const regionFiltersEl = document.getElementById("region-filters");
const focusFiltersEl = document.getElementById("focus-filters");

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

function buildCheckbox(container, id, label, color = null) {
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

function renderCard(org) {
  const focusLabels = org.focus.map((f) => escapeHTML(LABEL_MAP[f]));
  const regionSpeciesMeta = [
    ...org.region.map(escapeHTML),
    ...org.species.map((s) => escapeHTML(COMMON_NAME_MAP[s]))
  ].join(" · ");
  return `
    <article class="lit-card">
      ${focusLabels.map((label) => `<span class="lit-type">${label}</span>`).join("")}
      <h3>${escapeHTML(org.name)}${org.acronym !== org.name ? ` (${escapeHTML(org.acronym)})` : ""}</h3>
      <div class="lit-meta">${regionSpeciesMeta}</div>
      <p>${escapeHTML(org.description)}</p>
      <a class="ngo-visit" href="${org.website}" target="_blank" rel="noopener noreferrer">Visit website →</a>
    </article>
  `;
}

(async function init() {
  try {
    setLoading("Loading NGO data…");
    const response = await fetch("../../data/ngos/ngos.json");
    if (!response.ok) throw new Error(`${response.status} ${response.statusText} for ../../data/ngos/ngos.json`);
    const data = await response.json();

    const organizations = data.organizations;
    organizations.sort((a, b) => a.name.localeCompare(b.name));

    const regions = new Set();
    const focuses = new Set();
    organizations.forEach((org) => {
      org.region.forEach((r) => regions.add(r));
      org.focus.forEach((f) => focuses.add(f));
    });

    const regionCheckboxes = {};
    regions.forEach((region) => {
      regionCheckboxes[region] = buildCheckbox(regionFiltersEl, region, region);
    });

    const focusCheckboxes = {};
    focuses.forEach((focus) => {
      focusCheckboxes[focus] = buildCheckbox(focusFiltersEl, focus, LABEL_MAP[focus]);
    });

    function applyFilters() {
      const activeRegions = new Set(Object.entries(regionCheckboxes).filter(([, cb]) => cb.checked).map(([r]) => r));
      const activeFocuses = new Set(Object.entries(focusCheckboxes).filter(([, cb]) => cb.checked).map(([f]) => f));
      const filtered = organizations.filter((org) =>
        org.region.some((r) => activeRegions.has(r)) && org.focus.some((f) => activeFocuses.has(f))
      );
      listEl.innerHTML = filtered.map(renderCard).join("") ||
        `<p class="muted">No records match the current filters.</p>`;
      countEl.textContent = `Showing ${filtered.length.toLocaleString()} of ${organizations.length.toLocaleString()} organizations.`;
    }

    for (const cb of Object.values(regionCheckboxes)) cb.addEventListener("change", applyFilters);
    for (const cb of Object.values(focusCheckboxes)) cb.addEventListener("change", applyFilters);

    document.getElementById("select-all").addEventListener("click", () => {
      for (const cb of [...Object.values(regionCheckboxes), ...Object.values(focusCheckboxes)]) cb.checked = true;
      applyFilters();
    });

    document.getElementById("select-none").addEventListener("click", () => {
      for (const cb of [...Object.values(regionCheckboxes), ...Object.values(focusCheckboxes)]) cb.checked = false;
      applyFilters();
    });

    applyFilters();
    setLoading(null);
    layoutEl.style.display = "grid";
  } catch (err) {
    console.error(err);
    setLoading(
      "Could not load NGO data: " + err.message +
      " — is the site being served over HTTP from the repo root?",
      true
    );
  }
})();
