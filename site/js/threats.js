/* Orangutan Conservation Dashboard — threats tab (list view)
 *
 * Reads the same GeoJSON files the Map tab's Oil palm / Mining / Hydroelectric
 * layers use (data/threats/*.geojson, produced by scripts/fetch_threats.py) and
 * renders them as a filterable list — a textual companion to the spatial view.
 */

const SOURCES = {
  oil_palm: { url: "../../data/threats/oil_palm.geojson", label: "Oil palm concession", color: "#c98a2b" },
  mining: { url: "../../data/threats/mining_concessions.geojson", label: "Mining concession", color: "#8b3a3a" },
  hydro: { url: "../../data/threats/hydro_plants.geojson", label: "Hydroelectric plant", color: "#3ba3d1" },
};
const TOP_N_POLYGONS = 100; // per category, largest by area — full set is on the map

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

function toRecord(category, feature) {
  const p = feature.properties || {};
  if (category === "hydro") {
    return {
      category, title: p.name, area_ha: null,
      meta: [p.operator, p.output_mw ? `${p.output_mw} MW` : null, p.data_source || "OpenStreetMap"].filter(Boolean),
    };
  }
  const area = parseFloat(p.gfw_area__ha);
  return {
    category,
    title: p.conc_name || p.company || "Unnamed",
    area_ha: Number.isFinite(area) ? area : null,
    meta: [
      category === "mining" ? p.mineral : p.company,
      Number.isFinite(area) ? `${Math.round(area).toLocaleString()} ha` : null,
      p.source ? `${p.source}${p.source_yr ? ` (${p.source_yr})` : ""}` : null,
    ].filter(Boolean),
  };
}

function renderCard(rec) {
  const meta = SOURCES[rec.category];
  return `
    <article class="lit-card">
      <span class="lit-type" style="--type-color:${meta.color}">${meta.label}</span>
      <h3>${escapeHTML(rec.title)}</h3>
      <div class="lit-meta">${rec.meta.map(escapeHTML).join(" · ")}</div>
    </article>
  `;
}

(async function init() {
  try {
    setLoading("Loading threats data…");
    const [oilPalm, mining, hydro] = await Promise.all(
      Object.values(SOURCES).map((s) => fetch(s.url).then((r) => {
        if (!r.ok) throw new Error(`${r.status} ${r.statusText} for ${s.url}`);
        return r.json();
      }))
    );

    const oilPalmRecords = oilPalm.features.map((f) => toRecord("oil_palm", f));
    const miningRecords = mining.features.map((f) => toRecord("mining", f));
    const hydroRecords = hydro.features.map((f) => toRecord("hydro", f));

    oilPalmRecords.sort((a, b) => (b.area_ha ?? 0) - (a.area_ha ?? 0));
    miningRecords.sort((a, b) => (b.area_ha ?? 0) - (a.area_ha ?? 0));

    const oilPalmShown = oilPalmRecords.slice(0, TOP_N_POLYGONS);
    const miningShown = miningRecords.slice(0, TOP_N_POLYGONS);
    const records = [...oilPalmShown, ...miningShown, ...hydroRecords];

    const droppedNote = document.createElement("p");
    droppedNote.className = "muted";
    droppedNote.style.fontSize = "0.78rem";
    droppedNote.textContent =
      `Showing the ${TOP_N_POLYGONS} largest oil palm concessions (of ${oilPalmRecords.length.toLocaleString()} total) ` +
      `and the ${TOP_N_POLYGONS} largest mining concessions (of ${miningRecords.length.toLocaleString()} total), ` +
      `plus all ${hydroRecords.length} hydroelectric plants — the full set is on the Map tab.`;
    document.querySelector(".lit-intro").appendChild(droppedNote);

    const checkboxes = {};
    for (const [key, meta] of Object.entries(SOURCES)) {
      checkboxes[key] = buildCheckbox(categoryFiltersEl, key, meta.label, meta.color);
    }

    function applyFilters() {
      const active = new Set(Object.entries(checkboxes).filter(([, cb]) => cb.checked).map(([c]) => c));
      const filtered = records.filter((r) => active.has(r.category));
      listEl.innerHTML = filtered.map(renderCard).join("") ||
        `<p class="muted">No records match the current filters.</p>`;
      countEl.textContent = `Showing ${filtered.length.toLocaleString()} of ${records.length.toLocaleString()} listed records.`;
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
    setLoading(null);
    layoutEl.style.display = "grid";
  } catch (err) {
    console.error(err);
    setLoading(
      "Could not load threats data: " + err.message +
      " — is the site being served over HTTP from the repo root, and has " +
      "scripts/fetch_threats.py been run?",
      true
    );
  }
})();
