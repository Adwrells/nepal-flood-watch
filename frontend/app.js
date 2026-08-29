/* Nepal Flood Watch - dashboard client.
   No framework and no build step: one file, plain DOM, Leaflet for the map.
   Data comes from the FastAPI backend; tiles come from its local cache so the
   console keeps rendering when the network is down. */

const BANDS = ['SEVERE', 'DANGER', 'WARNING', 'WATCH', 'NORMAL'];
const BAND_VAR = { SEVERE: '--severe', DANGER: '--danger', WARNING: '--warning', WATCH: '--watch', NORMAL: '--normal' };
const POLL_MS = 60_000;                       // UI refresh; the backend cycle is slower
const NEARBY_RADIUS_KM = 30;   // ground-truth radius for the Explore panel
/* Below this zoom the map is a national severity overview and shows gauges
   only. Event pins would otherwise bury 309 gauge markers under 150 pins at
   exactly the zoom where the severity pattern is the whole point. */
const EVENT_ZOOM = 8;
/* Bump when the tile SOURCE changes. Tiles are cached for a week client-side,
   so without this a viewer keeps the old provider's imagery (which, in the
   CARTO case, was watermarked) long after the server switched. */
const TILE_VERSION = 3;

const state = {
  stations: [],
  hazards: [],
  events: [],
  selected: null,
  filter: '',
  layers: { gauges: true, impoundment: true, events: true, quakes: true, fires: true },
  theme: localStorage.getItem('theme') || 'dark',
  region: 'NP',
  basemap: 'dark',
  imagery: null,          // /api/imagery/options payload
  explore: null,          // the place currently under inspection
  satLayer: 'esri',
  satDate: null,
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
  // Only follow the theme while on a map basemap; satellite is theme-neutral.
  if (state.basemap !== 'esri') setBasemap(theme);
  render();                                   // marker colours are theme tokens
}

/* Main-map basemap. Satellite is a mosaic, not current imagery -- the banner
   in the Explore tab is where that gets said, but the radio label says
   "Satellite" rather than "Live" for the same reason. */
function setBasemap(id) {
  state.basemap = id;
  if (!tileLayer) return;
  if (id === 'esri') {
    tileLayer.setUrl(`/api/satellite/{z}/{x}/{y}.jpg?v=${TILE_VERSION}`);
    tileLayer.options.maxZoom = 19;
  } else {
    tileLayer.setUrl(`/api/tiles/${id}/{z}/{x}/{y}.png?v=${TILE_VERSION}`);
    tileLayer.options.maxZoom = 12;
  }
  const radio = document.querySelector(`input[name="basemap"][value="${id}"]`);
  if (radio) radio.checked = true;
  if (typeof writeHash === 'function') writeHash();
}

// ---------------------------------------------------------------------------
// Map
// ---------------------------------------------------------------------------
const map = L.map('map', { zoomControl: false, attributionControl: true })
  .setView([28.2, 84.0], 7);
L.control.zoom({ position: 'bottomright' }).addTo(map);

tileLayer = L.tileLayer(`/api/tiles/${state.theme}/{z}/{x}/{y}.png?v=${TILE_VERSION}`, {
  minZoom: 5, maxZoom: 12,
  attribution: 'Basemap &copy; <a href="https://www.esri.com">Esri</a>, HERE, Garmin, &copy; OpenStreetMap contributors',
}).addTo(map);

/* Event pins live in their own pane BENEATH Leaflet's overlay pane (z 400),
   because gauges are the primary data. By default divIcon markers land in the
   marker pane (z 600) and bury the gauge circles completely at country zoom. */
map.createPane('eventsPane');
map.getPane('eventsPane').style.zIndex = 390;

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
    pane: 'eventsPane',
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
  marker.on('click', () => exploreAt({ ...e, name: e.title }));
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

  // Overview zoom stays a clean severity map; pins are for inspection.
  if (map.getZoom() >= EVENT_ZOOM) {
    state.events.forEach((e) => {
      if (e.lat == null) return;
      eventMarker(e).addTo(groups.events);
    });
  }

  syncLayers();
}

/* Rebuild only when crossing the overview/inspection threshold, not on every
   zoom step -- redrawing 450 markers per wheel-click is visibly janky. */
let lastZoomBand = null;
map.on('zoomend', () => {
  const band = map.getZoom() >= EVENT_ZOOM ? 'detail' : 'overview';
  if (band !== lastZoomBand) {
    lastZoomBand = band;
    drawMap();
    updateEventHint();
  }
});

/* A layer that silently does nothing reads as a bug, so the toggle says why. */
function updateEventHint() {
  const label = document.querySelector('[data-layer="events"]').closest('label');
  if (!label) return;
  const zoomedOut = map.getZoom() < EVENT_ZOOM;
  label.classList.toggle('gated', zoomedOut);
  let hint = label.querySelector('.gate-hint');
  if (zoomedOut && !hint) {
    hint = document.createElement('span');
    hint.className = 'gate-hint';
    hint.textContent = 'zoom in';
    label.appendChild(hint);
  } else if (!zoomedOut && hint) {
    hint.remove();
  }
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
  writeHash();
  const d = await fetchJson(`/api/station/${id}`);
  const p = d.predictive, fc = p.forecast;

  $('#detail').hidden = false;
  $('#detail').innerHTML = `
    <div class="detail-head">
      <div>
        <h3>${esc(d.name)}</h3>
        <span class="tag" style="background:${bandColor(d.descriptive.band)}">${d.descriptive.band}</span>
        <span class="muted">FSI ${d.descriptive.fsi} · ${
          d.descriptive.trend === 'unknown' ? 'awaiting a second reading' : d.descriptive.trend
        }</span>
      </div>
      <span class="detail-tools">
        <button class="btn" id="detail-explore" type="button">Satellite</button>
        <button class="btn icon" id="detail-close" aria-label="Close details">&times;</button>
      </span>
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

    <h4>Forecast — next ${fc.horizon_hours.length} h</h4>
    ${sparkline(d.history, fc, d.danger_level, d.warning_level)}
    ${sparkKey(d.danger_level != null, d.warning_level != null)}
    ${(() => {
      const v = forecastVerdict(d);
      return `<div class="verdict ${v.tone}">
        <div class="verdict-head"><span class="verdict-icon">${v.icon}</span>
          <b>${esc(v.headline)}</b></div>
        <p>${esc(v.body)}</p>
        ${v.evidence ? `<p class="evidence">Basis: ${esc(v.evidence)} · method ${esc(fc.method)}</p>` : ''}
      </div>`;
    })()}

    <h4>Recommended actions</h4>
    <ol class="actions">
      ${d.prescriptive.actions.map((a) => `
        <li class="${a.feasible ? '' : 'infeasible'}">
          ${esc(a.action)}
          ${a.note ? `<span class="muted small">${esc(a.note)}</span>` : ''}
        </li>`).join('')}
    </ol>`;

  $('#detail-close').addEventListener('click', () => { $('#detail').hidden = true; state.selected = null; renderList(); });
  $('#detail-explore').addEventListener('click', () => {
    const st = state.stations.find((x) => x.id === id);
    if (st) exploreAt(st);
  });
  const s = state.stations.find((x) => x.id === id);
  if (s) map.setView([s.lat, s.lon], Math.max(map.getZoom(), 9));
}


/* ---------------------------------------------------------------------------
   Explaining the forecast.

   A line going up is not a finding. What makes it a finding is the rate, the
   sample it was measured over, and the spread around it, so the chart always
   ships with a key and a plain-language verdict that quotes those numbers. If
   the evidence is thin the verdict says so, rather than dressing up a
   two-point trend as a prediction.
--------------------------------------------------------------------------- */
const ICON = {
  observed: '<svg viewBox="0 0 16 8" width="16" height="8" aria-hidden="true"><path d="M0 6 L5 4 L10 2 L16 1" fill="none" stroke="currentColor" stroke-width="2"/></svg>',
  forecast: '<svg viewBox="0 0 16 8" width="16" height="8" aria-hidden="true"><path d="M0 6 L5 4 L10 2 L16 1" fill="none" stroke="currentColor" stroke-width="2" stroke-dasharray="4 3"/></svg>',
  band:     '<svg viewBox="0 0 16 8" width="16" height="8" aria-hidden="true"><rect x="0" y="1" width="16" height="6" fill="currentColor" opacity=".3"/></svg>',
  mark:     '<svg viewBox="0 0 16 8" width="16" height="8" aria-hidden="true"><line x1="0" y1="4" x2="16" y2="4" stroke="currentColor" stroke-width="1.5" stroke-dasharray="3 3"/></svg>',
  rising:   '<svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true"><path d="M2 12 L8 5 L14 9" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><path d="M10 4 L14 4 L14 8" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
  falling:  '<svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true"><path d="M2 5 L8 12 L14 7" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  steady:   '<svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true"><line x1="2" y1="8" x2="14" y2="8" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
  warn:     '<svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true"><path d="M8 1 L15 14 L1 14 Z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><line x1="8" y1="6" x2="8" y2="10" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><circle cx="8" cy="12" r="0.9" fill="currentColor"/></svg>',
  thin:     '<svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true"><circle cx="8" cy="8" r="6.5" fill="none" stroke="currentColor" stroke-width="1.6"/><line x1="8" y1="4.5" x2="8" y2="8.5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/><circle cx="8" cy="11" r="0.9" fill="currentColor"/></svg>',
};

function sparkKey(hasDanger, hasWarning) {
  const item = (icon, label, color) =>
    `<span class="key-item" style="color:${color}">${icon}<em>${label}</em></span>`;
  return `<div class="spark-key">
    ${item(ICON.observed, 'Observed', cssVar('--accent'))}
    ${item(ICON.forecast, 'Forecast', cssVar('--accent'))}
    ${item(ICON.band, '80% range', cssVar('--accent'))}
    ${hasDanger ? item(ICON.mark, 'Danger', cssVar('--danger')) : ''}
    ${hasWarning ? item(ICON.mark, 'Warning', cssVar('--warning')) : ''}
  </div>`;
}

/* The verdict. Every clause is tied to a number the model actually computed:
   this is the "so what", not a restatement of the chart. */
function forecastVerdict(d) {
  const p = d.predictive, fc = p.forecast, desc = d.descriptive;
  const rate = d.rise_rate;
  const n = (fc.note.match(/^(\d+) readings/) || [])[1];
  const sigma = (fc.note.match(/sigma ([\d.]+)/) || [])[1];

  // Thin evidence is itself a finding, and must not be quietly hidden.
  if (fc.method === 'insufficient-history') {
    return {
      icon: ICON.thin, tone: 'thin',
      headline: 'Not enough history to forecast',
      body: 'This gauge has fewer than three stored readings, so it is scored on '
        + 'its current level alone with no trend term. The projection appears '
        + 'once a few more cycles have run.',
    };
  }

  const last = fc.values[fc.values.length - 1];
  const now = desc.level_m;
  const change = (last != null && now != null) ? last - now : null;
  const spread = fc.upper.length
    ? fc.upper[fc.upper.length - 1] - fc.lower[fc.lower.length - 1] : null;

  const evidence = [
    n ? n + ' readings' : null,
    sigma ? 'residual sigma ' + sigma + ' m' : null,
    fc.confidence + ' confidence',
  ].filter(Boolean).join(' \u00b7 ');

  let icon = ICON.steady, tone = 'calm', headline, body;

  if (p.hours_to_danger != null && p.hours_to_danger <= 12) {
    icon = ICON.warn;
    tone = 'urgent';
    headline = 'Projected to reach the danger mark in ' + p.hours_to_danger + ' h';
    body = 'Rising ' + (rate ? rate.toFixed(3) : '?') + ' m/h. Carrying that rate '
      + 'from ' + fmt(now) + ' m up to the ' + fmt(d.danger_level) + ' m danger mark '
      + 'gives ' + p.hours_to_danger + ' h. The logistic model puts the 6-hour '
      + 'breach probability at ' + Math.round((p.p_exceed_6h || 0) * 100) + '%.';
  } else if (rate > 0.02) {
    icon = ICON.rising;
    tone = 'watch';
    headline = 'Rising'
      + (change != null ? ', ' + (change >= 0 ? '+' : '') + change.toFixed(2) + ' m expected over 12 h' : '');
    body = 'Measured rise ' + rate.toFixed(3) + ' m/h. Damped Holt projects '
      + fmt(last) + ' m by hour 12'
      + (spread ? ', with an 80% range ' + spread.toFixed(2) + ' m wide' : '') + '. '
      + (d.danger_level
          ? 'Danger mark is ' + fmt(d.danger_level) + ' m.'
          : 'No danger mark is published for this gauge.');
  } else if (rate < -0.02) {
    icon = ICON.falling;
    tone = 'calm';
    headline = 'Falling ' + Math.abs(rate).toFixed(3) + ' m/h';
    body = d.prescriptive && d.prescriptive.impoundment_override
      ? 'A falling river here is NOT reassuring: it matches the upstream '
        + 'impoundment signature. See the outburst protocol below.'
      : 'Level is receding. Projected ' + fmt(last) + ' m by hour 12.';
  } else {
    headline = 'Steady, no meaningful trend';
    body = "Change is under 0.02 m/h, inside this gauge's own noise floor. "
      + 'The projection stays near ' + fmt(last) + ' m.';
  }

  if (fc.confidence === 'low') {
    body += ' Treat the projection as indicative only: it rests on a short series.';
  }
  if (desc.percentile_vs_own_history != null) {
    body += ' The current level sits at the ' + desc.percentile_vs_own_history
      + "th percentile of this gauge's own recorded history.";
  }

  return { icon, tone, headline, body, evidence };
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

function render() {
  renderList();
  renderLegend();
  drawMap();
  if (satMap && !$('#pane-explore').hidden) drawSatOverlay();
}

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


// ---------------------------------------------------------------------------
// Explore tab: inspect one place from orbit.
//
// Two kinds of imagery, and the difference matters enough to state on screen
// every time: Esri is sub-metre but a mosaic months to years old, GIBS is only
// 250 m but genuinely from yesterday. Someone deciding whether a village is
// under water must not read old detail as current truth.
// ---------------------------------------------------------------------------
let satMap = null;
let satTiles = null;
let satOverlay = null;      // our own gauges and events, drawn over the imagery
let satFocus = null;        // ring around the place being inspected

function initSatMap() {
  if (satMap) return satMap;
  satMap = L.map('satmap', { zoomControl: false, attributionControl: true })
    .setView([28.2, 84.0], 9);
  satTiles = L.tileLayer(`/api/satellite/{z}/{x}/{y}.jpg?v=${TILE_VERSION}`, {
    maxZoom: 19, attribution: 'Imagery &copy; Esri, Maxar',
  }).addTo(satMap);
  document.getElementById('satmap').classList.add('sat-imagery');
  satOverlay = L.layerGroup().addTo(satMap);
  satFocus = L.layerGroup().addTo(satMap);

  // Panning the imagery is the point of this view, so the overlay follows.
  satMap.on('moveend', drawSatOverlay);
  return satMap;
}

/* Draw our tags on top of the imagery, limited to what is in view.
   Imagery alone answers "what is there"; the tags answer "what does the model
   think about it", and the whole value of this panel is seeing both at once.
   Labels are drawn at higher zoom only, so the view does not turn to soup. */
function drawSatOverlay() {
  if (!satOverlay) return;
  satOverlay.clearLayers();
  const bounds = satMap.getBounds();
  const zoom = satMap.getZoom();
  const labelled = zoom >= 9;

  state.stations.forEach((st) => {
    if (st.lat == null || !bounds.contains([st.lat, st.lon])) return;
    const color = bandColor(st.band);
    L.circleMarker([st.lat, st.lon], {
      radius: labelled ? 7 : 5,
      fillColor: color, fillOpacity: 0.9,
      color: '#fff', weight: st.fsi >= 75 ? 2 : 1,
    }).bindTooltip(
      `${st.name} — FSI ${st.fsi} ${st.band}`, { direction: 'top' }
    ).on('click', () => exploreAt(st)).addTo(satOverlay);

    if (labelled) {
      L.marker([st.lat, st.lon], {
        icon: L.divIcon({
          className: 'sat-label',
          html: `<span style="--band:${color}">${Math.round(st.fsi)}</span>`,
          iconSize: [26, 16], iconAnchor: [-6, 8],
        }),
        interactive: false,
      }).addTo(satOverlay);
    }
  });

  state.events.forEach((e) => {
    if (e.lat == null || e.kind === 'alert' || !bounds.contains([e.lat, e.lon])) return;
    const style = EVENT_STYLE[e.kind] || EVENT_STYLE.news;
    L.circleMarker([e.lat, e.lon], {
      radius: 5, fillColor: cssVar(style.varName), fillOpacity: 0.75,
      color: '#fff', weight: 1, dashArray: e.approximate ? '2 2' : null,
    }).bindTooltip(
      `${style.label}: ${e.title}${e.approximate ? ' (approximate)' : ''}`,
      { direction: 'top' }
    ).addTo(satOverlay);
  });
}

function satUrlFor(layerId) {
  if (layerId === 'esri') return `/api/satellite/{z}/{x}/{y}.jpg?v=${TILE_VERSION}`;
  return `/api/gibs/${layerId}/${state.satDate}/{z}/{x}/{y}.jpg`;
}

function applyImagery() {
  const isDaily = state.satLayer !== 'esri';
  const meta = isDaily
    ? state.imagery.daily.find((d) => d.id === state.satLayer)
    : state.imagery.basemaps.find((b) => b.id === 'esri');

  $('#date-field').hidden = !isDaily;
  satTiles.options.maxZoom = meta.max_zoom;
  satTiles.setUrl(satUrlFor(state.satLayer));
  satTiles.options.attribution = meta.attribution;
  if (satMap.getZoom() > meta.max_zoom) satMap.setZoom(meta.max_zoom);

  // The honesty banner. Daily imagery is flagged as current; the mosaic is not.
  const fresh = $('#freshness');
  fresh.hidden = false;
  fresh.className = `freshness ${isDaily ? 'current' : 'stale'}`;
  fresh.innerHTML = `<b>${esc(meta.label)}</b> — ${esc(meta.freshness)}` +
    (meta.note ? `<br><span class="muted small">${esc(meta.note)}</span>` : '');
}

function renderExploreHead(place) {
  const band = place.band || 'NORMAL';
  const color = bandColor(band);
  $('#explore-head').innerHTML = `
    <div class="explore-title">
      <h3>${esc(place.name)}</h3>
      <span class="muted small">${esc(place.district || place.basin || '')}
        · ${place.lat.toFixed(4)}, ${place.lon.toFixed(4)}</span>
    </div>
    ${place.fsi != null ? `
      <div class="danger-meter" style="--band:${color}">
        <div class="danger-num"><b>${Math.round(place.fsi)}</b><span>FSI</span></div>
        <div class="danger-bar"><i style="width:${Math.min(100, place.fsi)}%"></i></div>
        <div class="danger-side">
          <span class="tag" style="background:${color}">${band}</span>
          <span class="muted small">
            ${place.p_exceed_6h != null ? Math.round(place.p_exceed_6h * 100) + '% in 6 h' : ''}
            ${place.hours_to_danger != null ? ' · ' + place.hours_to_danger + ' h to danger' : ''}
          </span>
        </div>
      </div>
      ${place.impoundment_suspected
        ? '<p class="impound-note">Possible upstream impoundment — treat a falling river here as a warning, not an all-clear.</p>'
        : ''}
    ` : `<p class="muted small">${esc(place.detail || '')}</p>`}`;
}

async function exploreAt(place) {
  state.explore = place;
  showTab('explore');

  if (!state.imagery) state.imagery = await fetchJson('/api/imagery/options');
  if (!state.satDate) state.satDate = state.imagery.dates[0];

  // Populate the selectors once.
  const sel = $('#imagery-layer');
  if (!sel.options.length) {
    sel.innerHTML = [
      `<option value="esri">${esc(state.imagery.basemaps.find((b) => b.id === 'esri').label)}</option>`,
      ...state.imagery.daily.map((d) => `<option value="${esc(d.id)}">${esc(d.label)}</option>`),
    ].join('');
    $('#imagery-date').innerHTML = state.imagery.dates
      .map((d) => `<option value="${esc(d)}">${esc(d)}</option>`).join('');
  }

  $('#imagery-controls').hidden = false;
  $('#sat-wrap').hidden = false;
  // Deep links can set the layer, so the control must be told what it is showing.
  sel.value = state.satLayer;
  $('#imagery-date').value = state.satDate;
  $('#date-field').hidden = state.satLayer === 'esri';

  renderExploreHead(place);
  initSatMap();
  applyImagery();

  // Leaflet cannot size a map that was display:none when created.
  satMap.invalidateSize();
  const zoom = state.satLayer === 'esri' ? 14 : 8;
  satMap.setView([place.lat, place.lon], zoom);

  satFocus.clearLayers();
  L.circleMarker([place.lat, place.lon], {
    radius: 14, color: cssVar('--accent'), weight: 2, fill: false, dashArray: '5 4',
  }).addTo(satFocus);
  drawSatOverlay();

  $('#worldview').href =
    `${state.imagery.worldview}?v=${place.lon - 1.5},${place.lat - 1},${place.lon + 1.5},${place.lat + 1}` +
    `&t=${state.satDate}`;

  renderExternalLinks(place);
  renderNearby(place);
}

/* Hand-off to the big consumer map services.
   These are deep links, not embedded tiles, and that distinction is deliberate:
   proxying Google's tiles would breach the Maps Platform terms, and those same
   terms forbid caching tiles offline anyway. A link costs nothing, needs no API
   key, and gives the user the full product -- Street View included, which no
   tile layer could provide. */
function renderExternalLinks(place) {
  const { lat, lon } = place;
  const links = [
    ['Google Maps', `https://www.google.com/maps/@${lat},${lon},15z`],
    ['Street View', `https://www.google.com/maps/@?api=1&map_action=pano&viewpoint=${lat},${lon}`],
    ['Google Earth', `https://earth.google.com/web/@${lat},${lon},1000a,15000d`],
    ['OpenStreetMap', `https://www.openstreetmap.org/#map=15/${lat}/${lon}`],
  ];
  $('#external-links').innerHTML = `
    <h4>Open this place in</h4>
    <div class="ext-links">
      ${links.map(([label, url]) =>
        `<a class="btn" href="${safeUrl(url)}" target="_blank" rel="noopener noreferrer">${label}</a>`
      ).join('')}
    </div>`;
}

/* Ground truth beside the imagery.
   A 250 m MODIS pixel cannot see a washed-out footbridge, and a BIPAD report
   cannot show how far the water has spread. Showing both is the point. */
async function renderNearby(place) {
  const el = $('#nearby');
  el.innerHTML = '<p class="empty">Loading nearby reports…</p>';
  try {
    const n = await fetchJson(
      `/api/nearby?lat=${place.lat}&lon=${place.lon}&radius_km=${NEARBY_RADIUS_KM}`);

    const section = (title, items, render) => items.length
      ? `<h4>${title} <span class="muted">${items.length}</span></h4>
         <ul class="nearby-list">${items.map(render).join('')}</ul>`
      : '';

    el.innerHTML = `
      <div class="nearby-head">
        <h4>Within ${NEARBY_RADIUS_KM} km</h4>
        <span class="muted small">${n.counts.gauges} gauges ·
          ${n.counts.incidents} incidents · ${n.counts.hazards} hazards ·
          ${n.counts.news} headlines</span>
      </div>

      ${section('Gauges', n.gauges.slice(0, 8), (g) => `
        <li>
          <button class="nearby-row" data-station="${g.id}">
            <span class="dot" style="background:${bandColor(g.band)}"></span>
            <span class="nearby-main">${esc(g.name)}
              <span class="muted small">${g.distance_km} km · ${esc(g.band)}</span></span>
            <b style="color:${bandColor(g.band)}">${Math.round(g.fsi)}</b>
          </button>
        </li>`)}

      ${section('Official incidents (BIPAD)', n.incidents.slice(0, 10), (i) => `
        <li>
          <a class="nearby-row" href="${safeUrl(i.url)}" target="_blank" rel="noopener noreferrer">
            <span class="dot" style="background:${cssVar('--warning')}"></span>
            <span class="nearby-main">${esc(i.title)}
              <span class="muted small">${esc(i.hazard)} · ${i.distance_km} km ·
                ${fmtDate(i.occurred_on)}</span></span>
          </a>
        </li>`)}

      ${section('Earthquakes &amp; fires', n.hazards.slice(0, 6), (h) => `
        <li>
          <a class="nearby-row" href="${safeUrl(h.url)}" target="_blank" rel="noopener noreferrer">
            <span class="dot" style="background:${cssVar(h.kind === 'fire' ? '--fire' : '--quake')}"></span>
            <span class="nearby-main">${esc(h.title)}
              <span class="muted small">${h.kind === 'earthquake' ? 'M' + h.magnitude : (h.magnitude ?? '?') + ' MW'}
                · ${h.distance_km} km${h.extra && h.extra.landslide_trigger ? ' · landslide-capable' : ''}</span></span>
          </a>
        </li>`)}

      ${section('Headlines in these districts', n.news.slice(0, 8), (w) => `
        <li>
          <a class="nearby-row" href="${safeUrl(w.url)}" target="_blank" rel="noopener noreferrer">
            <span class="dot" style="background:${cssVar('--accent')}"></span>
            <span class="nearby-main">${esc(w.title)}
              <span class="muted small">${esc(w.source)} · ${esc((w.matched || []).join(', '))}</span></span>
          </a>
        </li>`)}

      ${Object.values(n.counts).every((c) => c === 0)
        ? '<p class="empty">Nothing reported within this radius.</p>' : ''}`;

    el.querySelectorAll('[data-station]').forEach((b) =>
      b.addEventListener('click', () => {
        const st = state.stations.find((x) => x.id === Number(b.dataset.station));
        if (st) exploreAt(st);
      }));
  } catch (err) {
    el.innerHTML = '<p class="empty">Could not load nearby reports.</p>';
    console.error(err);
  }
}

function showTab(which) {
  const feeds = which === 'feeds';
  if (typeof writeHash === 'function') writeHash();
  $('#pane-feeds').hidden = !feeds;
  $('#pane-explore').hidden = feeds;
  $('#tab-feeds').setAttribute('aria-selected', String(feeds));
  $('#tab-explore').setAttribute('aria-selected', String(!feeds));
  if (!feeds && satMap) setTimeout(() => satMap.invalidateSize(), 0);
}

$('#tab-feeds').addEventListener('click', () => showTab('feeds'));
$('#tab-explore').addEventListener('click', () => showTab('explore'));

$('#imagery-layer').addEventListener('change', (e) => {
  state.satLayer = e.target.value;
  applyImagery();
  // Daily imagery tops out far coarser than the mosaic; pull back so the view
  // does not land on an empty grey grid.
  if (state.satLayer !== 'esri' && satMap.getZoom() > 8) satMap.setZoom(8);
});

$('#imagery-date').addEventListener('change', (e) => {
  state.satDate = e.target.value;
  applyImagery();
  if (state.explore) {
    $('#worldview').href =
      `${state.imagery.worldview}?v=${state.explore.lon - 1.5},${state.explore.lat - 1},` +
      `${state.explore.lon + 1.5},${state.explore.lat + 1}&t=${state.satDate}`;
  }
});

$('#sat-zoom-in').addEventListener('click', () => satMap && satMap.zoomIn());
$('#sat-zoom-out').addEventListener('click', () => satMap && satMap.zoomOut());

document.querySelectorAll('input[name="basemap"]').forEach((r) =>
  r.addEventListener('change', () => setBasemap(r.value)));


/* ---------------------------------------------------------------------------
   Deep links.

   State lives in the URL hash so a view can be shared: #station=5080&tab=explore
   points a colleague at one gauge's orbital view rather than at "the dashboard".
   Reading happens once at boot; writing is debounced so panning the map does not
   flood the history stack.
--------------------------------------------------------------------------- */
function readHash() {
  const h = new URLSearchParams(location.hash.slice(1));
  const out = {};
  for (const [k, v] of h) out[k] = v;
  return out;
}

let hashTimer = null;
function writeHash() {
  clearTimeout(hashTimer);
  hashTimer = setTimeout(() => {
    const parts = [];
    if (state.selected) parts.push(`station=${state.selected}`);
    if (!$('#pane-explore').hidden) parts.push('tab=explore');
    if (state.theme !== 'dark') parts.push(`theme=${state.theme}`);
    if (state.basemap !== 'dark') parts.push(`basemap=${state.basemap}`);
    if (state.satLayer !== 'esri') parts.push(`imagery=${state.satLayer}`);
    const next = parts.length ? '#' + parts.join('&') : ' ';
    history.replaceState(null, '', next);
  }, 400);
}

/* Applied after the first data load, since a station link needs the station
   list to exist before it can resolve. */
async function applyHash() {
  const h = readHash();
  if (h.theme === 'light' || h.theme === 'dark') applyTheme(h.theme);
  if (h.basemap && ['dark', 'light', 'esri'].includes(h.basemap)) setBasemap(h.basemap);
  if (h.imagery) state.satLayer = h.imagery;

  if (h.station) {
    const st = state.stations.find((x) => x.id === Number(h.station));
    if (st) {
      await selectStation(st.id);
      if (h.tab === 'explore') await exploreAt(st);
    }
  } else if (h.tab === 'explore') {
    showTab('explore');
  }
}

/* Region selector. Nepal is the only enabled region today; the others are
   listed but disabled so the extension point is visible rather than implied. */
async function loadRegions() {
  try {
    const { default: def, regions } = await fetchJson('/api/regions');
    state.region = def;
    // Live regions first, then the declared-but-disabled ones grouped under a
    // single heading -- repeating "(not yet available)" on every row said the
    // same thing three times.
    const live = regions.filter((r) => r.enabled);
    const soon = regions.filter((r) => !r.enabled);
    const opt = (r) => `<option value="${esc(r.code)}"${r.code === def ? ' selected' : ''}${
      r.enabled ? '' : ' disabled'}>${esc(r.name)}</option>`;
    $('#region').innerHTML =
      live.map(opt).join('') +
      (soon.length ? `<optgroup label="Others — coming soon">${soon.map(opt).join('')}</optgroup>` : '');
    const active = regions.find((r) => r.code === def);
    if (active) map.setView(active.center, active.zoom);
  } catch { /* selector is optional; the map still works */ }
}

$('#region').addEventListener('change', (e) => {
  state.region = e.target.value;
  refresh();
});

applyTheme(state.theme);
updateEventHint();
loadRegions();
// A station link cannot resolve until the station list exists.
refresh().then(applyHash);
setInterval(refresh, POLL_MS);
