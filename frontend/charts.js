/* Time-series charts for every gauge.
 *
 * Two views of the same data:
 *   - a grid of small multiples, one per river, in the Charts tab
 *   - a floating window with the full chart plus all four analytic layers
 *
 * The x-axis has two modes. Linear is the default and the honest one. Telescope
 * maps history onto log(age), so the last hour is wide and older readings
 * contract toward the left edge without ever falling off. That is genuinely
 * useful for a gauge you have watched for days, but it distorts slope: the same
 * rise drawn in the compressed region looks steeper than it does near "now".
 * So it is opt-in, labelled, and never the default.
 *
 * The animation is a consequence of the scale rather than a bolt-on. Both modes
 * are anchored on a moving clock, so re-rendering on a frame timer makes points
 * drift left on their own; in telescope mode they also compress as they age.
 */

const Charts = (() => {
  const TAU_MS = 20 * 60 * 1000;   // telescope time constant: ~20 min stays wide
  const NOW_FRAC = 0.68;           // where "now" sits across the plot width
  const FPS = 20;                  // smooth enough to read as motion, cheap to leave running

  /* Reasons to hold the chart still.
   *
   * A hidden tab must not keep re-rendering: the drift is invisible and the
   * cost is not. And an animating chart never lets the page reach render-idle,
   * which is what headless screenshot capture waits on -- the same trap the
   * live event stream set, hence the same #nolive=1 escape hatch. */
  // Read once at load: the app rewrites the hash from its own state and drops
  // this flag, so checking it live would stop working after the first write.
  const NO_LIVE = /(^|&)nolive=1(&|$)/.test(location.hash.slice(1));

  const reduceMotion = () =>
    NO_LIVE || window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  const paused = () => document.hidden;

  /* Station names arrive from a scraper, so they are untrusted text on the
     way into an attribute inside innerHTML. Escape at that boundary. */
  const esc = (v) => String(v == null ? '' : v).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  const ms = (t) => {
    const d = new Date(t);
    return Number.isNaN(d.getTime()) ? null : d.getTime();
  };

  /* ------------------------------------------------------------------ scales */

  /* Map a timestamp to an x position.
   *
   * History occupies [0, NOW_FRAC*W] and the forecast [NOW_FRAC*W, W], so the
   * present is always at the same place on screen no matter how much history
   * has accumulated. In telescope mode only the history side compresses;
   * compressing the forecast too would misrepresent lead time, which is the
   * one number an operator actually acts on.
   */
  function makeScaleX(nowMs, oldestMs, latestMs, W, telescope) {
    const past = Math.max(1, nowMs - oldestMs);
    const future = Math.max(1, latestMs - nowMs);
    const xNow = W * NOW_FRAC;

    return (t) => {
      if (t >= nowMs) return xNow + ((t - nowMs) / future) * (W - xNow);
      const age = nowMs - t;
      if (!telescope) return xNow - (age / past) * xNow;
      const k = Math.log1p(age / TAU_MS) / Math.log1p(past / TAU_MS);
      return xNow - k * xNow;
    };
  }

  function makeScaleY(values, H, padFrac) {
    const pf = padFrac == null ? 0.12 : padFrac;
    const vals = values.filter((v) => v != null && Number.isFinite(v));
    if (!vals.length) return { y: () => H / 2, lo: 0, hi: 1 };
    let lo = Math.min.apply(null, vals);
    let hi = Math.max.apply(null, vals);
    const pad = (hi - lo) * pf || Math.max(0.25, Math.abs(hi) * 0.05);
    lo -= pad; hi += pad;
    return { y: (v) => H - ((v - lo) / (hi - lo)) * H, lo, hi };
  }

  /* Round tick values a person would actually choose. */
  function ticksY(lo, hi, count) {
    const n = count || 4;
    const raw = (hi - lo) / n;
    if (!(raw > 0)) return [];
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag)
      .find((s) => s >= raw) || mag * 10;
    const out = [];
    for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) out.push(v);
    return out;
  }

  const path = (pts) => pts
    .map((p, i) => (i ? 'L' : 'M') + p[0].toFixed(1) + ',' + p[1].toFixed(1)).join(' ');

  const clockLabel = (t) => new Date(t).toLocaleTimeString([],
    { hour: '2-digit', minute: '2-digit' });
  const dayLabel = (t) => new Date(t).toLocaleString([],
    { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });

  /* ------------------------------------------------------- the main chart */

  /* Render one gauge's series into `el`.
   *
   * Returns a handle with .stop() because the frame timer outlives the call, and
   * a closed window that keeps animating is a leak that only shows up as a warm
   * laptop twenty minutes later.
   */
  function timeSeries(el, data, opts) {
    const o = opts || {};
    const telescope = !!o.telescope;
    const W = 640, H = 260, M = { t: 14, r: 16, b: 26, l: 44 };
    const pw = W - M.l - M.r, ph = H - M.t - M.b;

    const obs = (data.observed || [])
      .map((d) => ({ t: ms(d.ts), v: d.value }))
      .filter((d) => d.t != null && d.v != null);
    const fc = (data.forecast || [])
      .map((f) => ({ t: ms(f.ts), v: f.value, lo: f.lower, hi: f.upper }))
      .filter((f) => f.t != null && f.v != null);

    if (obs.length < 2 && !fc.length) {
      el.innerHTML = '<p class="empty">Not enough history to plot yet.</p>';
      return { stop() {} };
    }

    const warning = data.marks ? data.marks.warning : null;
    const danger = data.marks ? data.marks.danger : null;

    // The y domain is set by what must be readable: the observations, the
    // marks, and the central forecast. The 80% band is allowed to widen it,
    // but only so far -- at a 12 h horizon that band can span several metres,
    // and letting it drive the scale squashes the actual river into a sliver.
    // Beyond the cap the band simply runs to the edge of the plot, which reads
    // as "wider than shown"; the tooltip and the table still carry the exact
    // bounds, so nothing is hidden, only kept from dominating.
    const core = obs.map((d) => d.v)
      .concat(fc.map((f) => f.v), [warning, danger])
      .filter((v) => v != null && Number.isFinite(v));
    const coreLo = core.length ? Math.min.apply(null, core) : 0;
    const coreHi = core.length ? Math.max.apply(null, core) : 1;
    const cap = Math.max((coreHi - coreLo) * 0.75, 0.2);
    const bandVals = fc.map((f) => f.lo).concat(fc.map((f) => f.hi))
      .filter((v) => v != null && Number.isFinite(v))
      .map((v) => Math.min(Math.max(v, coreLo - cap), coreHi + cap));

    const sy = makeScaleY(core.concat(bandVals), ph);
    const y = sy.y, lo = sy.lo, hi = sy.hi;

    const oldest = obs.length ? obs[0].t : Date.now();
    const latest = fc.length ? fc[fc.length - 1].t
      : (obs.length ? obs[obs.length - 1].t : Date.now());
    // Anchor on the series, not the wall clock: a stale feed must not slide the
    // whole chart leftward and imply readings that never arrived.
    const anchor = obs.length ? obs[obs.length - 1].t : Date.now();

    let timer = null, stopped = false;
    const clipId = 'clip' + Math.random().toString(36).slice(2, 9);

    let drawnOnce = false;

    function frame() {
      if (stopped) return;
      if (timer) { clearTimeout(timer); timer = null; }
      // A hidden tab stops rescheduling entirely; visibilitychange restarts it.
      // The very first paint is exempt: pausing before it leaves the chart
      // stuck on its loading text for anyone whose tab was not focused when
      // the data arrived.
      if (paused() && drawnOnce) return;
      // Drift is capped at one forecast horizon so an abandoned tab cannot
      // wander somewhere meaningless.
      const drift = Math.min(Date.now() - anchor, Math.max(0, latest - anchor));
      draw(anchor + (reduceMotion() ? 0 : Math.max(0, drift)));
      drawnOnce = true;
      if (paused() || reduceMotion()) return;
      {
        timer = setTimeout(() => requestAnimationFrame(frame), 1000 / FPS);
      }
    }

    // Redraw on the way back so the chart is current, not stale then jumpy.
    const onVisible = () => { if (!document.hidden && !stopped) frame(); };
    document.addEventListener('visibilitychange', onVisible);

    function draw(nowMs) {
      const x = makeScaleX(nowMs, Math.min(oldest, nowMs - 60000),
        Math.max(latest, nowMs + 60000), pw, telescope);

      const obsPts = obs.map((d) => [x(d.t), y(d.v)]);
      const fcPts = fc.map((f) => [x(f.t), y(f.v)]);
      // Join the forecast to the last observation so the dashed line does not float.
      const joined = obsPts.length ? [obsPts[obsPts.length - 1]].concat(fcPts) : fcPts;

      const bandPath = fc.length
        ? path(fc.map((f) => [x(f.t), y(f.hi)])) + ' ' +
          fc.slice().reverse().map((f) => 'L' + x(f.t).toFixed(1) + ',' + y(f.lo).toFixed(1)).join(' ') + ' Z'
        : '';

      const refLine = (v, cvar, label) => {
        if (v == null || v < lo || v > hi) return '';
        const yy = y(v);
        return '<g class="ref">' +
          '<line x1="0" y1="' + yy.toFixed(1) + '" x2="' + pw + '" y2="' + yy.toFixed(1) +
          '" stroke="var(' + cvar + ')" stroke-width="1.5" stroke-dasharray="5 4" opacity=".9"/>' +
          '<text x="' + (pw - 4) + '" y="' + (yy - 5).toFixed(1) + '" text-anchor="end" ' +
          'class="ref-label">' + label + ' ' + v.toFixed(2) + ' m</text></g>';
      };

      const xNow = pw * NOW_FRAC;
      const gridY = ticksY(lo, hi, 5).map((v) =>
        '<line x1="0" y1="' + y(v).toFixed(1) + '" x2="' + pw + '" y2="' + y(v).toFixed(1) + '" class="grid"/>' +
        '<text x="-8" y="' + (y(v) + 3.5).toFixed(1) + '" text-anchor="end" class="axis-label">' +
        v.toFixed(1) + '</text>').join('');

      // Time ticks: in telescope mode label by age, because a clock time on a
      // log axis reads as evenly spaced when it is not.
      const xticks = telescope
        ? [0.25, 1, 4, 12, 48]
            .filter((h) => nowMs - h * 3600000 >= oldest)
            .map((h) => ({ px: x(nowMs - h * 3600000), lab: h < 1 ? (h * 60) + 'm' : h + 'h' }))
        : [0, 0.34, 0.68].map((f) => ({
            px: pw * f,
            lab: clockLabel(oldest + (nowMs - oldest) * (f / NOW_FRAC)),
          }));
      const gridX = xticks.filter((t) => t.px > 2 && t.px < pw - 2).map((t) =>
        '<line x1="' + t.px.toFixed(1) + '" y1="0" x2="' + t.px.toFixed(1) + '" y2="' + ph + '" class="grid"/>' +
        '<text x="' + t.px.toFixed(1) + '" y="' + (ph + 16) + '" text-anchor="middle" class="axis-label">' +
        t.lab + '</text>').join('');

      let crossMark = '';
      const cross = data.crossing;
      if (cross && ms(cross.ts) != null) {
        const cx = x(ms(cross.ts)), cy = y(cross.value);
        if (cx > 0 && cx < pw) {
          crossMark = '<g class="crossing">' +
            '<line x1="' + cx.toFixed(1) + '" y1="' + cy.toFixed(1) + '" x2="' + cx.toFixed(1) +
            '" y2="' + ph + '" stroke="var(--danger)" stroke-width="1" stroke-dasharray="2 3"/>' +
            '<circle cx="' + cx.toFixed(1) + '" cy="' + cy.toFixed(1) +
            '" r="5" fill="var(--danger)" stroke="var(--surface)" stroke-width="2"/></g>';
        }
      }

      const last = obsPts.length ? obsPts[obsPts.length - 1] : null;
      const label = esc(data.name || 'Gauge') + ' water level: ' + obs.length +
        ' readings then a ' + fc.length + '-step forecast';

      el.innerHTML =
        '<svg class="ts-chart" viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="' + label + '">' +
        '<defs><clipPath id="' + clipId + '"><rect x="0" y="0" width="' + pw +
        '" height="' + ph + '"/></clipPath></defs>' +
        '<g transform="translate(' + M.l + ',' + M.t + ')">' +
        gridY + gridX +
        (bandPath ? '<path d="' + bandPath + '" fill="var(--accent)" opacity=".16" ' +
          'clip-path="url(#' + clipId + ')"/>' : '') +
        refLine(warning, '--warning', 'Warning') +
        refLine(danger, '--danger', 'Danger') +
        '<line x1="' + xNow.toFixed(1) + '" y1="0" x2="' + xNow.toFixed(1) + '" y2="' + ph + '" class="now-line"/>' +
        '<text x="' + (xNow + 4).toFixed(1) + '" y="10" class="now-label">now</text>' +
        (obsPts.length > 1 ? '<path d="' + path(obsPts) + '" fill="none" stroke="var(--accent)" ' +
          'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>' : '') +
        (joined.length > 1 ? '<path d="' + path(joined) + '" fill="none" stroke="var(--accent)" ' +
          'stroke-width="2" stroke-dasharray="5 4" opacity=".85"/>' : '') +
        crossMark +
        (last ? '<circle cx="' + last[0].toFixed(1) + '" cy="' + last[1].toFixed(1) +
          '" r="4" fill="var(--accent)" stroke="var(--surface)" stroke-width="2">' +
          // A never-ending SMIL animation keeps the page from ever reaching
          // render-idle, which breaks screenshot capture and is exactly what
          // reduced-motion readers asked not to see.
          (reduceMotion() ? ''
            : '<animate attributeName="r" values="4;6;4" dur="2.4s" repeatCount="indefinite"/>') +
          '</circle>' : '') +
        '<rect class="hit" x="0" y="0" width="' + pw + '" height="' + ph + '" fill="transparent"/>' +
        '<g class="cursor" hidden><line y1="0" y2="' + ph + '" stroke="var(--muted)" stroke-width="1"/>' +
        '<circle r="4.5" fill="var(--accent)" stroke="var(--surface)" stroke-width="2"/></g>' +
        '</g></svg><div class="ts-tip" hidden></div>';

      wireHover(el, { obs, fc, x, y, M, W, H });
    }

    frame();
    return {
      stop() {
        stopped = true;
        if (timer) clearTimeout(timer);
        document.removeEventListener('visibilitychange', onVisible);
      },
    };
  }

  /* Crosshair and tooltip. A line chart without one makes the reader estimate
     values off the gridlines, which is the thing gridlines are worst at. */
  function wireHover(el, ctx) {
    const svg = el.querySelector('svg');
    const tip = el.querySelector('.ts-tip');
    const cursor = el.querySelector('.cursor');
    const hit = el.querySelector('.hit');
    if (!svg || !hit || !tip || !cursor) return;

    const pts = ctx.obs.map((d) => ({ t: d.t, v: d.v, kind: 'observed' }))
      .concat(ctx.fc.map((f) => ({ t: f.t, v: f.v, lo: f.lo, hi: f.hi, kind: 'forecast' })));
    if (!pts.length) return;

    const toLocal = (evt) => {
      const m = svg.getScreenCTM();
      if (!m) return null;
      const p = svg.createSVGPoint();
      p.x = evt.clientX; p.y = evt.clientY;
      const q = p.matrixTransform(m.inverse());
      return { x: q.x - ctx.M.l, y: q.y - ctx.M.t };
    };

    const move = (evt) => {
      const loc = toLocal(evt);
      if (!loc) return;
      let best = null, bd = Infinity;
      for (const p of pts) {
        const d = Math.abs(ctx.x(p.t) - loc.x);
        if (d < bd) { bd = d; best = p; }
      }
      if (!best) return;
      const px = ctx.x(best.t), py = ctx.y(best.v);
      cursor.hidden = false;
      const ln = cursor.querySelector('line'), ci = cursor.querySelector('circle');
      ln.setAttribute('x1', px); ln.setAttribute('x2', px);
      ci.setAttribute('cx', px); ci.setAttribute('cy', py);

      const range = best.kind === 'forecast' && best.lo != null
        ? '<span class="tip-range">80% range ' + best.lo.toFixed(2) + '–' + best.hi.toFixed(2) + ' m</span>'
        : '';
      tip.hidden = false;
      tip.innerHTML = '<b>' + best.v.toFixed(2) + ' m</b>' +
        '<span class="tip-kind">' + best.kind + '</span>' +
        '<span class="tip-time">' + dayLabel(best.t) + '</span>' + range;

      const box = el.getBoundingClientRect();
      const sx = (px + ctx.M.l) / ctx.W * box.width;
      const sy = (py + ctx.M.t) / ctx.H * box.height;
      tip.style.left = Math.min(Math.max(sx, 70), Math.max(70, box.width - 70)) + 'px';
      tip.style.top = Math.max(0, sy - 14) + 'px';
    };

    const leave = () => { cursor.hidden = true; tip.hidden = true; };
    hit.addEventListener('pointermove', move);
    hit.addEventListener('pointerleave', leave);
    hit.addEventListener('pointerdown', move);
  }

  /* ------------------------------------------------------ small multiples */

  /* A thumbnail is a shape, not a readout: no axes, no labels, just the
     trajectory and where it sits relative to the danger mark. */
  function thumb(series, marks) {
    const pts = (series || []).filter((s) => s.value != null);
    if (pts.length < 2) return '<div class="thumb-empty">no series yet</div>';
    const W = 150, H = 40;
    const vals = pts.map((p) => p.value);
    const danger = marks ? marks.danger : null;
    const sy = makeScaleY(vals.concat([danger]), H, 0.18);
    const x = (i) => (i / (pts.length - 1)) * W;
    const d = path(pts.map((p, i) => [x(i), sy.y(p.value)]));
    const dy = danger != null ? sy.y(danger) : null;
    return '<svg class="thumb" viewBox="0 0 ' + W + ' ' + H + '" aria-hidden="true">' +
      (dy != null && dy > 0 && dy < H
        ? '<line x1="0" y1="' + dy.toFixed(1) + '" x2="' + W + '" y2="' + dy.toFixed(1) +
          '" stroke="var(--danger)" stroke-width="1" stroke-dasharray="3 3" opacity=".7"/>' : '') +
      '<path d="' + d + '" fill="none" stroke="var(--accent)" stroke-width="1.75" ' +
      'stroke-linejoin="round" stroke-linecap="round"/>' +
      '<circle cx="' + W + '" cy="' + sy.y(vals[vals.length - 1]).toFixed(1) +
      '" r="2.5" fill="var(--accent)"/></svg>';
  }

  /* The numbers behind the picture. Required so the chart is not the only way
     to read the data — a screen reader gets the table, not the path geometry. */
  function table(data) {
    const rows = (data.observed || []).slice(-12)
      .map((o) => '<tr><td>' + dayLabel(ms(o.ts)) + '</td><td>' +
        Number(o.value).toFixed(2) + '</td><td>observed</td></tr>')
      .concat((data.forecast || []).map((f) => '<tr><td>' + dayLabel(ms(f.ts)) +
        '</td><td>' + Number(f.value).toFixed(2) + '</td><td>forecast (' +
        Number(f.lower).toFixed(2) + '–' + Number(f.upper).toFixed(2) + ')</td></tr>'));
    return '<table class="ts-table"><caption>Last 12 readings and the forecast, in metres</caption>' +
      '<thead><tr><th>Time</th><th>Level (m)</th><th>Source</th></tr></thead>' +
      '<tbody>' + rows.join('') + '</tbody></table>';
  }

  return { timeSeries, thumb, table, makeScaleX, makeScaleY, ticksY, _ms: ms };
})();
