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
  pendingStart: null,
  pendingEnd: null,
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
L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", {
  maxZoom: 20, maxNativeZoom: 19, attribution: "Imagery © Esri",
}).addTo(map);
L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}", {
  maxZoom: 20, maxNativeZoom: 19, opacity: 0.8,
}).addTo(map);
$(".leaflet-top.leaflet-right").style.marginTop = "56px";

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

$("#search").addEventListener("submit", async (e) => {
  e.preventDefault();
  const q = $("#search-q").value.trim();
  if (!q) return;
  const m = q.match(/^\s*(-?\d+(?:\.\d+)?)\s*[, ]\s*(-?\d+(?:\.\d+)?)\s*$/);
  if (m) { map.setView([+m[1], +m[2]], 17); return; }
  try {
    const res = await fetch(`https://nominatim.openstreetmap.org/search?format=json&limit=1&q=${encodeURIComponent(q)}`);
    const [hit] = await res.json();
    if (!hit) return toast("Place not found", true);
    map.setView([+hit.lat, +hit.lon], 15);
  } catch {
    toast("Place search is unavailable; enter coordinates as lat, lng", true);
  }
});

// ------------------------------------------------------------ mission

function setPick(mode) {
  state.pick = state.pick === mode ? null : mode;
  $("#btn-set-start").classList.toggle("on", state.pick === "start");
  $("#btn-set-end").classList.toggle("on", state.pick === "end");
  $("#btn-mark").classList.toggle("on", state.pick === "marker");
  const hint = { start: "Click the map to place the START point", end: "Click the map to place the END point", marker: "Click the map to mark an object" }[state.pick];
  $("#map-hint").textContent = hint || "";
  $("#map-hint").classList.toggle("hidden", !hint);
  map.getContainer().style.cursor = state.pick ? "crosshair" : "";
}
$("#btn-set-start").addEventListener("click", () => setPick("start"));
$("#btn-set-end").addEventListener("click", () => setPick("end"));
$("#btn-mark").addEventListener("click", () => setPick("marker"));

map.on("click", async (e) => {
  const p = [e.latlng.lat, e.latlng.lng];
  if (!state.pick) return;
  const mode = state.pick;
  setPick(null);
  if (mode === "marker") {
    await api("/api/markers", { method: "POST", json: { lat: p[0], lng: p[1], label: "object" } }).catch((err) => toast(err.message, true));
    return loadMarkers();
  }
  const t = state.tracking || {};
  state.pendingStart = mode === "start" ? p : state.pendingStart || t.start;
  state.pendingEnd = mode === "end" ? p : state.pendingEnd || t.end;
  if (state.pendingStart && state.pendingEnd) {
    try {
      renderTracking(await api("/api/tracking/route", { method: "POST", json: { start: state.pendingStart, end: state.pendingEnd } }));
      state.pendingStart = state.pendingEnd = null;
      toast("Route saved. Press Start mission when the vehicle is ready.");
    } catch (err) { toast(err.message, true); }
  } else {
    drawPending();
    setPick(mode === "start" ? "end" : "start");
  }
});

function drawPending() {
  layers.strip.clearLayers();
  if (state.pendingStart) L.marker(state.pendingStart, { icon: startIcon }).addTo(layers.strip);
  if (state.pendingEnd) L.marker(state.pendingEnd, { icon: endIcon }).addTo(layers.strip);
}

$("#btn-start").addEventListener("click", async () => {
  try {
    renderTracking(await api("/api/tracking/start", { method: "POST" }));
    if (!state.tracking.vehicle) toast("Mission started. Waiting for the vehicle's GPS…");
  } catch (err) { toast(err.message, true); }
});

$("#btn-reset").addEventListener("click", async () => {
  if (!confirm("Clear the route, mission progress and vehicle track?")) return;
  stopSim();
  state.pendingStart = state.pendingEnd = null;
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

// Simulation: drives from the current vehicle position (or a point 400 m
// before the start) to the start, then along the strip, posting fixes.
$("#btn-sim").addEventListener("click", () => {
  if (state.sim) return stopSim();
  const t = state.tracking;
  if (!t?.start || !t?.end) return toast("Mark a start and end point first", true);
  if (state.gpsWatch !== null) $("#btn-gps").click();
  let from = t.vehicle ? [t.vehicle.lat, t.vehicle.lng] : null;
  if (!from) {
    const dLat = t.start[0] - t.end[0], dLng = t.start[1] - t.end[1];
    const k = 400 / Math.max(1, haversine(t.start, t.end));
    from = [t.start[0] + dLat * k + 0.002, t.start[1] + dLng * k];
  }
  const legs = [[from, t.start], [t.start, t.end]];
  const stepM = 25;
  const pts = [];
  for (const [a, b] of legs) {
    const n = Math.max(1, Math.ceil(haversine(a, b) / stepM));
    for (let i = 0; i <= n; i++) {
      const jitter = () => (Math.random() - 0.5) * 0.00004;
      pts.push([a[0] + ((b[0] - a[0]) * i) / n + jitter(), a[1] + ((b[1] - a[1]) * i) / n + jitter()]);
    }
  }
  let i = 0;
  const go = async () => {
    if (!state.sim) return;
    if (i >= pts.length) return stopSim();
    const [lat, lng] = pts[i++];
    await sendPosition(lat, lng, { accuracy: 5, speed: stepM });
    state.sim = setTimeout(go, 700);
  };
  state.sim = setTimeout(go, 0);
  const begin = t.status === "planned" || t.status === "idle" || t.status === "complete";
  if (begin) api("/api/tracking/start", { method: "POST" }).then(renderTracking).catch(() => {});
  updateGpsUi();
});

function stopSim() {
  if (state.sim) clearTimeout(state.sim);
  state.sim = null;
  updateGpsUi();
}

// ------------------------------------------------------------ tracking render

const PHASES = {
  idle: ["idle", "Mark a start and end point on the map."],
  planned: ["planned", "Route set. Start the mission when the vehicle is ready."],
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
    L.marker(t.start, { icon: startIcon }).bindTooltip("Start").addTo(strip);
    L.marker(t.end, { icon: endIcon }).bindTooltip("End").addTo(strip);
  } else {
    drawPending();
  }

  const v = t.vehicle;
  if (v) {
    const here = [v.lat, v.lng];
    if (t.trail.length > 1) L.polyline(t.trail, { color: "#f5a524", weight: 2, opacity: 0.8 }).addTo(layers.vehicle);
    if (v.accuracy) L.circle(here, { radius: v.accuracy, color: "#f5a524", weight: 1, fillOpacity: 0.06 }).addTo(layers.vehicle);
    L.marker(here, { icon: vehicleIcon, zIndexOffset: 1000 }).bindTooltip(`Updated ${new Date(v.ts * 1000).toLocaleTimeString()}`).addTo(layers.vehicle);
    if (t.start && (t.status === "approach" || t.status === "planned")) {
      L.polyline([here, t.start], { color: "#ffffff", weight: 3, dashArray: "6 8", opacity: 0.9 }).addTo(approach);
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
  if (t.strip_length_m != null) rows.push(["Strip length", fmtM(t.strip_length_m)]);
  if (t.approach && t.status === "approach") {
    rows.push(["To start point", fmtM(t.approach.distance_to_start_m)]);
    if (t.approach.progress != null) rows.push(["Approach", `${Math.round(t.approach.progress * 100)}%`]);
  }
  if (t.strip && (t.status === "laying" || t.status === "complete")) {
    rows.push(["Remaining", fmtM(t.strip.remaining_m)]);
    rows.push(["Off centreline", `${fmtM(t.strip.cross_track_m)}${t.strip.off_route ? " ⚠" : ""}`, t.strip.off_route]);
  }
  if (v) rows.push(["Last fix", `${Math.max(0, Math.round(Date.now() / 1000 + state.serverOffset - v.ts))} s ago`]);
  $("#track-stats").innerHTML = rows.map(([k, val, warn]) => `<div class="kv"><span>${k}</span><span class="${warn ? "warn" : ""}">${val}</span></div>`).join("");
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
  return `<b>${esc(m.label)}</b> #${m.id}<br>
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
    const icon = L.divIcon({ className: "", html: `<div class="obj-icon${soon ? " soon" : ""}"></div>`, iconSize: [30, 30], iconAnchor: [15, 15] });
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

function renderSettings(s) {
  state.settings = s;
  const sel = $("#sd-select");
  const val = String(+s.sd_hours);
  if (![...sel.options].some((o) => o.value === val)) {
    const o = new Option(`${val}h`, val);
    sel.insertBefore(o, sel.querySelector('[value="custom"]'));
  }
  sel.value = val;
  sel.dataset.current = val;

  const total = s.total_canisters, fired = s.fired_canisters;
  $("#ammo-total").textContent = total;
  $("#ammo-fired").textContent = fired;
  const shells = (n, cls) => Array.from({ length: 6 }, (_, i) => `<i class="${i < n ? cls : ""}"></i>`).join("");
  $("#shells-avail").innerHTML = shells(Math.round((6 * (total - fired)) / total), "full");
  $("#shells-fired").innerHTML = shells(Math.round((6 * fired) / total), "spent");
}

$("#sd-select").addEventListener("change", async (e) => {
  const sel = e.target;
  let hours = sel.value;
  if (hours === "custom") {
    hours = prompt(`Self-destruct time in hours (${state.settings.sd_min}–${state.settings.sd_max}):`, sel.dataset.current);
    if (hours === null) { sel.value = sel.dataset.current; return; }
  }
  try {
    const apply = $("#sd-apply").checked;
    if (apply && !confirm(`Set self-destruct time of all marked objects to ${hours}h after they were marked? Objects older than that will be removed.`)) {
      sel.value = sel.dataset.current;
      return;
    }
    renderSettings(await api("/api/settings/sd", { method: "PUT", json: { hours: +hours, apply_to_existing: apply } }));
    toast(`Self-destruct time set to ${hours}h${apply ? " for all marked objects" : " for new detections"}`);
    loadMarkers();
  } catch (err) {
    sel.value = sel.dataset.current;
    toast(err.message, true);
  }
});

$("#ammo-edit").addEventListener("click", async () => {
  const s = state.settings;
  const fired = prompt(`Canisters fired (of ${s.total_canisters}):`, s.fired_canisters);
  if (fired === null) return;
  try { renderSettings(await api("/api/settings/ammo", { method: "PUT", json: { fired: +fired } })); }
  catch (err) { toast(err.message, true); }
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
  renderSettings(await api("/api/settings"));
  await Promise.all([loadTracking(), loadMarkers()]);
  setInterval(loadTracking, 3000);
  setInterval(loadMarkers, 10000);
  setInterval(async () => { try { renderSettings(await api("/api/settings")); } catch { /* next time */ } }, 15000);
  updateGpsUi();
})();
