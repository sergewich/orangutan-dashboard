/* Orangutan Conservation Dashboard — map tab
 *
 * Loads the species range boundary and GFW integrated deforestation alerts
 * (produced by scripts/fetch_deforestation.py) and renders them on a Leaflet
 * map with OSM + Esri satellite basemaps and per-confidence alert toggles.
 *
 * Data is fetched over HTTP relative to the repo root, so the site must be
 * served (e.g. `python -m http.server` from the repo root), not opened as a
 * file:// URL. Paths are relative to site/tabs/map.html.
 */

const SPECIES = {
  id: "pongo_tapanuliensis",
  label: "Pongo tapanuliensis (Tapanuli orangutan)",
  rangeUrl: "../../data/ranges/pongo_tapanuliensis.geojson",
  alertsUrl: "../../data/deforestation/pongo_tapanuliensis_integrated_alerts.geojson",
  fireAlertsUrl: "../../data/fire/pongo_tapanuliensis_fire_alerts.geojson",
  center: [1.62, 99.1],
  zoom: 10,
};

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
  center: SPECIES.center,
  zoom: SPECIES.zoom,
  preferCanvas: true, // canvas rendering keeps ~27k alert points smooth
  layers: [esri],
});

// ── Overlay layer groups ──────────────────────────────────────────────────
const rangeLayer = L.geoJSON(null, {
  style: (f) => {
    const possible = (f.properties && f.properties.PRESENCE) === 3;
    return {
      color: "#e8792b",
      weight: 2,
      dashArray: possible ? "6 5" : null,
      fill: true,
      fillColor: "#e8792b",
      fillOpacity: 0.06,
    };
  },
  onEachFeature: (f, layer) => {
    const p = f.properties || {};
    layer.bindPopup(`<b>${p.SCI_NAME || SPECIES.id}</b><br>${p.LEGEND || ""}`);
  },
});

const alertGroups = {
  nominal: L.layerGroup(),
  high: L.layerGroup(),
  highest: L.layerGroup(),
};

const fireAlertsLayer = L.layerGroup();

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

// ── Info + legend controls ────────────────────────────────────────────────
const infoControl = L.control({ position: "topright" });
infoControl.onAdd = function () {
  this._div = L.DomUtil.create("div", "info-box");
  this._div.innerHTML = "<h3>Deforestation alerts</h3><span class='muted'>loading…</span>";
  return this._div;
};
infoControl.addTo(map);

function updateInfo(stats) {
  infoControl._div.innerHTML =
    `<h3>${SPECIES.label}</h3>` +
    `<div>GFW integrated alerts</div>` +
    `<div class='muted'>window: ${stats.minDate || "?"} → ${stats.maxDate || "?"}</div>` +
    `<div style='margin-top:.35rem'><b>${stats.total.toLocaleString()}</b> alert points shown</div>` +
    `<div class='muted'>nominal ${stats.nominal.toLocaleString()} · ` +
    `high ${stats.high.toLocaleString()} · highest ${stats.highest.toLocaleString()}</div>`;
}

const legend = L.control({ position: "bottomright" });
legend.onAdd = function () {
  const div = L.DomUtil.create("div", "legend");
  div.innerHTML =
    "<div class='row'><span class='dot highest'></span>highest confidence</div>" +
    "<div class='row'><span class='dot high'></span>high confidence</div>" +
    "<div class='row'><span class='dot nominal'></span>nominal confidence</div>" +
    "<div class='row' style='margin-top:.35rem'><span class='swatch'></span>species range</div>" +
    "<div class='row'><span class='swatch possible'></span>possibly extant</div>" +
    "<div class='row'><span class='dot fire'></span>fire alerts</div>";
  return div;
};
legend.addTo(map);

// ── Layer control ─────────────────────────────────────────────────────────
L.control.layers(
  { "Satellite (Esri)": esri, "Street (OSM)": osm },
  {
    "Species range": rangeLayer,
    "Alerts — highest": alertGroups.highest,
    "Alerts — high": alertGroups.high,
    "Alerts — nominal": alertGroups.nominal,
    "Fire alerts": fireAlertsLayer,
  },
  { collapsed: false }
).addTo(map);

// ── Load data ─────────────────────────────────────────────────────────────
async function loadJSON(url) {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText} for ${url}`);
  return resp.json();
}

(async function init() {
  try {
    setLoading("Loading range boundary…");
    const range = await loadJSON(SPECIES.rangeUrl);
    rangeLayer.addData(range).addTo(map);
    try {
      map.fitBounds(rangeLayer.getBounds(), { padding: [20, 20] });
    } catch (_) { /* keep default center if bounds are empty */ }

    setLoading("Loading deforestation alerts…");
    const alerts = await loadJSON(SPECIES.alertsUrl);
    const stats = { total: 0, nominal: 0, high: 0, highest: 0, minDate: null, maxDate: null };
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

    setLoading("Loading fire alerts…");
    try {
      const fireAlerts = await loadJSON(SPECIES.fireAlertsUrl);
      for (const f of fireAlerts.features) {
        addFirePoint(f);
      }
    } catch (fireErr) {
      // Fire data is a separate, optional layer (scripts/fetch_fire.py may not
      // have been run yet) — don't let its absence break range/deforestation display.
      console.warn("Fire alerts not loaded:", fireErr.message);
    }

    // Show all confidence layers by default.
    alertGroups.highest.addTo(map);
    alertGroups.high.addTo(map);
    alertGroups.nominal.addTo(map);

    updateInfo(stats);
    setLoading(null);
  } catch (err) {
    console.error(err);
    setLoading(
      "Could not load data: " + err.message +
      " — is the site being served over HTTP from the repo root?",
      true
    );
  }
})();
