/* Orangutan Conservation Dashboard — map tab
 *
 * All three species' range boundaries load and show together by default
 * (data/ranges/*.geojson — simplified for web display by
 * scripts/simplify_ranges.py, pongo_pygmaeus's was 40MB of full shapefile
 * precision otherwise, unrenderable). Deforestation + fire alerts are
 * per-species and heavier, so a switcher control picks one species' alerts
 * to load at a time rather than fetching all three simultaneously.
 *
 * Data is fetched over HTTP relative to the repo root, so the site must be
 * served (e.g. `python -m http.server` from the repo root), not opened as a
 * file:// URL. Paths are relative to site/tabs/map.html.
 */

const SPECIES_LIST = [
  { id: "pongo_tapanuliensis", label: "Pongo tapanuliensis (Tapanuli orangutan)", color: "#e8792b", center: [1.62, 99.1], zoom: 9 },
  { id: "pongo_abelii", label: "Pongo abelii (Sumatran orangutan)", color: "#2f9e8f", center: [3.4, 97.4], zoom: 7 },
  { id: "pongo_pygmaeus", label: "Pongo pygmaeus (Bornean orangutan)", color: "#a06cd5", center: [0.8, 113.5], zoom: 6 },
];

function speciesUrls(id) {
  return {
    rangeUrl: `../../data/ranges/${id}.geojson`,
    alertsUrl: `../../data/deforestation/${id}_integrated_alerts.geojson`,
    fireAlertsUrl: `../../data/fire/${id}_fire_alerts.geojson`,
  };
}

let currentSpecies = SPECIES_LIST[0];

const CONFIDENCE_COLORS = { nominal: "#f2d13c", high: "#f2913c", highest: "#e2402c" };

const loadingEl = document.getElementById("loading");
function setLoading(msg, isError = false) {
  if (!msg) { loadingEl.style.display = "none"; return; }
  loadingEl.style.display = "block";
  loadingEl.textContent = msg;
  loadingEl.classList.toggle("error", isError);
}

// ── Base map ──────────────────────────────────────────────────────────────
const osm = L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: "&copy; OpenStreetMap contributors",
});
const esri = L.tileLayer(
  "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
  { maxZoom: 19, attribution: "Tiles &copy; Esri — Source: Esri, Maxar, Earthstar Geographics" }
);

const map = L.map("map", {
  center: [1.5, 106], // rough overview center across Sumatra + Borneo, refined by fitBounds once ranges load
  zoom: 5,
  preferCanvas: true, // canvas rendering keeps ~27k alert points smooth
  layers: [esri],
});

// ── Forest loss overlay (Global Forest Watch raster tile cache) ────────────
// Pre-rendered PNG tiles from GFW's public tile cache — no auth, no backend
// processing needed. Colors are theirs; we don't have a verified legend for
// the exact palette, so link out rather than invent swatches. v1.13's "year"
// band covers 2001-2025 (confirmed via the GFW Data API asset metadata).
const forestLossLayer = L.tileLayer(
  "https://tiles.globalforestwatch.org/umd_tree_cover_loss/v1.13/dynamic/{z}/{x}/{y}.png",
  {
    maxZoom: 19,
    opacity: 0.85,
    attribution:
      'Hansen/UMD/Google/USGS/NASA tree cover loss (2001–2025) — ' +
      '<a href="https://data.globalforestwatch.org/documents/gfw::tree-cover-loss/about" target="_blank" rel="noopener">dataset details</a>',
  }
);

// ── Overlay layer groups ──────────────────────────────────────────────────
function speciesColorForName(sciName) {
  const match = SPECIES_LIST.find((s) => s.label.startsWith(sciName || ""));
  return (match && match.color) || "#e8792b";
}

// IUCN ORIGIN codes: 1 = native, 2 = reintroduced, 3 = introduced. Both
// pongo_abelii and pongo_pygmaeus's range files include non-native patches
// (e.g. abelii has both a reintroduced and a separately-mapped introduced
// population) — distinguish them from native range rather than blending in.
// A faint dashArray-only distinction didn't read at map zoom (especially
// with preferCanvas — fine dash patterns render unreliably there). Use a
// bold, species-color-independent warning color instead so it's unmistakable.
const ORIGIN_STYLE = {
  2: { color: "#ffcc00", dashArray: "10 6" }, // reintroduced: amber, dashed
  3: { color: "#ff4444", dashArray: "2 6" },  // introduced: red, dotted
};

const rangeLayer = L.geoJSON(null, {
  style: (f) => {
    const props = f.properties || {};
    const possible = props.PRESENCE === 3;
    const origin = props.ORIGIN;
    const originStyle = ORIGIN_STYLE[origin];
    const color = originStyle ? originStyle.color : speciesColorForName(props.SCI_NAME);
    return {
      color,
      weight: originStyle ? 3.5 : 2,
      dashArray: originStyle ? originStyle.dashArray : (possible ? "6 5" : null),
      fill: true,
      fillColor: color,
      fillOpacity: originStyle ? 0.25 : 0.08,
    };
  },
  onEachFeature: (f, layer) => {
    const p = f.properties || {};
    const originNote = p.ORIGIN === 2 ? " ⚠ reintroduced population"
      : p.ORIGIN === 3 ? " ⚠ introduced population" : "";
    layer.bindPopup(`<b>${p.SCI_NAME || "Orangutan range"}</b>${originNote}<br>${p.LEGEND || ""}`);
  },
});

const alertGroups = {
  nominal: L.layerGroup(),
  high: L.layerGroup(),
  highest: L.layerGroup(),
};

const fireAlertsLayer = L.layerGroup();

// ── Threats layers (scripts/fetch_threats.py) ───────────────────────────────
// Real, source-attributed spatial data (GFW concession datasets + OSM), not
// scraped/LLM-derived — no review-queue gate needed, unlike the Social tab's
// incident pipeline. Lazy-loaded on first toggle (see THREAT_LAYERS below):
// oil palm + mining concessions are ~2.5-3MB each and most page loads won't
// open them, so fetching eagerly on every visit isn't worth the cost.
const oilPalmLayer = L.geoJSON(null, {
  style: { color: "#c98a2b", weight: 1, fillColor: "#c98a2b", fillOpacity: 0.35 },
  onEachFeature: (f, layer) => {
    const p = f.properties || {};
    layer.bindPopup(
      `<b>Oil palm concession</b><br>${p.conc_name || p.company || "Unnamed"}<br>` +
      `${p.company ? `company: ${p.company}<br>` : ""}` +
      `${p.gfw_area__ha ? `area: ${Math.round(p.gfw_area__ha).toLocaleString()} ha<br>` : ""}` +
      `source: ${p.source || "?"} (${p.source_yr || "?"})`
    );
  },
});
const miningLayer = L.geoJSON(null, {
  style: { color: "#8b3a3a", weight: 1, fillColor: "#8b3a3a", fillOpacity: 0.35 },
  onEachFeature: (f, layer) => {
    const p = f.properties || {};
    layer.bindPopup(
      `<b>Mining concession</b><br>${p.conc_name || "Unnamed"}<br>` +
      `${p.mineral ? `mineral: ${p.mineral}<br>` : ""}` +
      `${p.gfw_area__ha ? `area: ${Math.round(p.gfw_area__ha).toLocaleString()} ha<br>` : ""}` +
      `source: ${p.source || "?"} (${p.source_yr || "?"})`
    );
  },
});
const hydroLayer = L.geoJSON(null, {
  pointToLayer: (f, latlng) => L.circleMarker(latlng, {
    radius: 6, weight: 2, color: "#3ba3d1", fillColor: "#3ba3d1", fillOpacity: 0.5,
  }),
  onEachFeature: (f, layer) => {
    const p = f.properties || {};
    layer.bindPopup(
      `<b>Hydroelectric plant</b><br>${p.name}<br>` +
      `${p.operator ? `operator: ${p.operator}<br>` : ""}` +
      `${p.output_mw ? `output: ${p.output_mw} MW<br>` : ""}` +
      `<span class='muted'>source: ${p.data_source || "OpenStreetMap"}</span>`
    );
  },
});

const THREAT_LAYERS = [
  { layer: oilPalmLayer, url: "../../data/threats/oil_palm.geojson", loaded: false },
  { layer: miningLayer, url: "../../data/threats/mining_concessions.geojson", loaded: false },
  { layer: hydroLayer, url: "../../data/threats/hydro_plants.geojson", loaded: false },
];

function addAlertPoint(feature) {
  const conf = (feature.properties && feature.properties.confidence) || "nominal";
  const group = alertGroups[conf] || alertGroups.nominal;
  const [lon, lat] = feature.geometry.coordinates;
  L.circleMarker([lat, lon], {
    radius: 3,
    stroke: false,
    fillColor: CONFIDENCE_COLORS[conf] || "#f2d13c",
    fillOpacity: 0.75,
  })
    .bindPopup(
      `<b>Alert</b><br>date: ${feature.properties.date}<br>` +
      `confidence: ${conf}<br>intensity: ${feature.properties.intensity ?? "—"}`
    )
    .addTo(group);
}

function addFirePoint(feature) {
  const [lon, lat] = feature.geometry.coordinates;
  L.circleMarker([lat, lon], {
    radius: 3,
    stroke: false,
    fillColor: "#ff0000",
    fillOpacity: 0.75,
  })
    .bindPopup(
      `<b>Fire Alert</b><br>date: ${feature.properties.date_acq}<br>` +
      `confidence: ${feature.properties.confidence}<br>brightness: ${feature.properties.brightness ?? "—"}`
    )
    .addTo(fireAlertsLayer);
}

// FIRMS' date_acq is "YYYY-MM-DDTHHMM" (no colon in the time part) — the
// YYYY-MM-DD prefix alone is enough for both display and min/max sorting.
function fireDateOnly(dateAcq) {
  return dateAcq ? dateAcq.slice(0, 10) : null;
}

// ── Info + legend controls ────────────────────────────────────────────────
const infoControl = L.control({ position: "topright" });
infoControl.onAdd = function () {
  this._div = L.DomUtil.create("div", "info-box");
  this._div.innerHTML = "<h3>Deforestation alerts</h3><span class='muted'>loading…</span>";
  return this._div;
};
infoControl.addTo(map);

const speciesControl = L.control({ position: "topright" });
speciesControl.onAdd = function () {
  const div = L.DomUtil.create("div", "info-box species-switcher");
  div.innerHTML =
    `<label for="species-select" style="display:block;margin-bottom:.3rem;color:var(--muted);font-size:.78rem">` +
    `Deforestation &amp; fire alerts for</label>` +
    `<select id="species-select">` +
    SPECIES_LIST.map((s) => `<option value="${s.id}">${s.label}</option>`).join("") +
    `</select>`;
  L.DomEvent.disableClickPropagation(div);
  return div;
};
speciesControl.addTo(map);

function updateInfo(deforestation, fire) {
  infoControl._div.innerHTML =
    `<h3>${currentSpecies.label}</h3>` +
    `<div>GFW integrated deforestation alerts</div>` +
    `<div class='muted'>window: ${deforestation.minDate || "?"} → ${deforestation.maxDate || "?"}</div>` +
    `<div style='margin-top:.2rem'><b>${deforestation.total.toLocaleString()}</b> alert points shown</div>` +
    `<div class='muted'>nominal ${deforestation.nominal.toLocaleString()} · ` +
    `high ${deforestation.high.toLocaleString()} · highest ${deforestation.highest.toLocaleString()}</div>` +
    `<div style='margin-top:.5rem'>NASA FIRMS fire alerts</div>` +
    (fire.total > 0
      ? `<div class='muted'>window: ${fire.minDate || "?"} → ${fire.maxDate || "?"}</div>` +
        `<div style='margin-top:.2rem'><b>${fire.total.toLocaleString()}</b> alert point${fire.total === 1 ? "" : "s"} shown</div>`
      : `<div class='muted'>none loaded — has scripts/fetch_fire.py been run?</div>`);
}

const legend = L.control({ position: "bottomleft" });
legend.onAdd = function () {
  const div = L.DomUtil.create("div", "legend");
  div.innerHTML =
    "<div class='legend-heading'>Species ranges</div>" +
    SPECIES_LIST.map((s) =>
      `<div class='row'><span class='swatch' style='border-top-color:${s.color}'></span>${s.label.split(" (")[0]}</div>`
    ).join("") +
    "<div class='row'><span class='swatch possible'></span>possibly extant (any species)</div>" +
    "<div class='row'><span class='swatch reintroduced'></span>reintroduced population</div>" +
    "<div class='row'><span class='swatch introduced'></span>introduced population</div>" +
    // Alerts are per-species (see the switcher) — this heading updates on
    // switch so the confidence/fire legend doesn't silently read as "only
    // whichever species loaded first."
    "<div class='legend-heading' id='legend-alerts-heading' style='margin-top:.5rem'>" +
    `Alerts — ${currentSpecies.label.split(" (")[0]}</div>` +
    "<div class='row'><span class='dot highest'></span>deforestation, highest confidence</div>" +
    "<div class='row'><span class='dot high'></span>deforestation, high confidence</div>" +
    "<div class='row'><span class='dot nominal'></span>deforestation, nominal confidence</div>" +
    "<div class='row'><span class='dot fire'></span>fire (NASA FIRMS)</div>" +
    "<div class='legend-heading' style='margin-top:.5rem'>Threats (all species, region-wide)</div>" +
    "<div class='row'><span class='swatch oil-palm'></span>oil palm concession</div>" +
    "<div class='row'><span class='swatch mining'></span>mining concession</div>" +
    "<div class='row'><span class='dot hydro'></span>hydroelectric plant</div>";
  return div;
};
legend.addTo(map);

function updateLegendSpecies(species) {
  const el = document.getElementById("legend-alerts-heading");
  if (el) el.textContent = `Alerts — ${species.label.split(" (")[0]}`;
}

// ── Layer control ─────────────────────────────────────────────────────────
L.control.layers(
  { "Satellite (Esri)": esri, "Street (OSM)": osm },
  {
    "Species range": rangeLayer,
    "Alerts — highest": alertGroups.highest,
    "Alerts — high": alertGroups.high,
    "Alerts — nominal": alertGroups.nominal,
    "Fire alerts": fireAlertsLayer,
    "Forest loss 2001–2025 (Hansen/UMD)": forestLossLayer,
    "Oil palm concessions": oilPalmLayer,
    "Mining concessions": miningLayer,
    "Hydroelectric plants": hydroLayer,
  },
  { collapsed: false }
).addTo(map);

// Lazy-load threat layers on first toggle rather than on page load — see
// the comment above THREAT_LAYERS for why.
map.on("overlayadd", async (e) => {
  const entry = THREAT_LAYERS.find((t) => t.layer === e.layer);
  if (!entry || entry.loaded) return;
  entry.loaded = true;
  try {
    const data = await loadJSON(entry.url);
    entry.layer.addData(data);
  } catch (err) {
    console.error(`Could not load ${entry.url}:`, err);
    entry.loaded = false; // allow retry on next toggle
    alert(
      `Could not load this layer: ${err.message}\n` +
      `Has scripts/fetch_threats.py been run?`
    );
  }
});

// ── Load data ─────────────────────────────────────────────────────────────
async function loadJSON(url) {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText} for ${url}`);
  return resp.json();
}

// Deforestation + fire alerts are per-species and much heavier than the
// (now-simplified) range boundaries, so only one species' worth loads at a
// time, swapped by the switcher control — rather than fetching all three
// simultaneously on every page view.
async function loadSpeciesAlerts(species) {
  currentSpecies = species;
  updateLegendSpecies(species);
  const urls = speciesUrls(species.id);

  alertGroups.nominal.clearLayers();
  alertGroups.high.clearLayers();
  alertGroups.highest.clearLayers();
  fireAlertsLayer.clearLayers();

  try {
    setLoading(`Loading deforestation alerts for ${species.label}…`);
    const stats = { total: 0, nominal: 0, high: 0, highest: 0, minDate: null, maxDate: null };
    try {
      const alerts = await loadJSON(urls.alertsUrl);
      for (const f of alerts.features) {
        addAlertPoint(f);
        const conf = (f.properties && f.properties.confidence) || "nominal";
        stats.total++;
        stats[conf] = (stats[conf] || 0) + 1;
        const d = f.properties && f.properties.date;
        if (d) {
          if (!stats.minDate || d < stats.minDate) stats.minDate = d;
          if (!stats.maxDate || d > stats.maxDate) stats.maxDate = d;
        }
      }
    } catch (alertErr) {
      console.warn("Deforestation alerts not loaded:", alertErr.message);
    }

    setLoading(`Loading fire alerts for ${species.label}…`);
    const fireStats = { total: 0, minDate: null, maxDate: null };
    try {
      const fireAlerts = await loadJSON(urls.fireAlertsUrl);
      for (const f of fireAlerts.features) {
        addFirePoint(f);
        fireStats.total++;
        const d = fireDateOnly(f.properties && f.properties.date_acq);
        if (d) {
          if (!fireStats.minDate || d < fireStats.minDate) fireStats.minDate = d;
          if (!fireStats.maxDate || d > fireStats.maxDate) fireStats.maxDate = d;
        }
      }
    } catch (fireErr) {
      // Fire data is a separate, optional layer (scripts/fetch_fire.py may not
      // have been run yet) — don't let its absence break the deforestation display.
      console.warn("Fire alerts not loaded:", fireErr.message);
    }

    // Show all confidence layers by default.
    alertGroups.highest.addTo(map);
    alertGroups.high.addTo(map);
    alertGroups.nominal.addTo(map);

    updateInfo(stats, fireStats);
    setLoading(null);
  } catch (err) {
    console.error(err);
    setLoading(
      "Could not load alert data: " + err.message +
      " — is the site being served over HTTP from the repo root?",
      true
    );
  }
}

document.getElementById("species-select").addEventListener("change", (e) => {
  const species = SPECIES_LIST.find((s) => s.id === e.target.value);
  if (species) loadSpeciesAlerts(species);
});

(async function init() {
  try {
    setLoading("Loading species ranges…");
    // All three ranges load together and stay visible — cheap now that
    // they're simplified (see the file-level comment), and seeing all
    // three at once is the whole point of a multi-species overview map.
    const rangeSets = await Promise.all(
      SPECIES_LIST.map((s) => loadJSON(speciesUrls(s.id).rangeUrl).catch((err) => {
        console.warn(`Range not loaded for ${s.id}:`, err.message);
        return null;
      }))
    );
    for (const range of rangeSets) {
      if (range) rangeLayer.addData(range);
    }
    rangeLayer.addTo(map);
    try {
      map.fitBounds(rangeLayer.getBounds(), { padding: [20, 20] });
    } catch (_) { /* keep the default overview center if bounds are empty */ }

    await loadSpeciesAlerts(currentSpecies);
  } catch (err) {
    console.error(err);
    setLoading(
      "Could not load data: " + err.message +
      " — is the site being served over HTTP from the repo root?",
      true
    );
  }
})();
