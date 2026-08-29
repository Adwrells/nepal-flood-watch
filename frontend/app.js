/* Nepal Flood Watch - dashboard client.
   No framework and no build step: one file, plain DOM, Leaflet for the map.
   Data comes from the FastAPI backend; tiles come from its local cache so the
   console keeps rendering when the network is down. */

const BANDS = ['SEVERE', 'DANGER', 'WARNING', 'WATCH', 'NORMAL'];
const BAND_VAR = { SEVERE: '--severe', DANGER: '--danger', WARNING: '--warning', WATCH: '--watch', NORMAL: '--normal' };
const POLL_MS = 60_000;                       // UI refresh; the backend cycle is slower

const state = {
  stations: [],
  hazards: [],
  events: [],
  selected: null,
  filter: '',
  layers: { gauges: true, impoundment: true, events: true, quakes: true, fires: true },
  theme: localStorage.getItem('theme') || 'dark',
  region: 'NP',
};

const $ = (sel) => document.querySelector(sel);
const cssVar = (name) => getComputedStyle(document.body).getPropertyValue(name).trim();
const bandColor = (band) => cssVar(BAND_VAR[band] || '--normal');

// ---------------------------------------------------------------------------
// Theme. Tile style follows it, so the basemap never fights the UI.
// ---------------------------------------------------------------------------
let tileLayer = null;

function applyTheme(theme) {
  state.theme = theme;
  document.body.dataset.theme = theme;
  localStorage.setItem('theme', theme);
  $('#theme-label').textContent = theme === 'dark' ? 'Dark' : 'Light';
  if (tileLayer) {
    tileLayer.setUrl(`/api/tiles/${theme === 'dark' ? 'dark' : 'light'}/{z}/{x}/{y}.png`);
  }
  render();                                   // marker colours are theme tokens
}

// ---------------------------------------------------------------------------
// Map
// ---------------------------------------------------------------------------
const map = L.map('map', { zoomControl: false, attributionControl: true })
  .setView([28.2, 84.0], 7);
L.control.zoom({ position: 'bottomright' }).addTo(map);

tileLayer = L.tileLayer(`/api/tiles/${state.theme}/{z}/{x}/{y}.png`, {
  minZoom: 5, maxZoom: 12,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors, &copy; <a href="https://carto.com/attributions">CARTO</a>',
}).addTo(map);

// One Leaflet layer group per toggle, so visibility is a single add/remove.
const groups = {
  gauges: L.layerGroup().addTo(map),
  impoundment: L.layerGroup().addTo(map),
  events: L.layerGroup().addTo(map),
  quakes: L.layerGroup().addTo(map),
  fires: L.layerGroup().addTo(map),
};

/* Major-event pins: model alerts, official incidents, and geolocated headlines.
   Teardrop divIcons rather than circles, so an EVENT never reads as a gauge.
   Approximate (district-centroid) pins are drawn hollow and labelled, because
   an operator must not mistake a headline for a surveyed coordinate. */
const EVENT_STYLE = {
  alert:       { glyph: '!', varName: '--danger',  label: 'Model alert' },
  impoundment: { glyph: 'I', varName: '--severe',  label: 'Impoundment watch' },
  incident:    { glyph: 'x', varName: '--warning', label: 'Official incident' },
  news:        { glyph: 'n', varName: '--accent',  label: 'News report' },
};

function eventMarker(e) {
  const style = EVENT_STYLE[e.kind] || EVENT_STYLE.news;
  const color = cssVar(style.varName);
  // Glyph is wrapped so it can be counter-rotated out of the teardrop's tilt.
  const html = `<span class="pin ${e.approximate ? 'approx' : ''}"
      style="--pin:${color}" aria-hidden="true"><b>${style.glyph}</b></span>`;
  const marker = L.marker([e.lat, e.lon], {
    icon: L.divIcon({ html, className: 'pin-wrap', iconSize: [22, 28], iconAnchor: [11, 28] }),
    title: e.title,
  });
  marker.bindPopup(`
    <h3>${esc(e.title)}</h3>
    <dl>
      <dt>Type</dt><dd>${style.label}</dd>
      <dt>Detail</dt><dd>${esc(e.detail || '-')}</dd>
      <dt>When</dt><dd>${fmtDate(e.when)}</dd>
      <dt>Source</dt><dd>${esc(e.source || '-')}</dd>
      ${e.approximate ? '<dt>Location</dt><dd>district centroid, approximate</dd>' : ''}
    </dl>
    ${safeUrl(e.url) ? `<a href="${safeUrl(e.url)}" target="_blank" rel="noopener noreferrer">Open source</a>` : ''}`);
  return marker;
}

/* Marker radius scales with severity so high-FSI gauges dominate visually even
   before colour is read. Ring width doubles as a non-colour severity cue. */
function gaugeMarker(s) {
  const r = 5 + (s.fsi / 100) * 9;
  const m = L.circleMarker([s.lat, s.lon], {
    radius: r,
    fillColor: bandColor(s.band),
    fillOpacity: 0.85,
    color: s.fsi >= 75 ? '#fff' : 'rgba(255,255,255,.45)',
    weight: s.fsi >= 75 ? 2.5 : 1,
  });
  m.bindTooltip(`${s.name} — FSI ${s.fsi} ${s.band}`, { direction: 'top' });
  m.on('click', () => selectStation(s.id));
  return m;
}

function drawMap() {
  Object.values(groups).forEach((g) => g.clearLayers());

  state.stations.forEach((s) => {
    if (s.lat == null) return;
    gaugeMarker(s).addTo(groups.gauges);

    // Impoundment gets a pulsing outer ring: the gauge itself looks calm, so
    // the marker must not.
    if (s.impoundment_suspected) {
      L.circleMarker([s.lat, s.lon], {
        radius: 18, color: cssVar('--severe'), weight: 2, fill: false,
        dashArray: '4 4', className: 'pulse',
      }).bindTooltip(`Possible upstream impoundment — ${s.name}`, { direction: 'top' })
        .addTo(groups.impoundment);
    }
  });

  state.hazards.forEach((h) => {
    if (h.kind === 'earthquake') {
      const trigger = h.extra && h.extra.landslide_trigger;
      L.circleMarker([h.lat, h.lon], {
        radius: 4 + (h.magnitude || 0) * 1.6,
        fillColor: cssVar('--quake'), fillOpacity: 0.5,
        color: trigger ? cssVar('--severe') : cssVar('--quake'),
        weight: trigger ? 2 : 1,
      }).bindTooltip(
        `M${h.magnitude} · ${h.title}${trigger ? ' · landslide-capable' : ''}`,
        { direction: 'top' }
      ).addTo(groups.quakes);
    } else if (h.kind === 'fire') {
      L.circleMarker([h.lat, h.lon], {
        radius: 4, fillColor: cssVar('--fire'), fillOpacity: 0.8, weight: 0,
      }).bindTooltip(`${h.title} · ${h.magnitude ?? '?'} MW`, { direction: 'top' })
        .addTo(groups.fires);
    }
  });

  state.events.forEach((e) => {
    if (e.lat == null) return;
    eventMarker(e).addTo(groups.events);
  });

  syncLayers();
}

function syncLayers() {
  Object.entries(state.layers).forEach(([key, on]) => {
    const g = groups[key];
    if (!g) return;
    if (on && !map.hasLayer(g)) map.addLayer(g);
    if (!on && map.hasLayer(g)) map.removeLayer(g);
  });
}

// ---------------------------------------------------------------------------
// Panels
// ---------------------------------------------------------------------------
function visibleStations() {
  const q = state.filter.toLowerCase();
  return state.stations.filter((s) =>
    !q || `${s.name} ${s.district} ${s.basin}`.toLowerCase().includes(q));
}

function renderList() {
  const rows = visibleStations();
  $('#list').innerHTML = rows.length
    ? rows.map((s) => `
      <button class="row" data-id="${s.id}" aria-current="${state.selected === s.id}"
              style="border-left-color:${bandColor(s.band)}">
        <span class="fsi" style="color:${bandColor(s.band)}">${Math.round(s.fsi)}</span>
        <span>
          <span class="name">${esc(s.name)}</span>
          <span class="meta">${esc(s.district || s.basin || '—')}
            <span class="tag" style="background:${bandColor(s.band)}">${s.band}</span>
            ${s.impoundment_suspected ? '<span class="tag" style="background:var(--severe)">IMPOUND</span>' : ''}
          </span>
        </span>
        <span class="p6">
          <b>${s.p_exceed_6h != null ? Math.round(s.p_exceed_6h * 100) + '%' : '—'}</b>
          ${s.hours_to_danger != null ? s.hours_to_danger + ' h' : '6 h risk'}
        </span>
      </button>`).join('')
    : '<p class="empty">No gauge matches that filter.</p>';

  $('#list').querySelectorAll('.row').forEach((b) =>
    b.addEventListener('click', () => selectStation(Number(b.dataset.id))));
}

function renderKpis(sum) {
  const tiles = [
    ['Gauges', sum.reporting + '/' + sum.total_stations, 'reporting'],
    ['At risk', sum.at_risk, 'FSI 50+'],
    ['Peak FSI', Math.round(sum.max_fsi), 'highest gauge'],
    ['Max P(6h)', Math.round((sum.highest_p6h || 0) * 100) + '%', 'danger breach'],
    ['Impound', sum.impoundment_alerts, 'outburst watch'],
  ];
  $('#kpis').innerHTML = tiles.map(([label, value, sub]) => `
    <div class="kpi"><b>${value}</b><span>${label}</span><em>${sub}</em></div>`).join('');

  const c = sum.last_cycle || {};
  const failed = Object.entries(c.sources || {}).filter(([, v]) => !v.ok).map(([k]) => k);
  $('#updated').textContent = c.finished
    ? `updated ${new Date(c.finished).toLocaleTimeString()} · every ${c.cycle_minutes ?? 12} min` +
      (failed.length ? ` · ${failed.join(', ')} down` : '')
    : 'first cycle running…';
}

function renderLegend() {
  const counts = {};
  state.stations.forEach((s) => { counts[s.band] = (counts[s.band] || 0) + 1; });
  $('#legend').innerHTML = BANDS.map((b) => `
    <div><i style="background:${bandColor(b)}"></i>${b}<span>${counts[b] || 0}</span></div>`).join('');
}

async function renderFeeds() {
  const [incidents, news] = await Promise.all([
    fetchJson('/api/incidents?limit=40'), fetchJson('/api/news?limit=40'),
  ]);
  $('#incidents').innerHTML = incidents.length
    ? incidents.map((i) => `
      <a class="feed-item" href="${safeUrl(i.url)}" target="_blank" rel="noopener noreferrer">
        <div class="t">${esc(i.title)}</div>
        <div class="s">${esc(i.hazard)} · ${fmtDate(i.occurred_on)}</div></a>`).join('')
    : '<p class="empty">No recent incidents.</p>';

  $('#news').innerHTML = news.length
    ? news.map((n) => `
      <a class="feed-item" href="${safeUrl(n.url)}" target="_blank" rel="noopener noreferrer">
        <div class="t">${esc(n.title)}</div>
        <div class="s">${esc(n.source)}${n.districts ? ' · ' + esc(n.districts) : ''}</div></a>`).join('')
    : '<p class="empty">No flood headlines.</p>';
}

/* Detail drawer: descriptive -> diagnostic -> predictive -> prescriptive, in
   that order, because that is the order an operator actually needs them. */
async function selectStation(id) {
  state.selected = id;
  renderList();
  const d = await fetchJson(`/api/station/${id}`);
  const p = d.predictive, fc = p.forecast;

  $('#detail').hidden = false;
  $('#detail').innerHTML = `
    <div class="detail-head">
      <div>
        <h3>${esc(d.name)}</h3>
        <span class="tag" style="background:${bandColor(d.descriptive.band)}">${d.descriptive.band}</span>
        <span class="muted">FSI ${d.descriptive.fsi} · ${d.descriptive.trend}</span>
      </div>
      <button class="btn icon" id="detail-close" aria-label="Close details">&times;</button>
    </div>

    <dl class="facts">
      <dt>Level</dt><dd>${fmt(d.descriptive.level_m)} m</dd>
      <dt>Warning / Danger</dt><dd>${fmt(d.warning_level)} / ${fmt(d.danger_level)} m</dd>
      <dt>Time to danger</dt><dd>${p.hours_to_danger != null ? p.hours_to_danger + ' h' : 'not rising'}</dd>
      <dt>P(danger in 6 h)</dt><dd>${p.p_exceed_6h != null ? Math.round(p.p_exceed_6h * 100) + '%' : '—'}</dd>
      <dt>Percentile</dt><dd>${d.descriptive.percentile_vs_own_history ?? '—'}${d.descriptive.percentile_vs_own_history ? 'th of own history' : ''}</dd>
    </dl>

    <h4>Score drivers</h4>
    ${componentBars(d.diagnostic)}

    <h4>Forecast — next ${fc.horizon_hours.length} h <span class="muted">(${fc.method}, ${fc.confidence} confidence)</span></h4>
    ${sparkline(d.history, fc, d.danger_level, d.warning_level)}
    <p class="muted small">${esc(fc.note)}</p>

    <h4>Recommended actions</h4>
    <ol class="actions">
      ${d.prescriptive.actions.map((a) => `
        <li class="${a.feasible ? '' : 'infeasible'}">
          ${esc(a.action)}
          ${a.note ? `<span class="muted small">${esc(a.note)}</span>` : ''}
        </li>`).join('')}
    </ol>`;

  $('#detail-close').addEventListener('click', () => { $('#detail').hidden = true; state.selected = null; renderList(); });
  const s = state.stations.find((x) => x.id === id);
  if (s) map.setView([s.lat, s.lon], Math.max(map.getZoom(), 9));
}

function componentBars(components) {
  const labels = { level: 'Level vs danger', rise: 'Rate of rise', rain: 'Rainfall', corroboration: 'Reports nearby' };
  return `<div class="bars">${Object.entries(components || {}).map(([k, v]) => `
    <div class="bar-row">
      <span>${labels[k] || k}</span>
      <span class="bar"><i style="width:${Math.max(0, Math.min(100, v))}%"></i></span>
      <b>${Math.round(v)}</b>
    </div>`).join('')}</div>`;
}

/* Inline SVG sparkline: observed history solid, forecast dashed, with the 80%
   band as a shaded envelope and the danger mark as a reference line. */
function sparkline(history, fc, danger, warning) {
  const obs = history.filter((h) => h.level != null).map((h) => h.level);
  if (obs.length < 2 && !fc.values.length) return '<p class="muted small">Not enough history to plot.</p>';

  const all = [...obs, ...fc.values, ...fc.lower, ...fc.upper, danger, warning].filter((v) => v != null);
  const min = Math.min(...all), max = Math.max(...all);
  const pad = (max - min) * 0.1 || 0.5;
  const lo = min - pad, hi = max + pad;
  const W = 300, H = 90;
  const n = obs.length + fc.values.length;
  const x = (i) => (i / Math.max(1, n - 1)) * W;
  const y = (v) => H - ((v - lo) / (hi - lo)) * H;

  const obsPath = obs.map((v, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
  const fcPath = fc.values.map((v, i) => `${i ? 'L' : 'M'}${x(obs.length + i).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
  const band = fc.upper.map((v, i) => `${i ? 'L' : 'M'}${x(obs.length + i).toFixed(1)},${y(v).toFixed(1)}`).join(' ') +
    ' ' + fc.lower.map((v, i) => `L${x(obs.length + fc.lower.length - 1 - i).toFixed(1)},${y(fc.lower[fc.lower.length - 1 - i]).toFixed(1)}`).join(' ') + ' Z';

  const line = (v, color, label) => v == null ? '' :
    `<line x1="0" y1="${y(v).toFixed(1)}" x2="${W}" y2="${y(v).toFixed(1)}" stroke="${color}" stroke-dasharray="3 3" stroke-width="1"/>
     <text x="2" y="${(y(v) - 3).toFixed(1)}" fill="${color}" font-size="9">${label}</text>`;

  return `<svg class="spark" viewBox="0 0 ${W} ${H}" role="img" aria-label="Water level history and forecast">
    ${fc.values.length ? `<path d="${band}" fill="${cssVar('--accent')}" opacity=".15"/>` : ''}
    ${line(danger, cssVar('--danger'), 'danger')}
    ${line(warning, cssVar('--warning'), 'warning')}
    <path d="${obsPath}" fill="none" stroke="${cssVar('--accent')}" stroke-width="2"/>
    <path d="${fcPath}" fill="none" stroke="${cssVar('--accent')}" stroke-width="2" stroke-dasharray="4 3"/>
  </svg>`;
}

// ---------------------------------------------------------------------------
// Data
// ---------------------------------------------------------------------------
async function fetchJson(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url} -> ${r.status}`);
  return r.json();
}

async function refresh() {
  try {
    const [stations, summary, hazards, events] = await Promise.all([
      fetchJson('/api/stations'), fetchJson('/api/summary'),
      fetchJson('/api/hazards?limit=800'), fetchJson('/api/events?limit=150'),
    ]);
    state.stations = stations;
    state.hazards = hazards;
    state.events = events;
    renderKpis(summary);
    render();
    await renderFeeds();
  } catch (err) {
    $('#updated').textContent = 'backend unreachable — retrying';
    console.error(err);
  }
}

function render() { renderList(); renderLegend(); drawMap(); }

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/* Feed URLs are third-party data. Allow only http(s) and escape the result, so
   a javascript: href or an embedded quote in an RSS item cannot break out of
   the attribute. Anything else degrades to a non-link. */
const safeUrl = (u) => {
  try {
    const parsed = new URL(String(u), location.origin);
    return ['http:', 'https:'].includes(parsed.protocol) ? esc(parsed.href) : '';
  } catch { return ''; }
};
const fmt = (v) => v == null ? '—' : Number(v).toFixed(2);
const fmtDate = (s) => s ? new Date(s).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) : '—';

$('#filter').addEventListener('input', (e) => { state.filter = e.target.value; renderList(); });

$('#theme').addEventListener('click', () => applyTheme(state.theme === 'dark' ? 'light' : 'dark'));

document.querySelectorAll('[data-layer]').forEach((el) =>
  el.addEventListener('change', () => {
    state.layers[el.dataset.layer] = el.checked;
    syncLayers();
  }));

$('#refresh').addEventListener('click', async (e) => {
  e.target.setAttribute('aria-busy', 'true');
  e.target.textContent = 'Refreshing…';
  try { await fetch('/api/refresh', { method: 'POST' }); await refresh(); }
  finally { e.target.removeAttribute('aria-busy'); e.target.textContent = 'Refresh now'; }
});

/* Region selector. Nepal is the only enabled region today; the others are
   listed but disabled so the extension point is visible rather than implied. */
async function loadRegions() {
  try {
    const { default: def, regions } = await fetchJson('/api/regions');
    state.region = def;
    $('#region').innerHTML = regions.map((r) => `
      <option value="${esc(r.code)}" ${r.code === def ? 'selected' : ''}
              ${r.enabled ? '' : 'disabled'}>
        ${esc(r.name)}${r.enabled ? '' : ' (not yet available)'}
      </option>`).join('');
    const active = regions.find((r) => r.code === def);
    if (active) map.setView(active.center, active.zoom);
  } catch { /* selector is optional; the map still works */ }
}

$('#region').addEventListener('change', (e) => {
  state.region = e.target.value;
  refresh();
});

applyTheme(state.theme);
loadRegions();
refresh();
setInterval(refresh, POLL_MS);
