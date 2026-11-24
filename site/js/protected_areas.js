/* Orangutan Conservation Dashboard — Protected areas tab
 *
 * Reads data/archive/pa_alerts/<YYYY-MM>.json (complete fire + deforestation
 * alert counts per protected area, computed by scripts/compute_pa_alerts.py)
 * and data/archive/manifest.json (which months are available) and renders a
 * sortable-by-click table. See CLAUDE.md's "Historic alert archive &
 * per-protected-area counts" section for why these counts, unlike the Map
 * tab's point layer, are always complete even in high-volume months.
 */

const COMMON_NAME_MAP = {
  pongo_abelii: "Sumatran",
  pongo_pygmaeus: "Bornean",
  pongo_tapanuliensis: "Tapanuli",
};

const loadingEl = document.getElementById("loading");
const layoutEl = document.getElementById("pa-layout");
const monthSelect = document.getElementById("month-select");
const rowsEl = document.getElementById("pa-rows");
const countEl = document.getElementById("pa-count");

function setLoading(msg, isError = false) {
  if (!msg) { loadingEl.style.display = "none"; return; }
  loadingEl.style.display = "block";
  loadingEl.textContent = msg;
  loadingEl.classList.toggle("error", isError);
}

async function loadJSON(url) {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText} for ${url}`);
  return resp.json();
}

function escapeHTML(s) {
  const d = document.createElement("div");
  d.textContent = s ?? "";
  return d.innerHTML;
}

function fmt(n) {
  return n === null || n === undefined ? "—" : n.toLocaleString();
}

let currentSort = { key: "total", dir: "desc" };

function sortedPlaces(places) {
  const key = currentSort.key;
  const dir = currentSort.dir === "asc" ? 1 : -1;
  return [...places].sort((a, b) => {
    const va = key === "total" ? (a.fire_count || 0) + (a.deforestation_count || 0) : a[key];
    const vb = key === "total" ? (b.fire_count || 0) + (b.deforestation_count || 0) : b[key];
    if (va === vb) return 0;
    if (va === null || va === undefined) return 1;
    if (vb === null || vb === undefined) return -1;
    if (typeof va === "string") return va.localeCompare(vb) * dir;
    return (va - vb) * dir;
  });
}

function render(data) {
  const places = data.places || [];
  rowsEl.innerHTML = sortedPlaces(places).map((p) => `
    <tr>
      <td>${escapeHTML(p.name)}</td>
      <td>${COMMON_NAME_MAP[p.species] || escapeHTML(p.species)}</td>
      <td>${escapeHTML((p.designations || []).join(", "))}</td>
      <td>${fmt(p.fire_count)}</td>
      <td>${p.deforestation_count === null
        ? '<span title="Could not be queried this run">—</span>'
        : fmt(p.deforestation_count)}</td>
      <td>${p.deforestation_area_ha === null || p.deforestation_area_ha === undefined
        ? "—" : p.deforestation_area_ha.toLocaleString(undefined, { maximumFractionDigits: 1 })}</td>
    </tr>`).join("");
  countEl.textContent = `${places.length} protected area(s) tracked for ${data.month}`;
}

document.querySelectorAll(".pa-table th").forEach((th, i) => {
  const keys = ["name", "species", null, "fire_count", "deforestation_count", "deforestation_area_ha"];
  const key = i === 0 ? "name" : keys[i];
  if (!key) return;
  th.style.cursor = "pointer";
  th.addEventListener("click", () => {
    if (currentSort.key === key) currentSort.dir = currentSort.dir === "asc" ? "desc" : "asc";
    else currentSort = { key, dir: "desc" };
    if (window.__currentPAData) render(window.__currentPAData);
  });
});

async function loadMonth(month) {
  setLoading(`Loading ${month}…`);
  try {
    const data = await loadJSON(`../../data/archive/pa_alerts/${month}.json`);
    window.__currentPAData = data;
    render(data);
    layoutEl.style.display = "block";
    setLoading(null);
  } catch (err) {
    console.error(err);
    setLoading("Could not load this month's data: " + err.message, true);
  }
}

(async function init() {
  try {
    setLoading("Loading available months…");
    const manifest = await loadJSON("../../data/archive/manifest.json");
    const months = (manifest.pa_alerts || []).slice().sort().reverse();
    if (!months.length) {
      setLoading("No protected-area alert data has been computed yet — check back after the next monthly refresh.", true);
      return;
    }
    monthSelect.innerHTML = months.map((m) => `<option value="${m}">${m}</option>`).join("");
    monthSelect.addEventListener("change", (e) => loadMonth(e.target.value));
    await loadMonth(months[0]);
  } catch (err) {
    console.error(err);
    setLoading("Could not load data: " + err.message +
      " — is the site being served over HTTP from the repo root?", true);
  }
})();
