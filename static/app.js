"use strict";

// ------------------------------------------------------------ helpers

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

async function api(path, opts = {}) {
  const init = { ...opts, headers: { ...(opts.headers || {}) } };
  if (opts.json !== undefined) {
    init.body = JSON.stringify(opts.json);
    init.headers["Content-Type"] = "application/json";
  }
  const res = await fetch(path, init);
  if (res.status === 401) {
    location.href = "/login";
    throw new Error("Not signed in");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = Array.isArray(data.detail) ? data.detail.map((d) => d.msg).join("; ") : data.detail;
    throw new Error(detail || `Request failed (${res.status})`);
  }
  return data;
}

let toastTimer;
function toast(msg, isError = false) {
  let el = $(".toast");
  if (!el) {
    el = document.createElement("div");
    el.className = "toast";
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.classList.toggle("error", isError);
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.remove(), 4500);
}

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const fmtM = (m) => (m >= 1000 ? `${(m / 1000).toFixed(2)} km` : `${Math.round(m)} m`);
const fmtDate = (t) => new Date(t * 1000).toLocaleString();

function fmtCountdown(sec) {
  if (sec <= 0) return "expired";
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = Math.floor(sec % 60);
  return h ? `${h}h ${String(m).padStart(2, "0")}m` : `${m}m ${String(s).padStart(2, "0")}s`;
}

function haversine(a, b) {
  const R = 6371000, rad = Math.PI / 180;
  const dLat = (b[0] - a[0]) * rad, dLng = (b[1] - a[1]) * rad;
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(a[0] * rad) * Math.cos(b[0] * rad) * Math.sin(dLng / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(h));
}

// ------------------------------------------------------------ state

const state = {
  me: null,
  tracking: null,
  markers: [],
  serverOffset: 0, // server clock minus local clock, seconds
  settings: null,
  pick: null, // "start" | "end" | "marker"
  config: null,
  gpsWatch: null,
  sim: null,
  chatHistory: [],
};

// ------------------------------------------------------------ navigation

function show(view) {
  $$(".view").forEach((v) => v.classList.toggle("hidden", v.id !== `view-${view}`));
  $$(".rail button").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  if (view === "dashboard") setTimeout(() => map.invalidateSize(), 50);
  if (view === "detect") { loadReference(); loadJobs(); }
  if (view === "users") loadUsers();
  if (view === "docs") loadDocs();
}
$$(".rail button").forEach((b) => b.addEventListener("click", () => show(b.dataset.view)));
$$("[data-goto]").forEach((b) => b.addEventListener("click", () => show(b.dataset.goto)));

$("#logout").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST" }).catch(() => {});
  location.href = "/login";
});

// ------------------------------------------------------------ map

const map = L.map("map", { zoomControl: false, attributionControl: true }).setView([30.3753, 69.3451], 6);
L.control.zoom({ position: "topright" }).addTo(map);
$(".leaflet-top.leaflet-right").style.marginTop = "56px";

function setTiles(cfg) {
  if (!cfg.tile_url) {
    toast("No map tiles are installed. Put an .mbtiles file in tiles/ (see README).", true);
    return;
  }
  L.tileLayer(cfg.tile_url, {
    maxZoom: Math.max(20, cfg.tile_max_zoom),
    maxNativeZoom: cfg.tile_max_zoom,
    attribution: cfg.tiles === "online" ? "Imagery © Esri" : "Offline tiles",
  }).addTo(map);
}

const layers = {
  approach: L.layerGroup().addTo(map),
  strip: L.layerGroup().addTo(map),
  vehicle: L.layerGroup().addTo(map),
  objects: L.layerGroup().addTo(map),
  field: L.layerGroup().addTo(map),
};

const vehicleIcon = L.divIcon({ className: "veh-icon", html: '<div class="dot"></div><div class="tag">MDS</div>', iconSize: [38, 38], iconAnchor: [19, 19] });
const startIcon = L.divIcon({ className: "", html: '<div class="pt-icon"></div>', iconSize: [18, 18], iconAnchor: [9, 9] });
const endIcon = L.divIcon({ className: "", html: '<div class="pt-icon end"></div>', iconSize: [18, 18], iconAnchor: [9, 9] });
let centredOnce = false;

$("#route-filter").addEventListener("change", (e) => {
  const v = e.target.value;
  const want = { all: ["approach", "strip"], approach: ["approach"], strip: ["strip"], none: [] }[v];
  for (const k of ["approach", "strip"]) {
    if (want.includes(k)) layers[k].addTo(map); else map.removeLayer(layers[k]);
  }
});

// Search box: "lat, lng" (works offline).
$("#search").addEventListener("submit", (e) => {
  e.preventDefault();
  const p = parseLatLng(...$("#search-q").value.split(","));
  if (typeof p === "string") return toast(`Enter "lat, lng", e.g. 33.6844, 73.0479. ${p}`, true);
  map.setView(p, 17);
});

// Returns [lat, lng] or an error message.
function parseLatLng(latS = "", lngS = "") {
  latS = String(latS).trim(); lngS = String(lngS).trim();
  const num = /^-?\d+(\.\d+)?$/;
  if (!num.test(latS) || !num.test(lngS)) return "Latitude and longitude must be decimal degrees, e.g. 33.684422 and 73.047882";
  const lat = +latS, lng = +lngS;
  if (lat < -90 || lat > 90) return "Latitude must be between -90 and 90";
  if (lng < -180 || lng > 180) return "Longitude must be between -180 and 180";
  return [lat, lng];
}

const fmtLL = (p) => (p ? `${p[0].toFixed(6)}, ${p[1].toFixed(6)}` : "–");

// ------------------------------------------------------------ mission: points

function setPick(mode) {
  state.pick = state.pick === mode ? null : mode;
  const hint = { start: "Click the map to place the START point", end: "Click the map to place the END point", marker: "Click the map to place the marker" }[state.pick];
  $("#map-hint").textContent = hint || "";
  $("#map-hint").classList.toggle("hidden", !hint);
  // While picking, markers, lines and the marked area must not catch the click.
  map.getContainer().classList.toggle("picking", !!state.pick);
}
$("#btn-mark").addEventListener("click", () => openMarkerDialog());
$("#btn-set-start").addEventListener("click", () => openPointDialog("start"));
$("#btn-set-end").addEventListener("click", () => openPointDialog("end"));
$("#btn-params").addEventListener("click", () => openParamsDialog());

map.on("click", async (e) => {
  if (!state.pick) return;
  const mode = state.pick;
  setPick(null);
  const p = [e.latlng.lat, e.latlng.lng];
  if (mode === "marker") return openMarkerDialog(p);
  await savePoint(mode, p[0], p[1]);
});

async function savePoint(which, lat, lng) {
  try {
    renderTracking(await api("/api/tracking/point", { method: "POST", json: { which, lat, lng } }));
    resetNavRoute();
    map.panTo([lat, lng]);
    if (which === "start" && !state.tracking.params) openParamsDialog();
    else if (which === "start" && !state.tracking.end) toast("Start point set. Now set the end point.");
    return true;
  } catch (err) {
    toast(err.message, true);
    return false;
  }
}

const pointDlg = $("#point-dialog");
let pointWhich = "start";

function openPointDialog(which) {
  pointWhich = which;
  $("#pt-title").textContent = which === "start" ? "Start point" : "End point";
  $("#pt-error").classList.add("hidden");
  const cur = state.tracking?.[which];
  $("#pt-lat").value = cur ? cur[0].toFixed(6) : "";
  $("#pt-lng").value = cur ? cur[1].toFixed(6) : "";
  pointDlg.showModal();
}

function showError(id, msg) {
  $(id).textContent = msg;
  $(id).classList.remove("hidden");
}

$("#pt-cancel").addEventListener("click", () => pointDlg.close());
$("#pt-pick").addEventListener("click", () => { pointDlg.close(); setPick(pointWhich); });

$("#pt-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const p = parseLatLng($("#pt-lat").value, $("#pt-lng").value);
  if (typeof p === "string") return showError("#pt-error", p);
  pointDlg.close();
  await savePoint(pointWhich, p[0], p[1]);
});

// ------------------------------------------------------------ markers: add dialog

const markerDlg = $("#marker-dialog");

function openMarkerDialog(p) {
  $("#mk-error").classList.add("hidden");
  if (p) {
    $("#mk-lat").value = p[0].toFixed(6);
    $("#mk-lng").value = p[1].toFixed(6);
  } else {
    $("#mk-lat").value = $("#mk-lng").value = "";
  }
  markerDlg.showModal();
}

$("#mk-cancel").addEventListener("click", () => markerDlg.close());
$("#mk-pick").addEventListener("click", () => { markerDlg.close(); setPick("marker"); });

$("#mk-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const p = parseLatLng($("#mk-lat").value, $("#mk-lng").value);
  if (typeof p === "string") return showError("#mk-error", p);
  const kind = $('input[name="mk-kind"]:checked').value;
  try {
    await api("/api/markers", { method: "POST", json: { lat: p[0], lng: p[1], kind } });
    markerDlg.close();
    toast(`${kind} marker added`);
    loadMarkers();
  } catch (err) { showError("#mk-error", err.message); }
});

// ------------------------------------------------------------ mission: mine parameters

const paramsDlg = $("#params-dialog");
let densityTable = null;

async function openParamsDialog() {
  densityTable = densityTable || await api("/api/density");
  const p = state.tracking?.params;
  $("#pm-type-" + (p?.mine_type || "at")).checked = true;
  $("#pm-gap").innerHTML = densityTable.time_gaps.map((g) => `<option value="${g}">${g} s</option>`).join("");
  if (p) $("#pm-gap").value = String(p.time_gap_s);
  fillDensityOptions(p);
  paramsDlg.showModal();
  calcParams();
}

function fillDensityOptions(p) {
  const mixed = $("#pm-type-mixed").checked;
  const atm = mixed ? densityTable.mixed.map((x) => x.atm) : densityTable.anti_tank;
  $("#pm-atm-label").textContent = "ATM density";
  $("#pm-atm").innerHTML = atm.map((v) => `<option value="${v}">${v}</option>`).join("");
  if (p && p.mine_type === (mixed ? "mixed" : "at")) $("#pm-atm").value = String(p.atm);
  $("#pm-apm-field").classList.toggle("hidden", !mixed);
  fillApm();
}

// The chart pairs each ATM density with exactly one APM density.
function fillApm() {
  const pair = densityTable.mixed.find((x) => String(x.atm) === $("#pm-atm").value);
  $("#pm-apm").innerHTML = pair ? `<option value="${pair.apm}">${pair.apm}</option>` : "";
}

$$('input[name="pm-type"]').forEach((r) => r.addEventListener("change", () => { fillDensityOptions(null); calcParams(); }));
$("#pm-atm").addEventListener("change", () => { fillApm(); calcParams(); });
$("#pm-gap").addEventListener("change", calcParams);
$("#pm-cancel").addEventListener("click", () => paramsDlg.close());

function paramsBody() {
  const mixed = $("#pm-type-mixed").checked;
  return {
    mine_type: mixed ? "mixed" : "at",
    atm: +$("#pm-atm").value,
    apm: mixed ? +$("#pm-apm").value : null,
    time_gap: +$("#pm-gap").value,
  };
}

async function calcParams() {
  const out = $("#pm-result");
  try {
    const r = await api("/api/density/calc", { method: "POST", json: paramsBody() });
    out.innerHTML = paramsHtml(r, true);
  } catch (err) {
    out.innerHTML = `<p class="warn">${esc(err.message)}</p>`;
  }
}

function paramsHtml(r, full) {
  const t = state.tracking;
  const strip = t?.strip_length_m != null
    ? `<div class="kv"><span>Planned strip (start → end)</span><span>${fmtM(t.strip_length_m)}</span></div>` : "";
  const head = `
    <div class="kv"><span>Frontage covered</span><b>${r.frontage_m} m</b></div>
    <div class="kv"><span>Required vehicle speed</span><b>${r.required_speed_kmh} km/h</b></div>
    <div class="kv"><span>Space interval b/w canisters</span><span>${r.interval_m} m</span></div>
    ${strip}`;
  if (!full) return head;
  //const checks = r.checks.map((c) => `<li class="${c.ok ? "" : "warn"}">${c.ok ? "✓" : "⚠"} ${esc(c.name)}: ${esc(c.formula)} = ${c.computed}; chart: ${c.table}</li>`).join("");
  const flags = r.flags.map((f) => `<li class="warn">⚠ ${esc(f)}</li>`).join("");
  return `${head}
    <div class="label" style="margin-top:10px">Taken from the chart</div>
    <ul class="small src-list">${r.rows_used.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>
    
    ${flags ? `<ul class="small src-list">${flags}</ul>` : ""}
    <p class="small muted">${esc(r.source)}. Speeds in km/h.</p>`;
}

$("#pm-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    renderTracking(await api("/api/tracking/params", { method: "PUT", json: paramsBody() }));
    paramsDlg.close();
    if (!state.tracking.end) toast("Parameters saved. Now set the end point.");
  } catch (err) { $("#pm-result").innerHTML = `<p class="warn">${esc(err.message)}</p>`; }
});

$("#btn-reset").addEventListener("click", async () => {
  if (!confirm("Clear the start and end points, parameters, progress and vehicle track?")) return;
  stopSim();
  closeNav();
  renderTracking(await api("/api/tracking/reset", { method: "POST" }));
});

// ------------------------------------------------------------ GPS

let lastSent = 0;
async function sendPosition(lat, lng, extra = {}) {
  try {
    renderTracking(await api("/api/tracking/position", { method: "POST", json: { lat, lng, ...extra } }));
  } catch (err) { toast(err.message, true); }
}

$("#btn-gps").addEventListener("click", () => {
  if (state.gpsWatch !== null) {
    navigator.geolocation.clearWatch(state.gpsWatch);
    state.gpsWatch = null;
    updateGpsUi();
    return;
  }
  if (!("geolocation" in navigator)) return toast("This browser has no geolocation", true);
  if (!window.isSecureContext) return toast("Browsers only share GPS over HTTPS (or on localhost). See the README.", true);
  stopSim();
  state.gpsWatch = navigator.geolocation.watchPosition(
    (pos) => {
      const now = Date.now();
      if (now - lastSent < 2000) return; // at most one fix every 2 s
      lastSent = now;
      const c = pos.coords;
      sendPosition(c.latitude, c.longitude, { accuracy: c.accuracy, heading: c.heading, speed: c.speed });
    },
    (err) => {
      toast(`GPS error: ${err.message}`, true);
      navigator.geolocation.clearWatch(state.gpsWatch);
      state.gpsWatch = null;
      updateGpsUi();
    },
    { enableHighAccuracy: true, maximumAge: 1000, timeout: 20000 },
  );
  updateGpsUi();
});

function updateGpsUi() {
  const on = state.gpsWatch !== null, sim = !!state.sim;
  $("#btn-gps").classList.toggle("on", on);
  $("#btn-gps").textContent = on ? "Stop sharing GPS" : "Use this device's GPS";
  $("#btn-sim").classList.toggle("on", sim);
  $("#btn-sim").textContent = sim ? "Stop simulation" : "Simulate";
  const pill = $("#gps-pill");
  pill.textContent = on ? "GPS live (this device)" : sim ? "Simulated vehicle" : "GPS off";
  pill.classList.toggle("live", on || sim);
}

// Simulation: follows the navigation route to the start point (or a straight
// line from 400 m away), then lays along the strip at the required speed.
const SIM_TICK_S = 0.7;
const SIM_APPROACH_MS = 15; // simulated approach speed, m/s (demo only)

$("#btn-sim").addEventListener("click", async () => {
  if (state.sim) return stopSim();
  const t = state.tracking;
  if (!t?.start || !t?.end) return toast("Set a start and end point first", true);
  if (!t.params) return toast("Choose the mine parameters first", true);
  if (state.gpsWatch !== null) $("#btn-gps").click();
  if (!t.vehicle) {
    const dLat = t.start[0] - t.end[0], dLng = t.start[1] - t.end[1];
    const k = 400 / Math.max(1, haversine(t.start, t.end));
    await sendPosition(t.start[0] + dLat * k + 0.002, t.start[1] + dLng * k, { accuracy: 5 });
  }
  if (!["approach", "laying"].includes(state.tracking.status)) await startMission();
  const from = [state.tracking.vehicle.lat, state.tracking.vehicle.lng];
  const approachPath = nav.route ? nav.route.geometry : [from, t.start];
  const pts = [
    ...resample(approachPath, SIM_APPROACH_MS * SIM_TICK_S).map((p) => [p, SIM_APPROACH_MS]),
    ...resample([t.start, t.end], (t.params.required_speed_kmh / 3.6) * SIM_TICK_S).map((p) => [p, t.params.required_speed_kmh / 3.6]),
  ];
  let i = 0;
  const go = async () => {
    if (!state.sim) return;
    if (i >= pts.length) return stopSim();
    const [[lat, lng], speed] = pts[i++];
    await sendPosition(lat, lng, { accuracy: 5, speed });
    state.sim = setTimeout(go, SIM_TICK_S * 1000);
  };
  state.sim = setTimeout(go, 0);
  updateGpsUi();
});

function resample(path, stepM) {
  const out = [];
  for (let k = 0; k + 1 < path.length; k++) {
    const a = path[k], b = path[k + 1];
    const n = Math.max(1, Math.ceil(haversine(a, b) / stepM));
    for (let i = k === 0 ? 0 : 1; i <= n; i++) out.push([a[0] + ((b[0] - a[0]) * i) / n, a[1] + ((b[1] - a[1]) * i) / n]);
  }
  return out;
}

function stopSim() {
  if (state.sim) clearTimeout(state.sim);
  state.sim = null;
  updateGpsUi();
}

// ------------------------------------------------------------ navigation

const nav = {
  open: false,
  route: null, // approach route from /api/route
  routeTo: null, // start point the route was made for
  cum: [], // cumulative distance at each geometry vertex
  stepAt: [], // distance along the route of each step's manoeuvre
  loading: false,
  offCount: 0,
  lastReroute: 0,
  prev: null, // previous fix, for speed when GPS gives none
  speedKmh: null,
};

async function startMission() {
  renderTracking(await api("/api/tracking/start", { method: "POST" }));
}

$("#btn-start").addEventListener("click", async () => {
  try {
    if (!["approach", "laying"].includes(state.tracking?.status)) await startMission();
    openNav();
  } catch (err) { toast(err.message, true); }
});
$("#nav-close").addEventListener("click", closeNav);

function openNav() {
  nav.open = true;
  $("#view-dashboard").classList.add("nav-open");
  $("#nav-panel").classList.remove("hidden");
  setTimeout(() => map.invalidateSize(), 50);
  updateNav(state.tracking);
}

function closeNav() {
  nav.open = false;
  $("#view-dashboard").classList.remove("nav-open");
  $("#nav-panel").classList.add("hidden");
  setTimeout(() => map.invalidateSize(), 50);
}

function resetNavRoute() {
  nav.route = null;
  nav.routeTo = null;
}

// Local flat projection, metres; fine over a few tens of km.
function toXY(o, p) {
  const k = Math.PI / 180, R = 6371000;
  return [(p[1] - o[1]) * k * R * Math.cos(o[0] * k), (p[0] - o[0]) * k * R];
}

function cumulative(path) {
  const c = [0];
  for (let i = 1; i < path.length; i++) c.push(c[i - 1] + haversine(path[i - 1], path[i]));
  return c;
}

// Where is p along the path? -> { along, off } in metres.
function locate(path, cum, p) {
  let best = { along: 0, off: Infinity };
  for (let i = 0; i + 1 < path.length; i++) {
    const [bx, by] = toXY(path[i], path[i + 1]), [px, py] = toXY(path[i], p);
    const L2 = bx * bx + by * by;
    const t = L2 ? Math.max(0, Math.min(1, (px * bx + py * by) / L2)) : 0;
    const off = Math.hypot(px - t * bx, py - t * by);
    if (off < best.off) best = { along: cum[i] + t * (cum[i + 1] - cum[i]), off };
  }
  return best;
}

async function fetchRoute(from, to) {
  if (nav.loading) return;
  nav.loading = true;
  try {
    const r = await api(`/api/route?from_lat=${from[0]}&from_lng=${from[1]}&to_lat=${to[0]}&to_lng=${to[1]}`);
    nav.route = r;
    nav.routeTo = to.join(",");
    nav.cum = cumulative(r.geometry);
    nav.stepAt = r.steps.map((s) => locate(r.geometry, nav.cum, s.location).along);
    nav.offCount = 0;
    if (nav.open) map.fitBounds([...r.geometry, ...(state.tracking.end ? [state.tracking.end] : [])], { padding: [40, 40] });
    renderTracking(state.tracking);
  } catch (err) {
    toast(`Routing failed: ${err.message}`, true);
  } finally {
    nav.loading = false;
  }
}

function currentSpeedKmh(v) {
  if (v.speed != null && v.speed >= 0) return v.speed * 3.6;
  const p = nav.prev;
  if (p && v.ts > p.ts) return (haversine([p.lat, p.lng], [v.lat, v.lng]) / (v.ts - p.ts)) * 3.6;
  return null;
}

function fmtDuration(s) {
  if (s == null || !Number.isFinite(s)) return "–";
  const m = Math.round(s / 60);
  return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${m % 60} min`;
}

function bearingDeg(a, b) {
  const k = Math.PI / 180;
  const y = Math.sin((b[1] - a[1]) * k) * Math.cos(b[0] * k);
  const x = Math.cos(a[0] * k) * Math.sin(b[0] * k) - Math.sin(a[0] * k) * Math.cos(b[0] * k) * Math.cos((b[1] - a[1]) * k);
  return (Math.atan2(y, x) / k + 360) % 360;
}
const COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];
const compassOf = (d) => COMPASS[Math.round(d / 45) % 8];

function updateNav(t) {
  const v = t?.vehicle;
  if (v && (!nav.prev || nav.prev.ts !== v.ts)) {
    nav.speedKmh = currentSpeedKmh(v);
    nav.prev = { lat: v.lat, lng: v.lng, ts: v.ts };
  }
  if (!nav.open || !t) return;
  const params = t.params;
  const tol = state.settings?.speed_tol_pct ?? 10;
  const el = (id, html) => { $(id).innerHTML = html; };
  $("#nav-phase").textContent = { approach: "To start point", laying: "Laying", complete: "Complete" }[t.status] || t.status;

  if (!v) {
    el("#nav-current", '<div class="nav-instr">Waiting for the vehicle\'s GPS…</div><div class="small muted">Press "Use this device\'s GPS" on the device in the vehicle.</div>');
    el("#nav-stats", "");
    el("#nav-steps", "");
    $("#nav-speed").classList.add("hidden");
    return;
  }
  const here = [v.lat, v.lng];
  if (!map.getBounds().pad(-0.15).contains(here)) map.panTo(here); // keep the vehicle in view
  const stripBrg = bearingDeg(t.start, t.end);
  const layStep = `Lay along the strip: head ${compassOf(stripBrg)} (${stripBrg.toFixed(0)}°) for ${fmtM(t.strip_length_m)}${params ? ` at ${params.required_speed_kmh} km/h` : ""}`;
  let stats = [], current = "", steps = [];

  if (t.status === "approach") {
    if (!nav.route || nav.routeTo !== t.start.join(",")) fetchRoute(here, t.start);
    if (nav.route) {
      const r = nav.route;
      const total = nav.cum[nav.cum.length - 1];
      const pos = locate(r.geometry, nav.cum, here);
      if (pos.off > (state.config?.off_route_m ?? 50)) nav.offCount++; else nav.offCount = 0;
      if (nav.offCount >= 2 && Date.now() - nav.lastReroute > 15000) {
        nav.lastReroute = Date.now();
        toast("Off route – recalculating");
        nav.route = null;
        fetchRoute(here, t.start);
      }
      const remaining = Math.max(0, total - pos.along);
      let next = nav.stepAt.findIndex((d, i) => i > 0 && d > pos.along + 5);
      if (next < 0) next = r.steps.length - 1;
      const toNext = Math.max(0, nav.stepAt[next] - pos.along);
      const eta = r.duration_s != null
        ? [r.duration_s * (remaining / total), "road speeds"]
        : nav.speedKmh > 3 ? [remaining / (nav.speedKmh / 3.6), "current speed"] : [null, ""];
      current = `<div class="nav-dist">${fmtM(toNext)}</div><div class="nav-instr">${esc(r.steps[next].text)}</div>
        ${nav.offCount ? '<div class="warn small">Off route</div>' : ""}`;
      stats = [
        ["To start point", fmtM(remaining)],
        ["ETA to start", `${fmtDuration(eta[0])}${eta[1] ? ` <span class="muted small">(${eta[1]})</span>` : ""}`],
        ["Total remaining", fmtM(remaining + (t.strip_length_m || 0))],
      ];
      steps = r.steps.map((s, i) => ({ text: s.text, dist: s.distance_m, done: i < next, now: i === next }));
      if (r.warning) stats.push(["Routing", `<span class="warn small">${esc(r.warning)}</span>`]);
    } else {
      current = '<div class="nav-instr">Calculating route…</div>';
    }
    steps.push({ text: layStep, dist: t.strip_length_m }, { text: "Arrive at end point", dist: 0 });
  } else if (t.status === "laying" || t.status === "complete") {
    const done = t.status === "complete";
    const remaining = t.strip?.remaining_m ?? 0;
    current = done
      ? '<div class="nav-instr">End point reached. Laying complete.</div>'
      : `<div class="nav-dist">${fmtM(remaining)}</div><div class="nav-instr">${esc(layStep)}</div>
         ${t.strip?.off_route ? `<div class="warn small">${fmtM(t.strip.cross_track_m)} off the centreline</div>` : ""}`;
    stats = [["To end point", fmtM(remaining)], ["Strip covered", `${Math.round((t.covered || 0) * 100)}%`]];
    steps = [{ text: "Start point reached", done: true }, { text: layStep, dist: t.strip_length_m, now: !done, done }, { text: "Arrive at end point", now: done }];
  } else {
    current = '<div class="nav-instr">Press Start to begin.</div>';
  }

  // Speed box during the laying leg.
  const box = $("#nav-speed");
  if (t.status === "laying" && params) {
    const req = params.required_speed_kmh, cur = nav.speedKmh;
    const lo = req * (1 - tol / 100), hi = req * (1 + tol / 100);
    let cls = "unknown", msg = "No speed reading yet";
    if (cur != null) {
      if (cur < lo) { cls = "bad"; msg = `Too slow – speed up to ${req} km/h`; }
      else if (cur > hi) { cls = "bad"; msg = `Too fast – slow down to ${req} km/h`; }
      else { cls = "ok"; msg = "Speed within tolerance"; }
    }
    box.className = `nav-speed ${cls}`;
    box.innerHTML = `<div><div class="label">Current</div><b>${cur == null ? "–" : cur.toFixed(1)}</b> km/h</div>
      <div><div class="label">Required</div><b>${req}</b> km/h</div>
      <div class="msg">${msg}<div class="small muted">Tolerance ±${tol}% (${lo.toFixed(1)}–${hi.toFixed(1)} km/h)</div></div>`;
  } else {
    box.className = "nav-speed hidden";
  }

  el("#nav-current", current);
  el("#nav-stats", stats.map(([k, val]) => `<div class="kv"><span>${k}</span><span>${val}</span></div>`).join(""));
  el("#nav-steps", steps.map((s) => `<li class="${s.done ? "done" : ""} ${s.now ? "now" : ""}"><span>${esc(s.text)}</span>${s.dist ? `<span class="muted">${fmtM(s.dist)}</span>` : ""}</li>`).join(""));
}

// ------------------------------------------------------------ tracking render

const PHASES = {
  idle: ["idle", "Set a start point, the mine parameters and an end point."],
  planned: ["planned", "Ready. Press Start when the vehicle is ready."],
  approach: ["approach", "Vehicle moving to the start point."],
  laying: ["laying", "Vehicle progressing along the strip."],
  complete: ["complete", "End point reached."],
};

function renderTracking(t) {
  state.tracking = t;
  const { approach, strip } = layers;
  approach.clearLayers();
  strip.clearLayers();
  layers.vehicle.clearLayers();

  if (t.start && t.end) {
    const covered = t.covered || 0;
    const mid = [t.start[0] + (t.end[0] - t.start[0]) * covered, t.start[1] + (t.end[1] - t.start[1]) * covered];
    L.polyline([t.start, t.end], { color: "#4c8dff", weight: 5, opacity: 0.45 }).addTo(strip);
    if (covered > 0) L.polyline([t.start, mid], { color: "#4c8dff", weight: 5 }).addTo(strip);
  }
  if (t.start) L.marker(t.start, { icon: startIcon }).bindTooltip(`Start point · ${fmtLL(t.start)}`).addTo(strip);
  if (t.end) L.marker(t.end, { icon: endIcon }).bindTooltip(`End point · ${fmtLL(t.end)}`).addTo(strip);

  const v = t.vehicle;
  if (v) {
    const here = [v.lat, v.lng];
    if (t.trail.length > 1) L.polyline(t.trail, { color: "#f5a524", weight: 2, opacity: 0.8 }).addTo(layers.vehicle);
    if (v.accuracy) L.circle(here, { radius: v.accuracy, color: "#f5a524", weight: 1, fillOpacity: 0.06 }).addTo(layers.vehicle);
    L.marker(here, { icon: vehicleIcon, zIndexOffset: 1000 }).bindTooltip(`${fmtLL(here)} · ${new Date(v.ts * 1000).toLocaleTimeString()}`).addTo(layers.vehicle);
    if (t.start && (t.status === "approach" || t.status === "planned")) {
      if (nav.route && nav.routeTo === t.start.join(",")) {
        L.polyline(nav.route.geometry, { color: "#1b1c18", weight: 8, opacity: 0.7 }).addTo(approach);
        L.polyline(nav.route.geometry, { color: "#ffffff", weight: 4, dashArray: nav.route.source === "osrm" ? null : "6 8" }).addTo(approach);
      } else {
        L.polyline([here, t.start], { color: "#ffffff", weight: 3, dashArray: "6 8", opacity: 0.9 }).addTo(approach);
      }
    }
    if (!centredOnce) { map.setView(here, 16); centredOnce = true; }
  } else if (t.start && !centredOnce) {
    map.fitBounds([t.start, t.end || t.start], { padding: [60, 60], maxZoom: 17 });
    centredOnce = true;
  }

  const pct = Math.round((t.covered || 0) * 100);
  $("#ring").style.strokeDashoffset = String(314.16 * (1 - (t.covered || 0)));
  $("#ring-pct").textContent = `${pct}%`;
  const [pill, text] = PHASES[t.status] || PHASES.idle;
  $("#phase-pill").textContent = pill;
  $("#phase-text").textContent = text;

  const rows = [];
  if (t.start) rows.push(["Start Point", fmtLL(t.start)]);
  if (t.end) rows.push(["End Point", fmtLL(t.end)]);
  if (t.strip_length_m != null) rows.push(["Strip length", fmtM(t.strip_length_m)]);
  const p = t.params;
  if (p) {
    rows.push(["Mine type", p.mine_type === "mixed" ? `Mixed (ATM ${p.atm} / APM ${p.apm})` : `ATM only (${p.atm})`]);
    rows.push(["Time gap · speed", `${p.time_gap_s} s · ${p.required_speed_kmh} km/h`]);
    rows.push(["Frontage", `${p.frontage_m} m`]);
    if (p.flags.length) rows.push(["Chart check", "⚠ see parameters", true]);
  }
  if (t.approach && t.status === "approach") rows.push(["To start (direct)", fmtM(t.approach.distance_to_start_m)]);
  if (t.strip && (t.status === "laying" || t.status === "complete")) {
    rows.push(["Remaining", fmtM(t.strip.remaining_m)]);
    rows.push(["Off centreline", `${fmtM(t.strip.cross_track_m)}${t.strip.off_route ? " ⚠" : ""}`, t.strip.off_route]);
  }
  if (v) rows.push(["Last fix", `${Math.max(0, Math.round(Date.now() / 1000 + state.serverOffset - v.ts))} s ago`]);
  if (t.ammo) renderAmmo(t.ammo, t.firing);
  $("#track-stats").innerHTML = rows.map(([k, val, warn]) => `<div class="kv"><span>${k}</span><span class="${warn ? "warn" : ""}">${val}</span></div>`).join("");
  updateNav(t);
}

async function loadTracking() {
  try { renderTracking(await api("/api/tracking")); } catch { /* shown on next poll */ }
}

// ------------------------------------------------------------ markers

function nowServer() { return Date.now() / 1000 + state.serverOffset; }

async function loadMarkers() {
  try {
    const data = await api("/api/markers");
    state.serverOffset = data.now - Date.now() / 1000;
    state.markers = data.markers;
    renderMarkers();
  } catch { /* retry on next poll */ }
}

function markerPopup(m) {
  const left = m.expires_at - nowServer();
  return `<b>${esc(m.label)}</b> marker #${m.id}<br>
    <span class="muted">${m.lat.toFixed(6)}, ${m.lng.toFixed(6)}</span><br>
    ${m.score != null ? `Match ${(m.score * 100).toFixed(0)}% · seen in ${m.hits} frame(s)<br>` : ""}
    Source: ${esc(m.source || "–")}<br>
    Marked: ${fmtDate(m.created_at)}<br>
    Self-destruct in: <b data-countdown="${m.expires_at}">${fmtCountdown(left)}</b>
    ${m.crop ? `<img src="/api/crops/${esc(m.crop)}" alt="detection crop">` : ""}
    <div style="margin-top:8px"><button class="btn sm danger" onclick="deleteMarker(${m.id})">Remove</button></div>`;
}

window.deleteMarker = async (id) => {
  await api(`/api/markers/${id}`, { method: "DELETE" }).catch((e) => toast(e.message, true));
  map.closePopup();
  loadMarkers();
};

function renderMarkers() {
  layers.objects.clearLayers();
  layers.field.clearLayers();
  const now = nowServer();
  const live = state.markers.filter((m) => m.expires_at > now);
  for (const m of live) {
    const soon = m.expires_at - now < 3600;
    const kind = m.label === "APM" ? "apm" : "atm";
    const icon = L.divIcon({ className: "", html: `<div class="obj-icon ${kind}${soon ? " soon" : ""}"><span>${m.label === "APM" ? "AP" : "AT"}</span></div>`, iconSize: [30, 30], iconAnchor: [15, 15] });
    L.marker([m.lat, m.lng], { icon }).bindPopup(() => markerPopup(m)).addTo(layers.objects);
  }
  // Perimeter of the marked area: convex hull of the objects.
  if (live.length >= 3) {
    const hull = convexHull(live.map((m) => [m.lat, m.lng]));
    const corners = hull.map((p) => `${p[0].toFixed(6)}, ${p[1].toFixed(6)}`).join("<br>");
    L.polygon(hull, { color: "#e5484d", weight: 2, dashArray: "8 6", fillOpacity: 0.04 })
      .bindPopup(`<b>Marked area perimeter</b><br><span class="muted">Corner coordinates</span><br>${corners}`)
      .addTo(layers.field);
  }
  $("#marker-count").textContent = live.length;
  $("#marker-count-atm").textContent = live.filter((m) => m.label === "ATM").length;
  $("#marker-count-apm").textContent = live.filter((m) => m.label === "APM").length;
  const next = live.reduce((a, m) => Math.min(a, m.expires_at), Infinity);
  $("#next-expiry").textContent = Number.isFinite(next) ? fmtCountdown(next - now) : "–";
  state.liveCount = live.length;
}

function convexHull(points) {
  const pts = [...points].sort((a, b) => a[1] - b[1] || a[0] - b[0]);
  const cross = (o, a, b) => (a[1] - o[1]) * (b[0] - o[0]) - (a[0] - o[0]) * (b[1] - o[1]);
  const lower = [], upper = [];
  for (const p of pts) {
    while (lower.length >= 2 && cross(lower.at(-2), lower.at(-1), p) <= 0) lower.pop();
    lower.push(p);
  }
  for (const p of pts.reverse()) {
    while (upper.length >= 2 && cross(upper.at(-2), upper.at(-1), p) <= 0) upper.pop();
    upper.push(p);
  }
  return lower.slice(0, -1).concat(upper.slice(0, -1));
}

// Tick every second: countdowns, and drop markers whose time is up.
setInterval(() => {
  const now = nowServer();
  $$("[data-countdown]").forEach((el) => { el.textContent = fmtCountdown(+el.dataset.countdown - now); });
  const live = state.markers.filter((m) => m.expires_at > now).length;
  if (live !== state.liveCount) renderMarkers();
  else {
    const next = state.markers.filter((m) => m.expires_at > now).reduce((a, m) => Math.min(a, m.expires_at), Infinity);
    $("#next-expiry").textContent = Number.isFinite(next) ? fmtCountdown(next - now) : "–";
  }
}, 1000);

$("#btn-clear-markers").addEventListener("click", async () => {
  if (!confirm("Remove all marked objects from the map?")) return;
  await api("/api/markers", { method: "DELETE" }).catch((e) => toast(e.message, true));
  loadMarkers();
});

// ------------------------------------------------------------ settings / ammo / SD

function fmtSd(hours) {
  const mins = Math.round(hours * 60);
  if (mins % 60 === 0) return `${mins / 60} hrs`;
  return mins < 60 ? `${mins} mins` : `${Math.floor(mins / 60)} hrs ${mins % 60} mins`;
}

function renderSettings(s) {
  state.settings = s;
  const sel = $("#sd-select");
  const val = String(+s.sd_hours);
  if (![...sel.options].some((o) => o.value === val)) {
    const o = new Option(fmtSd(+s.sd_hours), val);
    sel.insertBefore(o, sel.querySelector('[value="custom"]'));
  }
  sel.value = val;
  sel.dataset.current = val;
  if (document.activeElement?.name !== "pct") $("#tol-form [name=pct]").value = s.speed_tol_pct;
}

async function setSd(hours) {
  const sel = $("#sd-select");
  const apply = $("#sd-apply").checked;
  if (apply && !confirm(`Set the self-destruct time of all marked objects to ${fmtSd(hours)} after they were marked? Objects older than that will be removed.`)) {
    sel.value = sel.dataset.current;
    return false;
  }
  try {
    renderSettings(await api("/api/settings/sd", { method: "PUT", json: { hours, apply_to_existing: apply } }));
    toast(`Self-destruct time set to ${fmtSd(hours)}${apply ? " for all marked objects" : " for new marks"}`);
    loadMarkers();
    return true;
  } catch (err) {
    sel.value = sel.dataset.current;
    toast(err.message, true);
    return false;
  }
}

$("#sd-select").addEventListener("change", (e) => {
  if (e.target.value !== "custom") return setSd(+e.target.value);
  const h = +e.target.dataset.current;
  const whole = Number.isInteger(h);
  $("#sd-value").value = whole ? h : Math.round(h * 60);
  $("#sd-unit").value = whole ? "h" : "m";
  $("#sd-error").classList.add("hidden");
  $("#sd-dialog").showModal();
});

$("#sd-cancel").addEventListener("click", () => {
  $("#sd-dialog").close();
  $("#sd-select").value = $("#sd-select").dataset.current;
});

$("#sd-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const v = +$("#sd-value").value;
  const hours = $("#sd-unit").value === "h" ? v : v / 60;
  if (!(v > 0) || !Number.isInteger(v)) return showError("#sd-error", "Enter a whole number");
  if (hours < 1 / 60 || hours > 272) return showError("#sd-error", "The time must be between 1 minute and 272 hrs");
  $("#sd-dialog").close();
  await setSd(hours);
});

// Ammunition card and dialog.
const TYPES = ["GBL-240", "GBL-243"];

function renderAmmo(a, firing) {
  state.ammo = a;
  $("#ammo-total").textContent = a.total;
  $("#ammo-fired").textContent = a.fired;
  const shells = (n, cls) => Array.from({ length: 6 }, (_, i) => `<i class="${i < n ? cls : ""}"></i>`).join("");
  const frac = (x) => (a.loaded ? Math.round((6 * x) / a.loaded) : 0);
  $("#shells-avail").innerHTML = shells(frac(a.loaded - a.fired), "full");
  $("#shells-fired").innerHTML = shells(frac(a.fired), "spent");
  const row = (label, key) => `<tr><td>${label}</td>${TYPES.map((t) => `<td>${a.types[t][key]}</td>`).join("")}</tr>`;
  $("#ammo-rows").innerHTML = row("Total", "total") + row("Loaded", "loaded") + row("Fired", "fired") + row("Remaining loaded", "remaining");
  let note = "";
  if (firing?.active) note = `Firing ${firing.canister}: one every ${state.tracking?.params?.time_gap_s} s`;
  else if (firing?.stopped) note = `<span class="warn">${esc(firing.stopped)}</span>`;
  $("#ammo-firing").innerHTML = note;
}

$("#ammo-edit").addEventListener("click", async () => {
  const a = await api("/api/ammo");
  for (const t of TYPES) {
    $(`#am-total-${t.slice(4)}`).value = a.types[t].total;
    $(`#am-loaded-${t.slice(4)}`).value = a.types[t].loaded;
  }
  $("#am-fired").textContent = `Fired so far: GBL-240 ${a.types["GBL-240"].fired}, GBL-243 ${a.types["GBL-243"].fired}`;
  $("#am-reset").checked = false;
  $("#am-error").classList.add("hidden");
  $("#ammo-dialog").showModal();
});

$("#am-cancel").addEventListener("click", () => $("#ammo-dialog").close());

$("#am-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const total = {}, loaded = {};
  for (const t of TYPES) {
    total[t] = +$(`#am-total-${t.slice(4)}`).value;
    loaded[t] = +$(`#am-loaded-${t.slice(4)}`).value;
    if (!Number.isInteger(total[t]) || !Number.isInteger(loaded[t]) || total[t] < 0 || loaded[t] < 0) {
      return showError("#am-error", "Enter whole numbers, 0 or more");
    }
  }
  try {
    renderAmmo(await api("/api/ammo", { method: "PUT", json: { total, loaded, reset_fired: $("#am-reset").checked } }), state.tracking?.firing);
    $("#ammo-dialog").close();
    toast("Ammunition updated");
  } catch (err) { showError("#am-error", err.message); }
});

// ------------------------------------------------------------ detection

$("#detect-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const form = e.target;
  const fd = new FormData(form);
  for (const [k, v] of [...fd.entries()]) if (v === "" || (v instanceof File && !v.name)) fd.delete(k);
  const bar = $("#upload-bar");
  bar.classList.remove("hidden");
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/detections");
  xhr.upload.onprogress = (ev) => { if (ev.lengthComputable) bar.firstElementChild.style.width = `${(100 * ev.loaded) / ev.total}%`; };
  xhr.onload = () => {
    bar.classList.add("hidden");
    const data = JSON.parse(xhr.responseText || "{}");
    if (xhr.status >= 400) return toast(data.detail || "Upload failed", true);
    toast("Uploaded. Detection is running…");
    form.reset();
    watchJobs();
  };
  xhr.onerror = () => { bar.classList.add("hidden"); toast("Upload failed", true); };
  xhr.send(fd);
});

let jobsTimer = null;
async function loadJobs() {
  const jobs = await api("/api/jobs").catch(() => []);
  const cls = { done: "ok", error: "bad", running: "run", queued: "run" };
  $("#jobs").innerHTML = jobs.map((j) => `<tr>
    <td>${esc(j.filename)}</td>
    <td><span class="badge ${cls[j.status] || ""}">${esc(j.status)}</span></td>
    <td style="min-width:90px"><div class="bar"><i style="width:${Math.round(j.progress * 100)}%"></i></div></td>
    <td>${j.detections}</td><td>${j.markers}</td><td class="small muted">${esc(j.message || "")}</td></tr>`).join("")
    || '<tr><td colspan="6" class="muted">No videos processed yet.</td></tr>';
  return jobs;
}

function watchJobs() {
  clearTimeout(jobsTimer);
  const tick = async () => {
    const jobs = await loadJobs();
    if (jobs.some((j) => j.status === "running" || j.status === "queued")) jobsTimer = setTimeout(tick, 1500);
    else { loadMarkers(); jobsTimer = null; }
  };
  tick();
}

async function loadReference() {
  const names = await api("/api/reference").catch(() => []);
  const admin = state.me?.role === "admin";
  $("#ref-gallery").innerHTML = names.map((n) => `<figure><img src="/api/reference/${encodeURIComponent(n)}" alt="">
    ${esc(n.length > 18 ? n.slice(0, 16) + "…" : n)}
    ${admin ? `<br><button class="btn sm danger" data-del-ref="${esc(n)}">Delete</button>` : ""}</figure>`).join("")
    || '<p class="warn small">No reference pictures yet. Detection needs at least one.</p>';
  $$("[data-del-ref]").forEach((b) => b.addEventListener("click", async () => {
    await api(`/api/reference/${encodeURIComponent(b.dataset.delRef)}`, { method: "DELETE" }).catch((e) => toast(e.message, true));
    loadReference();
  }));
}

$("#ref-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/reference", { method: "POST", body: new FormData(e.target) });
    e.target.reset();
    loadReference();
  } catch (err) { toast(err.message, true); }
});

// ------------------------------------------------------------ users

async function loadUsers() {
  const users = await api("/api/users").catch((e) => { toast(e.message, true); return []; });
  $("#users").innerHTML = users.map((u) => `<tr>
    <td>${esc(u.username)}</td><td>${u.role}</td>
    <td><span class="badge ${u.active ? "ok" : "bad"}">${u.active ? "active" : "disabled"}</span></td>
    <td class="small muted">${fmtDate(u.created_at)}</td>
    <td>${u.role === "admin" ? "" : `
      <button class="btn sm" data-act="pw" data-id="${u.id}">Reset password</button>
      <button class="btn sm" data-act="toggle" data-id="${u.id}" data-active="${u.active}">${u.active ? "Disable" : "Enable"}</button>
      <button class="btn sm danger" data-act="del" data-id="${u.id}">Delete</button>`}</td></tr>`).join("");
}

$("#users").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-act]");
  if (!b) return;
  const id = b.dataset.id;
  try {
    if (b.dataset.act === "pw") {
      const pw = prompt("New password (min 8 characters):");
      if (!pw) return;
      await api(`/api/users/${id}`, { method: "PATCH", json: { password: pw } });
      toast("Password reset");
    } else if (b.dataset.act === "toggle") {
      await api(`/api/users/${id}`, { method: "PATCH", json: { active: b.dataset.active !== "1" } });
    } else if (b.dataset.act === "del") {
      if (!confirm("Delete this user?")) return;
      await api(`/api/users/${id}`, { method: "DELETE" });
    }
    loadUsers();
  } catch (err) { toast(err.message, true); }
});

$("#user-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  try {
    await api("/api/users", { method: "POST", json: { username: fd.get("username"), password: fd.get("password") } });
    e.target.reset();
    toast("User added");
    loadUsers();
  } catch (err) { toast(err.message, true); }
});

// ------------------------------------------------------------ documents

async function loadDocs() {
  const docs = await api("/api/docs").catch(() => []);
  const admin = state.me?.role === "admin";
  $("#rag-mode").textContent = state.settings?.rag_mode === "claude"
    ? "Answers are written by Claude from the retrieved excerpts (MDS_RAG_MODE=claude)."
    : "Answers are quoted from the documents on this server; nothing is sent to an external service (MDS_RAG_MODE=local).";
  $("#docs").innerHTML = docs.map((d) => `<tr><td>${esc(d.name)}</td><td>${d.chunks}</td>
    <td>${admin && d.uploaded ? `<button class="btn sm danger" data-del-doc="${esc(d.name)}">Remove</button>` : ""}</td></tr>`).join("");
  $$("[data-del-doc]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("Remove this document from the knowledge base?")) return;
    await api(`/api/docs/${encodeURIComponent(b.dataset.delDoc)}`, { method: "DELETE" }).catch((e) => toast(e.message, true));
    loadDocs();
  }));
}

$("#doc-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  toast("Indexing document…");
  try {
    await api("/api/docs", { method: "POST", body: new FormData(e.target) });
    e.target.reset();
    toast("Document added");
    loadDocs();
  } catch (err) { toast(err.message, true); }
});

// ------------------------------------------------------------ settings page

$("#tol-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    renderSettings(await api("/api/settings/speed-tolerance", { method: "PUT", json: { pct: +new FormData(e.target).get("pct") } }));
    toast("Speed tolerance saved");
  } catch (err) { toast(err.message, true); }
});

$("#pw-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  try {
    await api("/api/me/password", { method: "POST", json: { current: fd.get("current"), new: fd.get("new") } });
    e.target.reset();
    toast("Password changed");
  } catch (err) { toast(err.message, true); }
});

// ------------------------------------------------------------ chat

function addMsg(role, text, sources = []) {
  const el = document.createElement("div");
  el.className = `msg ${role === "user" ? "user" : ""}`;
  const who = role === "user" ? (state.me?.username || "Operator") : "AI";
  const pages = [...new Map(sources.map((s) => [`${s.doc}:${s.page}`, s])).values()].slice(0, 5);
  el.innerHTML = `<div class="who">${esc(who)}</div><div class="bubble">${esc(text)}</div>
    ${pages.length ? `<div class="sources">${pages.map((s) => `<span title="${esc(s.excerpt)}">${esc(s.doc)} p.${s.page}</span>`).join("")}</div>` : ""}`;
  $("#chat-body").appendChild(el);
  $("#chat-body").scrollTop = 1e9;
  return el;
}

$("#chat-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const q = $("#chat-q").value.trim();
  if (!q) return;
  $("#chat-q").value = "";
  addMsg("user", q);
  const thinking = addMsg("ai", "…");
  try {
    const r = await api("/api/chat", { method: "POST", json: { question: q, history: state.chatHistory } });
    thinking.remove();
    addMsg("ai", r.answer, r.sources);
    state.chatHistory.push({ role: "user", content: q }, { role: "assistant", content: r.answer });
    state.chatHistory = state.chatHistory.slice(-8);
  } catch (err) {
    thinking.remove();
    addMsg("ai", `Error: ${err.message}`);
  }
});
if (window.innerWidth < 900) $("#chat").classList.add("min"); // keep the map visible on phones
$("#chat-min").addEventListener("click", () => $("#chat").classList.toggle("min"));
$("#chat-close").addEventListener("click", () => { $("#chat").classList.add("hidden"); $("#chat-open").classList.remove("hidden"); });
$("#chat-open").addEventListener("click", () => { $("#chat").classList.remove("hidden"); $("#chat-open").classList.add("hidden"); });

// ------------------------------------------------------------ boot

(async function boot() {
  state.me = await api("/api/me");
  $("#username").textContent = state.me.username;
  $("#avatar").textContent = state.me.username[0];
  if (state.me.role === "admin") $$(".admin-only").forEach((el) => el.classList.remove("hidden"));
  state.config = await api("/api/config");
  setTiles(state.config);
  renderSettings(await api("/api/settings"));
  await Promise.all([loadTracking(), loadMarkers()]);
  setInterval(loadTracking, 3000);
  setInterval(loadMarkers, 10000);
  setInterval(async () => { try { renderSettings(await api("/api/settings")); } catch { /* next time */ } }, 15000);
  updateGpsUi();
})();
