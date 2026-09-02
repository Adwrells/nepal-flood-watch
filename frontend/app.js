/* Nepal Flood Watch - dashboard client.
   No framework and no build step: one file, plain DOM, Leaflet for the map.
   Data comes from the FastAPI backend; tiles come from its local cache so the
   console keeps rendering when the network is down. */

const BANDS = ['SEVERE', 'DANGER', 'WARNING', 'WATCH', 'NORMAL'];
const BAND_VAR = { SEVERE: '--severe', DANGER: '--danger', WARNING: '--warning', WATCH: '--watch', NORMAL: '--normal' };
// SSE carries live updates; this poll is only the fallback when it is blocked.
const POLL_MS = 300_000;
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
  layers: { gauges: true, impoundment: true, events: true, quakes: true,
            fires: true, facilities: true, glof: false },
  facilities: [],
  glofWatch: null,        // /api/outburst/glof-watch payload, fetched on demand
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

// The selection ping sits above every marker so it is never buried under a
// dense cluster of gauges or event pins.
map.createPane('selectionPane');
map.getPane('selectionPane').style.zIndex = 650;

// One Leaflet layer group per toggle, so visibility is a single add/remove.
const groups = {
  gauges: L.layerGroup().addTo(map),
  impoundment: L.layerGroup().addTo(map),
  events: L.layerGroup().addTo(map),
  facilities: L.layerGroup(),
  quakes: L.layerGroup().addTo(map),
  fires: L.layerGroup().addTo(map),
  glof: L.layerGroup(),
};
const selectionLayer = L.layerGroup().addTo(map);
const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

/* Radar-ping highlight for the selected station: a static halo marks the exact
   spot at all times, two offset rings breathe outward from it, and the core
   dot carries the band colour. Every ring is double-outlined in black and
   white so the marker reads against a dark basemap, a light one, and satellite
   imagery alike -- band colour alone washes out against some of those. */
function highlightStation(lat, lon, color) {
  selectionLayer.clearLayers();
  const html = `<span class="select-ping ${reduceMotion ? 'still' : ''}" style="--ping:${color}">
      <i class="select-ping-halo"></i>
      <i class="select-ping-ring"></i>
      <i class="select-ping-ring delay"></i>
      <i class="select-ping-core"></i></span>`;
  L.marker([lat, lon], {
    icon: L.divIcon({ html, className: 'select-ping-wrap', iconSize: [64, 64], iconAnchor: [32, 32] }),
    pane: 'selectionPane',
    interactive: false,
  }).addTo(selectionLayer);
}

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
    ${safeUrl(e.url) ? `<a href="${safeUrl(e.url)}" target="_blank" rel="noopener noreferrer">Open source</a>` : ''}
    ${mapLinks(e.lat, e.lon, e.title)}`);
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
  if (state.layers.facilities) loadFacilities().then(drawFacilities);
});

// Facilities are fetched for the visible area, so panning refetches them.
map.on('moveend', () => {
  if (state.layers.facilities) loadFacilities().then(drawFacilities);
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
        <button class="btn" id="detail-chart" type="button">Chart</button>
        <button class="btn" id="detail-explore" type="button">Satellite</button>
        <button class="btn icon" id="detail-close" aria-label="Close details">&times;</button>
      </span>
    </div>

    <dl class="facts">
      <dt>Level</dt><dd>${fmt(d.descriptive.level_m)} m</dd>
      <dt>Warning / Danger</dt><dd>${fmt(d.warning_level)} / ${fmt(d.danger_level)} m</dd>
      <dt>At the current rate</dt><dd>${p.hours_to_danger != null ? p.hours_to_danger + ' h to danger' : 'not rising'}</dd>
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
          ${a.action_ne ? `<span class="action-ne">${esc(a.action_ne)}</span>` : ''}
          ${a.note ? `<span class="muted small">${esc(a.note)}</span>` : ''}
        </li>`).join('')}
    </ol>`;

  $('#detail-close').addEventListener('click', () => {
    $('#detail').hidden = true; state.selected = null; renderList(); selectionLayer.clearLayers();
  });
  $('#detail-chart').addEventListener('click', () => openChartWindow(id));
  $('#detail-explore').addEventListener('click', () => {
    const st = state.stations.find((x) => x.id === id);
    if (st) exploreAt(st);
  });
  const s = state.stations.find((x) => x.id === id);
  if (s) {
    highlightStation(s.lat, s.lon, bandColor(s.band));
    const zoom = Math.max(map.getZoom(), 9);
    // flyTo reads as deliberate, not jarring; reduced-motion users get an
    // instant recentre instead of the glide.
    if (reduceMotion) map.setView([s.lat, s.lon], zoom);
    else map.flyTo([s.lat, s.lon], zoom, { duration: 0.7 });
  }
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
  el.addEventListener('change', async () => {
    state.layers[el.dataset.layer] = el.checked;
    if (el.dataset.layer === 'facilities' && el.checked) {
      await loadFacilities();
      drawFacilities();
    }
    if (el.dataset.layer === 'glof' && el.checked) {
      await loadGlofWatch();
      drawGlofLakes();
    }
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
  ['feeds', 'explore', 'updates', 'model', 'charts', 'glof', 'profile'].forEach((t) => {
    $(`#pane-${t}`).hidden = t !== which;
    $(`#tab-${t}`).setAttribute('aria-selected', String(t === which));
  });
  if (typeof writeHash === 'function') writeHash();
  if (which === 'explore' && satMap) setTimeout(() => satMap.invalidateSize(), 0);
  if (which === 'updates') renderUpdates();
  if (which === 'model') renderModel();
  if (which === 'charts') renderCharts();
  if (which === 'glof') renderGlofTab();
  if (which === 'profile') renderProfileTab();
}

$('#tab-feeds').addEventListener('click', () => showTab('feeds'));
$('#tab-explore').addEventListener('click', () => showTab('explore'));
$('#tab-updates').addEventListener('click', () => showTab('updates'));
$('#tab-model').addEventListener('click', () => showTab('model'));
$('#tab-charts').addEventListener('click', () => showTab('charts'));
$('#tab-glof').addEventListener('click', () => showTab('glof'));
$('#tab-profile').addEventListener('click', () => showTab('profile'));
$('#chart-filter').addEventListener('input', (e) => {
  chartState.filter = e.target.value; drawChartGrid();
});
$('#chart-sort').addEventListener('change', (e) => {
  chartState.sort = e.target.value; drawChartGrid();
});

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
/* Every key this app understands. An allowlist rather than sanitising, because
   the set is small and known: assigning arbitrary URL-supplied keys onto an
   object literal lets `#__proto__=x` reach Object.prototype, and a null-
   prototype object alone would still accept junk keys we never read. */
const HASH_KEYS = new Set([
  'station', 'tab', 'theme', 'basemap', 'imagery', 'ack', 'nolive',
]);

function readHash() {
  const h = new URLSearchParams(location.hash.slice(1));
  const out = Object.create(null);
  for (const [k, v] of h) {
    if (HASH_KEYS.has(k)) out[k] = v;
  }
  return out;
}

let hashTimer = null;
function writeHash() {
  clearTimeout(hashTimer);
  hashTimer = setTimeout(() => {
    const parts = [];
    if (state.selected) parts.push(`station=${state.selected}`);
    if (!$('#pane-explore').hidden) parts.push('tab=explore');
    else if (!$('#pane-updates').hidden) parts.push('tab=updates');
    else if (!$('#pane-model').hidden) parts.push('tab=model');
    else if (!$('#pane-glof').hidden) parts.push('tab=glof');
    else if (!$('#pane-profile').hidden) parts.push('tab=profile');
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
      else if (h.tab) showTab(h.tab);
    }
  } else if (h.tab && ['feeds', 'explore', 'updates', 'model', 'glof', 'profile'].includes(h.tab)) {
    showTab(h.tab);
  }
}


// ---------------------------------------------------------------------------
// Emergency contacts and the launch notice
// ---------------------------------------------------------------------------
/* Rendered as markup rather than NDRRMA's poster image on purpose: numbers in a
   JPEG cannot be tapped, copied, translated or read aloud by a screen reader,
   and on a phone during a flood tap-to-dial is the entire point. The content and
   the issuer are unchanged. */
const KIND_ICON = {
  police:   '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 2 4 5v6c0 4.5 3.4 8.3 8 9 4.6-.7 8-4.5 8-9V5l-8-3z"/></svg>',
  medical:  '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>',
  disaster: '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 2 2 20h20L12 2z"/><path d="M12 9v5M12 17h.01"/></svg>',
  fire:     '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 2s5 5 5 9a5 5 0 0 1-10 0c0-2 1-3 2-4 0 2 1 3 2 3 1 0 1-1 1-2 0-3-2-4 0-6z"/></svg>',
};

let emergencyData = null;

async function loadEmergency() {
  if (emergencyData) return emergencyData;
  emergencyData = await fetchJson('/api/emergency');
  return emergencyData;
}

function contactRow(c) {
  // tel: strips spaces; the displayed number keeps them for readability.
  return `<a class="tel" href="tel:${esc(c.number.replace(/\s/g, ''))}">
      <span class="tel-icon ${esc(c.kind)}">${KIND_ICON[c.kind] || ''}</span>
      <span class="tel-main">${esc(c.label)}
        <span class="muted small">${esc(c.label_ne)}</span></span>
      <b>${esc(c.number)}</b>
    </a>`;
}

async function renderNotice() {
  const d = await loadEmergency();
  $('#notice-numbers').innerHTML =
    `<div class="tel-list">${d.national.map(contactRow).join('')}</div>` +
    `<p class="muted small notice-note">${esc(d.note)}</p>`;
}

/* Shown once per browser unless dismissed permanently. A warning system that
   nags on every reload gets dismissed reflexively, which defeats it. */
function openNotice() {
  $('#notice').hidden = false;
  renderNotice();
  $('#notice-continue').focus();
}

function closeNotice() {
  if ($('#notice-hide').checked) {
    try { localStorage.setItem('noticeAck', '1'); } catch { /* private mode */ }
  }
  $('#notice').hidden = true;
}

$('#notice-close').addEventListener('click', closeNotice);
$('#notice-continue').addEventListener('click', closeNotice);
$('#notice').addEventListener('click', (e) => { if (e.target.id === 'notice') closeNotice(); });
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !$('#notice').hidden) closeNotice();
});
$('#show-emergency').addEventListener('click', openNotice);

// ---------------------------------------------------------------------------
// Updates tab: official sources
// ---------------------------------------------------------------------------
/* Facebook pages are LINKED, not scraped. Reading a Page's posts needs the
   Graph API with a token for a Page you administer; scraping facebook.com HTML
   breaks their Terms of Service. These are public officials' pages we do not
   administer, so a link is the honest and legal option -- and it opens the real
   post with its comments and video, which no scrape would reproduce. */
const OFFICIAL_SOURCES = [
  { group: 'Government', items: [
    { name: 'NDRRMA', detail: 'National Disaster Risk Reduction & Management Authority',
      url: 'https://bipadportal.gov.np/' },
    { name: 'DHM', detail: 'Hydrology & Meteorology — flood bulletins',
      url: 'https://www.dhm.gov.np/' },
    { name: 'Nepal Police', detail: 'Official updates', url: 'https://www.nepalpolice.gov.np/' },
  ]},
  { group: 'International', items: [
    { name: 'WHO Nepal', detail: 'World Health Organization — country office',
      url: 'https://www.who.int/nepal' },
    { name: 'UN OCHA Nepal', detail: 'Humanitarian coordination and situation reports',
      url: 'https://www.unocha.org/nepal' },
    { name: 'IFRC / Nepal Red Cross', detail: 'Relief operations and appeals',
      url: 'https://www.ifrc.org/emergencies' },
    { name: 'UNICEF Nepal', detail: 'Child and family emergency response',
      url: 'https://www.unicef.org/nepal/' },
    { name: 'ReliefWeb — Nepal', detail: 'Aggregated situation reports and assessments',
      url: 'https://reliefweb.int/country/npl' },
  ]},
  { group: 'Representatives', items: [
    { name: 'Balen Shah', detail: 'Mayor, Kathmandu Metropolitan City',
      url: 'https://www.facebook.com/balenOfficial' },
    { name: 'Sudan Gurung', detail: 'Home Minister, Government of Nepal',
      url: 'https://www.facebook.com/sudangrghaminepal/' },
    { name: 'Swarnim Wagle', detail: 'Minister of Finance, Government of Nepal',
      url: 'https://www.facebook.com/swarnim.wagle' },
  ]},
];

/* Reachability is a 45-minute background check on the backend (checking six
   homepages is not worth doing every 12-minute cycle) -- a dot per source
   rather than pretending these pages are a scraped feed, which they are not
   (NDRRMA's bulletin and DHM's notices are client-rendered SPAs with nothing
   structured to parse). */
function statusDot(status) {
  if (!status || status.reachable == null) return '<span class="src-dot" title="Not checked yet"></span>';
  const cls = status.reachable ? 'up' : 'down';
  const title = status.reachable
    ? `Reachable · checked ${fmtDate(status.checked_at)}`
    : `Unreachable · checked ${fmtDate(status.checked_at)}${status.error ? ' · ' + status.error : ''}`;
  return `<span class="src-dot ${cls}" title="${esc(title)}"></span>`;
}

async function renderUpdates() {
  const [news, official] = await Promise.all([
    fetchJson('/api/news?limit=40'),
    fetchJson('/api/official-sources').catch(() => null),
  ]);
  const statusByUrl = {};
  (official?.sources || []).forEach((s) => { statusByUrl[s.url] = s.status; });

  const links = OFFICIAL_SOURCES.map((g) => `
    <h4>${esc(g.group)}</h4>
    <div class="src-list">
      ${g.items.map((i) => `
        <a class="src" href="${safeUrl(i.url)}" target="_blank" rel="noopener noreferrer">
          ${statusDot(statusByUrl[i.url])}
          <span class="src-main">${esc(i.name)}
            <span class="muted small">${esc(i.detail)}</span></span>
          <span class="src-go" aria-hidden="true">&#8599;</span>
        </a>`).join('')}
    </div>`).join('');

  $('#updates').innerHTML = `
    <h4>Live headlines <span class="muted">${news.length}</span></h4>
    <p class="muted small src-note">Scraped from five Nepali news feeds every cycle.</p>
    ${news.length ? news.map((n) => `
      <a class="feed-item" href="${safeUrl(n.url)}" target="_blank" rel="noopener noreferrer">
        <div class="t">${esc(n.title)}</div>
        <div class="s">${esc(n.source)}${n.districts ? ' · ' + esc(n.districts) : ''}</div>
      </a>`).join('') : '<p class="empty">No flood headlines right now.</p>'}

    ${renderSafety()}
    ${links}
    <p class="muted small src-note">
      ${official ? `Link reachability checked every ${official.check_interval_minutes} min in the background.`
                 : 'Link reachability check unavailable.'}
      Linked, not scraped: these pages render client-side, so there is nothing
      structured to pull into a feed.
    </p>
    ${await renderRelief()}
    <p class="muted small src-note">
      Social pages open in a new tab. Their posts are not copied into this
      console: reading them programmatically needs Graph API access to Pages we
      do not administer.
    </p>`;
}

// ---------------------------------------------------------------------------
// Health facilities
// ---------------------------------------------------------------------------
/* 16,299 facilities nationally, so they are fetched for the visible area only
   and above a zoom threshold -- plotting all of them at country zoom would be a
   solid block of markers and tell an operator nothing. */
const FACILITY_ZOOM = 10;

async function loadFacilities() {
  if (!state.layers.facilities || map.getZoom() < FACILITY_ZOOM) {
    state.facilities = [];
    return;
  }
  const c = map.getCenter();
  const radius = Math.min(30, map.getBounds().getNorthEast()
    .distanceTo(map.getBounds().getSouthWest()) / 2000);
  try {
    const d = await fetchJson(
      `/api/facilities/nearest?lat=${c.lat}&lon=${c.lng}&limit=250&radius_km=${radius.toFixed(1)}`);
    state.facilities = d.facilities;
  } catch { state.facilities = []; }
}

function drawFacilities() {
  groups.facilities.clearLayers();
  if (map.getZoom() < FACILITY_ZOOM) return;
  state.facilities.forEach((f) => {
    L.circleMarker([f.lat, f.lon], {
      radius: 5, fillColor: cssVar('--facility'), fillOpacity: 0.9,
      color: '#fff', weight: 1,
    }).bindPopup(`
        <h3>${esc(f.title)}</h3>
        <dl>
          <dt>Type</dt><dd>Health facility</dd>
          <dt>Distance</dt><dd>${f.distance_km} km from map centre</dd>
          <dt>Source</dt><dd>${esc(f.source)}</dd>
        </dl>
        ${mapLinks(f.lat, f.lon, f.title)}`)
      .addTo(groups.facilities);
  });
}

// ---------------------------------------------------------------------------
// Glacial lake outburst flood (GLOF) watch
// ---------------------------------------------------------------------------
/* Six named lakes nationally -- unlike facilities there is no viewport-based
   fetch, since plotting all of them costs nothing at any zoom. This is a
   ranking of ALREADY-KNOWN dangerous lakes (ICIMOD/UNDP/DHM) cross-checked
   against the live gauge network, not a breach prediction -- see the GLOF
   tab and backend/app/hazards/glof_watch.py for why that distinction is kept
   explicit rather than implied by a percentage. */
async function loadGlofWatch() {
  try { state.glofWatch = await fetchJson('/api/outburst/glof-watch'); }
  catch { state.glofWatch = null; }
}

function drawGlofLakes() {
  groups.glof.clearLayers();
  if (!state.glofWatch) return;
  state.glofWatch.lakes.forEach((entry) => {
    const lake = entry.lake;
    const color = entry.live_corroboration ? cssVar('--danger') : cssVar('--glacier');
    const html = `<span class="glof-pin ${entry.live_corroboration ? 'active' : ''}"
        style="--glof:${color}" aria-hidden="true">&#9650;</span>`;
    L.marker([lake.lat, lake.lon], {
      icon: L.divIcon({ html, className: 'glof-pin-wrap', iconSize: [20, 20], iconAnchor: [10, 16] }),
      title: lake.name,
    }).bindPopup(`
        <h3>${esc(lake.name)} <span class="tag" style="background:${color}">Rank ${esc(lake.rank)}</span></h3>
        <dl>
          <dt>Basin / district</dt><dd>${esc(lake.basin)} &middot; ${esc(lake.district)}</dd>
          <dt>Live signal</dt><dd>${entry.live_corroboration ? 'Precursor signal active nearby' : 'No active precursor signal'}</dd>
          <dt>Note</dt><dd>${esc(lake.area_note)}</dd>
          <dt>Source</dt><dd><a href="${safeUrl(lake.source_url)}" target="_blank" rel="noopener noreferrer">${esc(lake.source)}</a></dd>
          ${lake.approximate ? '<dt>Location</dt><dd>approximate — no surveyed lake-level coordinate published</dd>' : ''}
        </dl>
        ${mapLinks(lake.lat, lake.lon, lake.name)}`)
      .addTo(groups.glof);
  });
}

async function renderGlofTab() {
  const el = $('#glof');
  el.innerHTML = '<p class="empty">Loading glacial lake watch…</p>';
  try {
    if (!state.glofWatch) await loadGlofWatch();
    const d = state.glofWatch;
    if (!d) throw new Error('no data');
    drawGlofLakes();

    el.innerHTML = `
      <h4>Known priority glacial lakes</h4>
      <p class="muted small src-note">${esc(d.scope)}</p>
      <div class="glof-cards">
        ${d.lakes.map((entry) => {
          const lake = entry.lake;
          return `<div class="glof-card ${entry.live_corroboration ? 'active' : ''}">
            <div class="glof-card-head">
              <b>${esc(lake.name)}</b>
              <span class="tag" style="background:${entry.live_corroboration ? cssVar('--danger') : cssVar('--glacier')}">
                Rank ${esc(lake.rank)}</span>
            </div>
            <p class="muted small">${esc(lake.basin)} basin &middot; ${esc(lake.district)}${lake.approximate ? ' (approximate)' : ''}</p>
            <p class="small">${esc(entry.note)}</p>
            <p class="muted small">${esc(lake.area_note)}</p>
            <a class="small" href="${safeUrl(lake.source_url)}" target="_blank" rel="noopener noreferrer">${esc(lake.source)} &#8599;</a>
          </div>`;
        }).join('')}
      </div>
      <h4>Basin context</h4>
      <p class="small">${esc(d.context.note)}</p>
      <p class="muted small">
        By basin: ${Object.entries(d.context.by_basin).map(([b, n]) => `${esc(b)} ${n}`).join(' · ')}
        &middot; <a href="${safeUrl(d.context.source_url)}" target="_blank" rel="noopener noreferrer">${esc(d.context.source)} &#8599;</a>
      </p>`;
  } catch (err) {
    el.innerHTML = '<p class="empty">Could not load the glacial lake watch list.</p>';
    console.error(err);
  }
}

// ---------------------------------------------------------------------------
// Country profile: census demographics and protected areas / wildlife
// ---------------------------------------------------------------------------
async function renderProfileTab() {
  const el = $('#profile');
  el.innerHTML = '<p class="empty">Loading country profile…</p>';
  try {
    const [demo, wild] = await Promise.all([
      fetchJson('/api/reference/demographics'), fetchJson('/api/reference/wildlife'),
    ]);

    const demoRows = demo.major_groups.map((g) => `
      <div class="bar-row">
        <span>${esc(g.group)}</span>
        <span class="bar"><i style="width:${g.percent * 5}%"></i></span>
        <b>${g.percent}%</b>
      </div>`).join('');

    const areasByKind = {};
    wild.protected_areas.areas.forEach((a) => {
      (areasByKind[a.kind] = areasByKind[a.kind] || []).push(a);
    });

    el.innerHTML = `
      <h4>Caste &amp; ethnicity — 2021 census</h4>
      <p class="muted small src-note">
        ${esc(demo.source.publisher)}, ${esc(demo.source.publication)} &middot;
        ${demo.total_groups_recorded} groups recorded &middot;
        <a href="${safeUrl(demo.source.url)}" target="_blank" rel="noopener noreferrer">census portal &#8599;</a> &middot;
        <a href="${safeUrl(demo.source.results_explorer_url)}" target="_blank" rel="noopener noreferrer">results explorer (province/district/municipality) &#8599;</a>
      </p>
      <div class="bars">${demoRows}</div>
      <p class="muted small">${esc(demo.source.note)}</p>
      <p class="muted small">Dalit (all sub-groups): ${demo.broad_classifications['Dalit (all sub-groups)']}% &middot; ${esc(demo.broad_classifications.note)}</p>

      <h4>Flagship species</h4>
      <p class="muted small src-note">Most recent national survey per species — figures are periodic, not annual.</p>
      <div class="species-cards">
        ${wild.species_counts.map((s) => `
          <div class="species-card">
            <b>${s.count.toLocaleString()}</b>
            <span>${esc(s.species)}</span>
            <span class="muted small">${s.survey_year ? s.survey_year + ' survey' : 'estimate'}</span>
            ${s.breakdown ? `<span class="muted small">${Object.entries(s.breakdown).map(([k, v]) => `${esc(k)} ${v}`).join(' · ')}</span>` : ''}
            ${s.note ? `<span class="muted small">${esc(s.note)}</span>` : ''}
            <a class="small" href="${safeUrl(s.source_url)}" target="_blank" rel="noopener noreferrer">${esc(s.source)} &#8599;</a>
          </div>`).join('')}
      </div>

      <h4>Protected areas — DNPWC</h4>
      <p class="muted small src-note">
        ${esc(wild.protected_areas.source.note)} &middot;
        <a href="${safeUrl(wild.protected_areas.source.url)}" target="_blank" rel="noopener noreferrer">${esc(wild.protected_areas.source.publisher)} &#8599;</a>
      </p>
      ${Object.entries(areasByKind).map(([kind, areas]) => `
        <p class="muted small"><b>${esc(kind)}</b> (${areas.length})</p>
        <ul class="area-list">
          ${areas.map((a) => `<li class="area-row">
            <span class="area-main">${esc(a.name)}${a.note ? `<span class="muted small"> — ${esc(a.note)}</span>` : ''}</span>
            <b>${a.area_km2 != null ? a.area_km2.toLocaleString() + ' km²' : '—'}</b>
          </li>`).join('')}
        </ul>`).join('')}`;
  } catch (err) {
    el.innerHTML = '<p class="empty">Could not load the country profile.</p>';
    console.error(err);
  }
}

// ---------------------------------------------------------------------------
// Open a coordinate in an external map
// ---------------------------------------------------------------------------
/* Every popup offers this. Deep links, not embedded tiles: Google's terms do
   not permit proxying their tiles, but linking out is free and gives the user
   Street View and turn-by-turn directions, which matter when the question is
   "how do I actually get there". */
function mapLinks(lat, lon, label = '') {
  const q = `${lat},${lon}`;
  const links = [
    ['Google', `https://www.google.com/maps/search/?api=1&query=${q}`],
    ['Directions', `https://www.google.com/maps/dir/?api=1&destination=${q}`],
    ['OSM', `https://www.openstreetmap.org/?mlat=${lat}&mlon=${lon}#map=16/${lat}/${lon}`],
  ];
  return `<div class="popup-links">
    ${links.map(([t, u]) =>
      `<a href="${safeUrl(u)}" target="_blank" rel="noopener noreferrer">${t}</a>`).join('')}
    <button class="copy-coord" data-coord="${q}" title="Copy coordinates">${q}</button>
  </div>`;
}

// Popups are created on demand, so the copy button is bound by delegation.
map.on('popupopen', (e) => {
  const btn = e.popup.getElement().querySelector('.copy-coord');
  if (!btn) return;
  btn.addEventListener('click', () => {
    navigator.clipboard.writeText(btn.dataset.coord).then(() => {
      const was = btn.textContent;
      btn.textContent = 'copied';
      setTimeout(() => { btn.textContent = was; }, 1200);
    }).catch(() => { /* clipboard blocked; the text is selectable anyway */ });
  });
});



// ---------------------------------------------------------------------------
// Flood safety guidance
// ---------------------------------------------------------------------------
/* Standard public-health flood guidance (WHO / IFRC / national disaster
   authorities). Deliberately short: guidance nobody finishes reading protects
   nobody. Split into do and do-not because the failure cases here are mostly
   people doing the wrong thing confidently, not people doing nothing.

   The "do not" list leads on driving and walking through water because those
   are consistently the largest single causes of flood deaths worldwide. */
/* Nepali (ne) alongside English, shown inline -- same convention as
   emergency.py's label/label_ne and the prescriptive actions' action_ne.
   Not a toggle: for the reader it is meant for, this line is the guidance. */
const SAFETY_GUIDE = {
  before: [
    { en: 'Know your evacuation route and a high point you can reach on foot.',
      ne: 'आफ्नो उद्धार मार्ग र पैदल पुग्न सकिने अग्लो ठाउँ पहिचान गर्नुहोस्।' },
    { en: 'Keep documents, medicines and a torch in one bag you can carry.',
      ne: 'कागजात, औषधि र टर्च एउटै बोक्न मिल्ने झोलामा राख्नुहोस्।' },
    { en: 'Agree a meeting point with your family in case phones fail.',
      ne: 'फोन काम नलागे पनि भेट्ने ठाउँ परिवारसँग पहिल्यै तय गर्नुहोस्।' },
    { en: 'Charge phones and power banks when a warning is issued.',
      ne: 'चेतावनी जारी हुनासाथ फोन र पावर बैंक चार्ज गर्नुहोस्।' },
  ],
  during_do: [
    { en: 'Move to higher ground as soon as a warning is issued — do not wait to see water.',
      ne: 'चेतावनी आउनासाथ अग्लो ठाउँमा जानुहोस् — पानी देखेपछि पर्खनु हुँदैन।' },
    { en: 'Switch off electricity and gas at the mains before leaving.',
      ne: 'घर छाड्नुअघि मूल स्विचबाट बिजुली र ग्यास बन्द गर्नुहोस्।' },
    { en: 'Take your emergency bag; leave everything else.',
      ne: 'आपतकालीन झोला मात्र लानुहोस्; बाँकी सबै छोड्नुहोस्।' },
    { en: 'Call 1149 (NEOC) or 100 (Police) if you or others are trapped.',
      ne: 'तपाईं वा अरू कोही फसेमा ११४९ (NEOC) वा १०० (प्रहरी) मा फोन गर्नुहोस्।' },
  ],
  during_dont: [
    { en: 'Do not walk through moving water. 15 cm can knock an adult over.',
      ne: 'बगिरहेको पानीबाट हिँड्नु हुँदैन। १५ से.मी. पानीले पनि व्यक्तिलाई लडाउन सक्छ।' },
    { en: 'Do not drive through a flooded road. 60 cm floats most vehicles.',
      ne: 'डुबेको सडकमा गाडी नचलाउनुहोस्। ६० से.मी. पानीले धेरैजसो गाडी बगाउन सक्छ।' },
    { en: 'Do not cross a bridge with water rising against it.',
      ne: 'पानी बढिरहेको पुलबाट वारपार नगर्नुहोस्।' },
    { en: 'Do not enter a river channel that has gone unusually dry — that can mean an upstream blockage is about to fail.',
      ne: 'असामान्य रूपमा सुकेको नदी नियालमा नपस्नुहोस् — यसले माथिल्लो अवरोध चाँडै भत्किन सक्ने संकेत गर्छ।' },
    { en: 'Do not touch electrical equipment while wet or standing in water.',
      ne: 'भिजेको वा पानीमा उभिएको अवस्थामा विद्युतीय उपकरण नछुनुहोस्।' },
  ],
  after: [
    { en: 'Assume flood water is contaminated — wash hands before eating.',
      ne: 'बाढीको पानी दूषित छ भनी मान्नुहोस् — खानुअघि हात धुनुहोस्।' },
    { en: 'Drink only boiled or treated water; waterborne disease follows floods.',
      ne: 'उमालेको वा शुद्धीकरण गरिएको पानी मात्र पिउनुहोस्; बाढीपछि पानीजन्य रोग फैलिन सक्छ।' },
    { en: 'Do not re-enter a damaged building until it has been checked.',
      ne: 'जाँच नभएसम्म क्षतिग्रस्त भवनमा फेरि नपस्नुहोस्।' },
    { en: 'Call 1115 (Health helpline) for medical advice.',
      ne: 'स्वास्थ्य सल्लाहका लागि १११५ (स्वास्थ्य हेल्पलाइन) मा फोन गर्नुहोस्।' },
  ],
};

function renderSafety() {
  const list = (items, cls = '') =>
    `<ul class="guide ${cls}">${items.map((x) =>
      `<li>${esc(x.en)}<span class="action-ne">${esc(x.ne)}</span></li>`).join('')}</ul>`;
  return `
    <h4>If a flood is coming</h4>
    <div class="guide-block">
      <p class="guide-label">Before</p>${list(SAFETY_GUIDE.before)}
      <p class="guide-label do">During — do</p>${list(SAFETY_GUIDE.during_do, 'do')}
      <p class="guide-label dont">During — do not</p>${list(SAFETY_GUIDE.during_dont, 'dont')}
      <p class="guide-label">Afterwards</p>${list(SAFETY_GUIDE.after)}
    </div>
    <p class="muted small src-note">
      General guidance consistent with WHO, IFRC and national disaster authority
      advice. It does not replace instructions from local officials — if they say
      move, move.
    </p>`;
}


// ---------------------------------------------------------------------------
// Small chart primitives
//
// Inline SVG rather than a charting library: these are four simple shapes, and
// pulling in a 200 KB dependency to draw a bar would cost more than it saves in
// a console that must load on a bad connection during a flood.
//
// All of them take plain arrays and return a string, so they compose into any
// panel and are trivial to test.
// ---------------------------------------------------------------------------
const VIZ = {
  /* Horizontal bars for named magnitudes. Values are 0-100 unless max given. */
  bars(items, { max = 100, unit = '' } = {}) {
    if (!items.length) return '<p class="empty">No data.</p>';
    const top = Math.max(max, ...items.map((i) => i.value || 0)) || 1;
    return `<div class="bars">${items.map((i) => `
      <div class="bar-row">
        <span title="${esc(i.label)}">${esc(i.label)}</span>
        <span class="bar"><i style="width:${Math.max(0, Math.min(100, (i.value / top) * 100))}%;
          ${i.color ? `background:${i.color}` : ''}"></i></span>
        <b>${Math.round(i.value)}${esc(unit)}</b>
      </div>`).join('')}</div>`;
  },

  /* Proportional segments on one line. Reads at a glance where a whole splits. */
  stack(items) {
    const total = items.reduce((a, b) => a + (b.value || 0), 0);
    if (!total) return '<p class="empty">Nothing to show.</p>';
    return `
      <div class="stack" role="img" aria-label="${esc(items.map((i) => `${i.label} ${i.value}`).join(', '))}">
        ${items.filter((i) => i.value > 0).map((i) => `
          <span style="width:${(i.value / total) * 100}%;background:${i.color || 'var(--accent)'}"
                title="${esc(i.label)}: ${i.value}"></span>`).join('')}
      </div>
      <div class="stack-key">${items.filter((i) => i.value > 0).map((i) => `
        <span><i style="background:${i.color || 'var(--accent)'}"></i>${esc(i.label)} ${i.value}</span>`).join('')}
      </div>`;
  },

  /* A single number with a caption. For counts that need no comparison. */
  stat(value, label, sub = '') {
    return `<div class="stat"><b>${esc(String(value))}</b><span>${esc(label)}</span>
      ${sub ? `<em>${esc(sub)}</em>` : ''}</div>`;
  },

  /* Skill bar centred on zero: left of centre is worse than doing nothing. */
  skill(value) {
    if (value === null || value === undefined) return '<span class="muted">n/a</span>';
    const pct = Math.max(-1, Math.min(1, value));
    const w = Math.abs(pct) * 50;
    const good = pct >= 0;
    return `<span class="skillbar" title="${(pct * 100).toFixed(2)}% vs persistence">
      <i style="left:${good ? 50 : 50 - w}%;width:${w || 0.6}%;
        background:${good ? 'var(--normal)' : 'var(--danger)'}"></i>
      <em>${(pct * 100).toFixed(1)}%</em></span>`;
  },
};

// ---------------------------------------------------------------------------
// Model tab: the analytics ladder, and which forecaster actually wins
// ---------------------------------------------------------------------------
const STAGE_NOTE = {
  cleaning: 'Standardise aggressively, never invent. An unparseable reading becomes null, never zero — zero would score NORMAL and hide an outage.',
  descriptive: 'What the network is doing right now.',
  diagnostic: 'What is driving the scores, measured — not the configured weights.',
  predictive: 'Where gauges are heading, and how far ahead we can see.',
  prescriptive: 'What someone should therefore do, gated by the time left to do it.',
};

async function renderModel() {
  const el = $('#model');
  el.innerHTML = '<p class="empty">Measuring…</p>';
  try {
    const [pipe, bake] = await Promise.all([
      fetchJson('/api/analytics/pipeline'),
      fetchJson('/api/models/bakeoff'),
    ]);

    const bandColors = { SEVERE: '--severe', DANGER: '--danger', WARNING: '--warning', WATCH: '--watch', NORMAL: '--normal' };
    const bandItems = Object.entries(pipe.descriptive.bands || {})
      .map(([k, v]) => ({ label: k, value: v, color: cssVar(bandColors[k] || '--accent') }));

    const drivers = Object.entries(pipe.diagnostic.mean_component_contribution || {})
      .map(([k, v]) => ({ label: k, value: v }));

    const rows = Object.entries(bake.models).map(([name, r]) => {
      if (!r.available) {
        return `<tr class="off"><td>${esc(name)}</td><td colspan="3" class="muted small">${esc(r.reason)}</td></tr>`;
      }
      if (!r.usable) {
        return `<tr class="off"><td>${esc(name)}</td><td colspan="3" class="muted small">${esc(r.status || 'insufficient data')}</td></tr>`;
      }
      const active = name === bake.active;
      return `<tr class="${active ? 'active' : ''}">
        <td>${esc(name)}${active ? ' <span class="tag" style="background:var(--accent)">active</span>' : ''}</td>
        <td class="num">${r.mae_m}</td>
        <td>${VIZ.skill(r.skill)}</td>
        <td class="muted small">${r.trained_on ? r.trained_on.toLocaleString() + ' ex.' : '—'}</td>
      </tr>`;
    }).join('');

    el.innerHTML = `
      <h4>Analytics pipeline</h4>
      <div class="ladder">
        ${['cleaning', 'descriptive', 'diagnostic', 'predictive', 'prescriptive'].map((k, i) => `
          <div class="rung">
            <span class="rung-n">${i + 1}</span>
            <div>
              <b>${k}</b>
              <p class="muted small">${esc(STAGE_NOTE[k])}</p>
            </div>
          </div>`).join('')}
      </div>

      <h4>Cleaning</h4>
      <div class="stats">
        ${VIZ.stat(pipe.cleaning.stations_kept ?? '—', 'kept', `of ${pipe.cleaning.stations_in ?? '—'} scraped`)}
        ${VIZ.stat(pipe.cleaning.reporting_level ?? '—', 'reporting', 'a level')}
        ${VIZ.stat(pipe.cleaning.with_danger_mark ?? '—', 'danger marks', 'published')}
      </div>

      <h4>Descriptive</h4>
      ${VIZ.stack(bandItems)}
      <div class="stats">
        ${VIZ.stat(pipe.descriptive.reporting, 'reporting', `of ${pipe.descriptive.gauges} gauges`)}
        ${VIZ.stat(pipe.descriptive.rising, 'rising', '> 0.02 m/h')}
        ${VIZ.stat(pipe.descriptive.stored_readings.toLocaleString(), 'readings', 'stored history')}
      </div>

      <h4>Diagnostic — what is driving scores</h4>
      ${VIZ.bars(drivers)}
      <p class="muted small src-note">${esc(pipe.diagnostic.note)}</p>

      <h4>Predictive</h4>
      <div class="stats">
        ${VIZ.stat(pipe.predictive.gauges_with_time_to_danger, 'gauges', 'with a time-to-danger')}
        ${VIZ.stat(pipe.predictive.soonest_hours ?? '—', 'hours', 'soonest to danger')}
        ${VIZ.stat(Math.round((pipe.predictive.highest_p6h || 0) * 100) + '%', 'peak P(6h)', 'danger breach')}
      </div>

      <h4>Model bake-off <span class="muted">vs ${esc(bake.baseline)}</span></h4>
      <table class="bake">
        <thead><tr><th>model</th><th class="num">MAE m</th><th>skill</th><th>trained</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
      <p class="gate">${esc(bake.gate)}</p>
      <p class="muted small src-note"><b>Verdict.</b> ${esc(bake.recommendation)}</p>
      <p class="muted small src-note">
        ${bake.training_examples.toLocaleString()} supervised examples available.
        ${bake.sklearn_available ? '' : 'scikit-learn is not installed, so the learned models are inactive — see backend/requirements-ml.txt.'}
      </p>

      <h4>Prescriptive</h4>
      <div class="stats">
        ${VIZ.stat(pipe.prescriptive.immediate, 'immediate', 'DANGER or SEVERE')}
        ${VIZ.stat(pipe.prescriptive.elevated, 'elevated', 'WARNING')}
        ${VIZ.stat(pipe.prescriptive.impoundment_overrides, 'overrides', 'impoundment')}
      </div>`;
  } catch (err) {
    el.innerHTML = '<p class="empty">Could not load the model report.</p>';
    console.error(err);
  }
}

// ---------------------------------------------------------------------------
// Live updates over server-sent events
// ---------------------------------------------------------------------------
/* Replaces a 60s poll. This console is watched for hours, and polling meant a
   new DANGER reading could sit unseen for most of a minute. SSE pushes once,
   when a cycle has actually finished.

   The poll is kept as a fallback on a long interval: if SSE is blocked by a
   proxy the console must still update, just less promptly. */
let sse = null;
let sseRetry = 0;

function connectLive() {
  // An open event stream never goes network-idle, which hangs headless capture
  // and any crawler that waits for quiescence. #nolive=1 opts out; the 5-minute
  // fallback poll still keeps such a client current.
  try { if (readHash().nolive) { setLiveState('offline'); return; } } catch { /* no hash */ }
  if (sse) sse.close();
  try {
    sse = new EventSource('/api/stream');
  } catch {
    return;                              // no EventSource; the fallback poll covers it
  }

  sse.addEventListener('cycle', () => {
    setLiveState('live', 'updating…');
    refresh().then(() => { setLiveState('live'); return refreshCharts(); });
    // Live corroboration can flip with the new cycle's impoundment signals.
    if (state.layers.glof) loadGlofWatch().then(drawGlofLakes);
    if (!$('#pane-glof').hidden) renderGlofTab();
  });

  sse.addEventListener('hello', () => { sseRetry = 0; setLiveState('live'); });

  sse.onerror = () => {
    setLiveState('offline');
    sse.close();
    // Back off rather than hammering a server that may be restarting.
    sseRetry = Math.min(sseRetry + 1, 6);
    setTimeout(connectLive, 2000 * sseRetry);
  };
}

function setLiveState(state, note = '') {
  const el = $('#live-dot');
  if (!el) return;
  el.dataset.state = state;
  el.title = state === 'live' ? (note || 'Live — updates pushed from the server')
    : 'Reconnecting — falling back to periodic refresh';
}

// ---------------------------------------------------------------------------
// Relief fund
// ---------------------------------------------------------------------------
/* Links only. The PMO has warned that unofficial QR codes and personal
   accounts are circulating; rendering our own QR would look identical, to
   whoever scans it, to the thing people are being told to distrust. */
async function renderRelief() {
  const d = await fetchJson('/api/relief');
  return `
    <h4>Donate — official channels</h4>
    <div class="relief-warn">
      <b>${esc(d.safety.headline)}</b>
      <p>${esc(d.safety.rule)}</p>
      <p class="muted small">${esc(d.safety.warning)}</p>
      <ul>${d.safety.points.map((x) => `<li>${esc(x)}</li>`).join('')}</ul>
    </div>
    <div class="src-list">
      ${d.channels.map((c) => `
        <a class="src" href="${safeUrl(c.url)}" target="_blank" rel="noopener noreferrer">
          <span class="src-main">${esc(c.name)}
            <span class="muted small">${esc(c.operator)}</span>
            <span class="muted small">${esc(c.methods)}</span></span>
          <span class="src-go" aria-hidden="true">&#8599;</span>
        </a>`).join('')}
    </div>
    <p class="muted small src-note">${esc(d.policy)}</p>`;
}



/* Phone-only: the map overlays start collapsed and open on a tap. The CSS that
   hides their options lives inside the 820px media query, so on desktop the
   `open` class changes nothing and the panels stay as they were. */
(function collapsibleOverlays() {
  document.querySelectorAll('.layers legend').forEach((legend) => {
    const box = legend.closest('.layers');
    legend.setAttribute('role', 'button');
    legend.setAttribute('tabindex', '0');
    legend.setAttribute('aria-expanded', 'false');

    const toggle = (e) => {
      e.stopPropagation();          // never let the tap reach the map beneath
      const open = box.classList.toggle('open');
      legend.setAttribute('aria-expanded', String(open));
    };

    legend.addEventListener('click', toggle);
    legend.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(e); }
    });
  });

  // Tapping the map closes them again, so they never linger over the data.
  map.on('click', () => {
    document.querySelectorAll('.layers.open').forEach((b) => {
      b.classList.remove('open');
      b.querySelector('legend')?.setAttribute('aria-expanded', 'false');
    });
  });
})();

/* On a phone the gauge list is collapsed so the map is above the fold. Tapping
   its heading expands it. Desktop is unaffected -- the CSS that shortens the
   list only applies under 820px, so the class is inert there. */
(function collapsibleGaugeList() {
  const heading = document.querySelector('.panel:not(.right) > h2');
  if (!heading) return;
  const panel = heading.closest('.panel');

  heading.setAttribute('role', 'button');
  heading.setAttribute('tabindex', '0');
  heading.setAttribute('aria-expanded', 'false');

  const toggle = () => {
    const open = panel.classList.toggle('expanded');
    heading.setAttribute('aria-expanded', String(open));
  };

  heading.addEventListener('click', toggle);
  heading.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); }
  });
})();

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
// The notice leads, because knowing who to call matters more than the map.
try {
  // ack=1 in the hash suppresses it for a deep link that is meant to land
  // straight on a view -- a shared link to one gauge should not open a modal.
  const acked = localStorage.getItem('noticeAck') || readHash().ack;
  if (!acked) openNotice();
} catch { openNotice(); }
// A station link cannot resolve until the station list exists.
refresh().then(applyHash);
setInterval(refresh, POLL_MS);
connectLive();


/* ---------------------------------------------------------------------------
   Charts: every river, and one river up close.

   The tab holds a small multiple per gauge -- enough to spot the one shape that
   matters in a wall of flat lines. Clicking any of them opens a floating window
   with the full series, the forecast, and all four analytic layers, because the
   chart on its own answers "what happened" and an operator also needs "why",
   "what next" and "so what".
   --------------------------------------------------------------------------- */

const chartState = { index: [], sort: 'fsi', filter: '', live: null,
                     telescope: false, openId: null, anchor: null };

async function renderCharts() {
  const host = $('#charts');
  if (!chartState.index.length) {
    host.innerHTML = '<p class="empty">Loading series…</p>';
    const d = await fetchJson('/api/charts/index');
    chartState.index = (d && d.stations) || [];
  }
  // Anything already loaded is refreshed by refreshCharts() on the next cycle.
  drawChartGrid();
}

function drawChartGrid() {
  const host = $('#charts');
  const q = chartState.filter.toLowerCase();
  let rows = chartState.index.filter((s) =>
    !q || [s.name, s.district, s.basin].some((v) => (v || '').toLowerCase().includes(q)));

  // Default order is severity, because a grid of 300 charts is only useful if
  // the one you need is near the top.
  const by = {
    fsi: (a, b) => (b.fsi || 0) - (a.fsi || 0),
    rise: (a, b) => (b.rise_rate || 0) - (a.rise_rate || 0),
    name: (a, b) => (a.name || '').localeCompare(b.name || ''),
  }[chartState.sort];
  rows = rows.slice().sort(by);

  if (!rows.length) { host.innerHTML = '<p class="empty">No gauge matches that filter.</p>'; return; }

  host.innerHTML = rows.map((s) => `
    <button class="chart-card" type="button" data-id="${s.id}"
            aria-label="Open the full chart for ${esc(s.name)}">
      <span class="cc-head">
        <span class="cc-name" title="${esc(s.name)}">${esc(s.name)}</span>
        <span class="cc-band" style="background:${bandColor(s.band)}">${esc(s.band)}</span>
      </span>
      ${Charts.thumb(s.series, s.marks)}
      <span class="cc-foot">
        <span>${s.level != null ? Number(s.level).toFixed(2) + ' m' : '—'}</span>
        <span class="muted">${s.marks.danger != null ? 'danger ' + s.marks.danger.toFixed(1) : 'no mark'}</span>
        ${s.hours_to_danger != null
          ? `<span class="cc-ttd" title="Straight-line extrapolation of the current rise rate, not the damped forecast">${s.hours_to_danger} h at this rate</span>` : ''}
      </span>
    </button>`).join('');

  host.querySelectorAll('.chart-card').forEach((b) =>
    b.addEventListener('click', () => openChartWindow(Number(b.dataset.id))));
}

/* The floating window. Focus is trapped while it is open and returned to the
   element that opened it on close, so keyboard users are not dumped at the top
   of the document. */
let chartWinOpener = null;

async function openChartWindow(id) {
  chartWinOpener = document.activeElement;
  closeChartWindow();

  const wrap = document.createElement('div');
  wrap.className = 'chart-win-backdrop';
  wrap.id = 'chart-win';
  wrap.innerHTML = `
    <div class="chart-win" role="dialog" aria-modal="true" aria-labelledby="cw-title">
      <div class="cw-head">
        <div>
          <h3 id="cw-title">Loading…</h3>
          <span class="cw-sub muted"></span>
        </div>
        <div class="cw-tools">
          <button class="btn small" id="cw-scale" type="button" aria-pressed="false"
                  title="Compress older readings onto a logarithmic age axis">Telescope</button>
          <button class="btn small" id="cw-table" type="button" aria-pressed="false">Table</button>
          <button class="btn icon" id="cw-close" aria-label="Close chart">&times;</button>
        </div>
      </div>
      <div class="cw-body">
        <div class="cw-chart" id="cw-chart"><p class="empty">Loading series…</p></div>
        <p class="cw-scale-note" hidden>
          Telescope view: the time axis is logarithmic in age, so the last minutes are
          wide and older readings compress leftward. Useful for long history —
          but slopes in the compressed region look steeper than they are.
        </p>
        <div class="cw-table-wrap" hidden></div>
        <div class="cw-layers" id="cw-layers"></div>
      </div>
    </div>`;
  document.body.appendChild(wrap);

  const close = () => closeChartWindow();
  $('#cw-close').addEventListener('click', close);
  wrap.addEventListener('click', (e) => { if (e.target === wrap) close(); });
  wrap.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { e.stopPropagation(); close(); return; }
    if (e.key !== 'Tab') return;
    const f = wrap.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])');
    if (!f.length) return;
    const first = f[0], last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  });
  $('#cw-close').focus();

  const d = await fetchJson(`/api/station/${id}/analysis`);
  if (!d || !document.getElementById('chart-win')) return;

  $('#cw-title').textContent = d.name;
  wrap.querySelector('.cw-sub').textContent =
    [d.district, d.basin, `FSI ${d.fsi}`, d.band].filter(Boolean).join(' · ');

  const paint = (data, slide) => {
    if (chartState.live) chartState.live.stop();
    chartState.live = Charts.timeSeries($('#cw-chart'), data, {
      telescope: chartState.telescope,
      transitionFrom: slide ? chartState.anchor : null,
    });
    chartState.anchor = chartState.live.anchor;
  };
  chartState.openId = id;
  paint(d, false);

  $('#cw-scale').addEventListener('click', (e) => {
    chartState.telescope = !chartState.telescope;
    e.currentTarget.setAttribute('aria-pressed', String(chartState.telescope));
    e.currentTarget.classList.toggle('on', chartState.telescope);
    wrap.querySelector('.cw-scale-note').hidden = !chartState.telescope;
    paint(d, false);
  });

  $('#cw-table').addEventListener('click', (e) => {
    const w = wrap.querySelector('.cw-table-wrap');
    const show = w.hidden;
    w.hidden = !show;
    e.currentTarget.setAttribute('aria-pressed', String(show));
    e.currentTarget.classList.toggle('on', show);
    if (show && !w.innerHTML) w.innerHTML = Charts.table(d);
  });

  $('#cw-layers').innerHTML = chartLayers(d);
}

/* Bring the charts up to date when a cycle lands.

   Without this the Charts tab keeps whatever it fetched on first open and an
   open window stays a snapshot, which is a strange thing for a console whose
   whole point is that the data moves. The open gauge is refetched at full
   resolution and repainted with a slide, so an arriving reading is something
   you can see happen rather than something you find by reloading. */
async function refreshCharts() {
  const chartsVisible = !$('#pane-charts').hidden;
  if (chartsVisible || chartState.index.length) {
    const d = await fetchJson('/api/charts/index');
    if (d && d.stations) {
      chartState.index = d.stations;
      if (chartsVisible) drawChartGrid();
    }
  }
  if (chartState.openId != null && document.getElementById('chart-win')) {
    const fresh = await fetchJson(`/api/station/${chartState.openId}/analysis`);
    // Guard against the window having been closed or switched mid-flight.
    if (fresh && chartState.openId === fresh.id && document.getElementById('chart-win')) {
      if (chartState.live) chartState.live.stop();
      chartState.live = Charts.timeSeries($('#cw-chart'), fresh, {
        telescope: chartState.telescope,
        transitionFrom: chartState.anchor,
      });
      chartState.anchor = chartState.live.anchor;
      $('#cw-layers').innerHTML = chartLayers(fresh);
    }
  }
}

function closeChartWindow() {
  chartState.openId = null;
  chartState.anchor = null;
  if (chartState.live) { chartState.live.stop(); chartState.live = null; }
  const el = document.getElementById('chart-win');
  if (el) el.remove();
  if (chartWinOpener && chartWinOpener.focus) chartWinOpener.focus();
  chartWinOpener = null;
}

/* The four layers, in the order an operator needs them: what is happening,
   why the score says so, what happens next, and what to do about it. */
function chartLayers(d) {
  const de = d.descriptive || {}, pr = d.predictive || {}, ps = d.prescriptive || {};
  const cross = d.crossing;

  const pct = de.percentile_vs_own_history;
  const descriptive = `
    <dl class="facts">
      <dt>Level now</dt><dd>${fmt(de.level_m)} m</dd>
      <dt>Trend</dt><dd>${esc(de.trend === 'unknown' ? 'awaiting a second reading' : de.trend || '—')}</dd>
      <dt>Acceleration</dt><dd>${de.acceleration_m_per_h2 != null
        ? fmt(de.acceleration_m_per_h2) + ' m/h²' : '—'}</dd>
      <dt>Against its own record</dt><dd>${pct != null ? pct + 'th percentile' : '—'}</dd>
      <dt>Reporting</dt><dd>${esc((de.reporting && de.reporting.state) || '—')}</dd>
      <dt>Rain 24 h / next 12 h</dt><dd>${fmt(d.rainfall.past_24h)} / ${fmt(d.rainfall.next_12h)} mm</dd>
    </dl>`;

  const diagnostic = `
    ${componentBars(d.diagnostic)}
    ${d.impoundment && d.impoundment.suspected
      ? `<p class="warn-note">Impoundment suspected — ${esc(d.impoundment.reason || '')}</p>` : ''}`;

  const predictive = `
    <dl class="facts">
      <dt>Method</dt><dd>${esc(pr.method || '—')} (${esc(pr.confidence || '—')} confidence)</dd>
      <dt>At the current rate</dt><dd>${pr.hours_to_danger != null
        ? pr.hours_to_danger + ' h to danger' : 'not rising toward danger'}</dd>
      <dt>P(danger in 6 h)</dt><dd>${pr.p_exceed_6h != null
        ? Math.round(pr.p_exceed_6h * 100) + '%' : '—'}</dd>
      <dt>Interval</dt><dd>${esc(pr.interval || '')}</dd>
    </dl>
    ${cross
      ? `<p class="${cross.certainty === 'central' ? 'warn-note' : 'muted small'}">
           Forecast reaches the danger mark in ${cross.hours_ahead} h
           ${cross.certainty === 'central'
             ? '(central estimate).'
             : '— but only at the top of the 80% range, so this is a tail risk, not the expectation.'}
         </p>`
      : '<p class="muted small">The damped forecast does not reach the danger mark within the horizon.</p>'}
    ${pr.hours_to_danger != null && !cross
      ? `<p class="muted small">Those two lines disagree on purpose. The countdown above extrapolates
           the current rise rate in a straight line; the forecast damps that trend and shrinks it by
           its signal-to-noise ratio. Straight-line extrapolation backtested 72% worse than assuming
           no change at all, so treat the countdown as a worst case if the rate holds, and the
           forecast as the expectation.</p>` : ''}
    ${pr.note ? `<p class="evidence">Basis: ${esc(pr.note)}</p>` : ''}`;

  const prescriptive = `
    <ol class="actions">${(ps.actions || []).map((a) => `
      <li class="${a.feasible ? '' : 'infeasible'}">${esc(a.action)}
        ${a.action_ne ? `<span class="action-ne">${esc(a.action_ne)}</span>` : ''}
        ${a.note ? `<span class="muted small">${esc(a.note)}</span>` : ''}</li>`).join('')}
    </ol>`;

  const block = (n, title, why, body) => `
    <section class="cw-layer">
      <h4><span class="cw-step">${n}</span>${title}</h4>
      <p class="cw-why muted small">${why}</p>
      ${body}
    </section>`;

  return block(1, 'Descriptive', 'What the gauge is doing right now.', descriptive) +
    block(2, 'Diagnostic', 'Which inputs produced that score, and how much each contributed.', diagnostic) +
    block(3, 'Predictive', 'Where the level goes next, and how sure that is.', predictive) +
    block(4, 'Prescriptive', 'What to do, given the lead time actually available.', prescriptive);
}
