# Changelog

All notable changes to Nepal Flood Watch. Versions follow
[semantic versioning](https://semver.org/); dates are ISO.

Every entry that fixes a defect says **how the defect was found**, because on
this project that has turned out to be the more useful half of the record.

---

## [v1.3.0] — 2026-09-01

Time-series charts for every river, and a correction to the strongest number
the system reports.

### Added

**Charts for all 309 gauges.** A Charts tab holding one small multiple per
river, severity-ranked and filterable, plus a floating window that opens on
click with the full series, the 12-hour forecast, its 80% prediction interval,
and DHM's published warning and danger marks as labelled reference lines.

![Charts tab](docs/images/charts-tab.png)

**All four analytic layers in one place.** The floating window stacks
descriptive → diagnostic → predictive → prescriptive beneath the chart, in the
order an operator needs them.

![Floating chart window](docs/images/chart-window.png)

**A telescope time axis.** Optional logarithmic-in-age x-axis: the last minutes
stay wide while older readings contract leftward without falling off, so days of
history and the last quarter-hour share one plot. Labelled by age (48h · 12h ·
4h · 1h · 15m) rather than clock time, and carrying an on-screen note that a log
axis makes slopes in the compressed region look steeper than they are. Opt-in,
never the default.

![Telescope view](docs/images/chart-telescope.png)

The drift animation falls out of the scale — both modes anchor on a moving
clock — so it needs no separate animation system. It pauses on hidden tabs and
stops under `prefers-reduced-motion`.

**New endpoints.** `GET /api/station/{id}/analysis` (chart-ready payload with
absolute timestamps and a pre-computed danger crossing) and
`GET /api/charts/index` (every gauge's recent series in one call).

**`docs/RESEARCH.md`** — techniques from the literature that touch decisions
already made here, each with what it would change and what it would cost.

**`tools/screenshots.py`** — the README images are now reproducible instead of
hand-captured.

### Fixed

**P(danger in 6 h) reported false certainty.** The probability was computed from
a single pair of readings with no allowance for how noisy that pair was. Kankai
at Mainachuli produced pairwise rise rates from −0.562 to +0.775 m/h *within one
hour*; the same function returned **0% at one end and 100% at the other**, and
the console displayed 100% for a river that did not flood. The logistic scale
now carries rate uncertainty measured from the gauge's own recent spread,
propagated over the full six hours. On that gauge the range collapses from
[0%, 100%] to [10%, 82%]; a clean, steadily climbing gauge still reaches >95%.

*Found by reading a screenshot taken for this release and not believing the
number.*

**Two contradictory numbers shown without explanation.** 35 gauges advertised a
countdown to danger while their forecast line stayed flat and the panel said
danger was never reached. These are different methods — straight-line
extrapolation versus the damped, shrunk forecast — and the linear one is exactly
what backtested 72% worse than persistence. Relabelled *"X h at this rate"*, with
the divergence explained in the predictive panel.

*Found by comparing the card badge against the chart beside it.*

**`/api/charts/index` ran an N+1 query.** One history query per gauge: 3.9 s for
309 gauges. A single windowed query returns the same data in 248 ms.

*Found by timing it.*

**The danger-crossing annotation never fired.** Every gauge in the database was
calm, so a dead branch and a working one were indistinguishable. Now pinned in
both directions by synthetic rising and falling gauges.

*Found by asking how many gauges exercised the branch, and getting zero.*

**Charts never rendered in a background tab.** The visibility pause ran before
the first paint, leaving the window on its loading text for anyone whose tab was
not focused when the data arrived.

*Found by a headless capture, which is always an unfocused tab.*

**A hidden tooltip rendered as an empty pill.** `display: grid` outranked the
user agent's `[hidden]` rule.

*Found in a screenshot.*

**Static assets sent no cache headers.** The server auto-deploys on push, so a
browser holding a heuristically cached `app.js` would run last week's frontend
against this week's API. Now `no-cache, must-revalidate`.

### Changed

- SQL in the bulk history query is fully static — no interpolated `IN` clause —
  so it needs no scanner suppression and no re-audit on reading.
- Chart grid drops to a 145px minimum, giving two columns on phones.

### Tests

128 passing, up from 103. New: `backend/tests/test_charts.py` (16 cases on the
chart payload) and `TestExceedanceConfidence` in `test_scoring.py` (9 cases
pinning the probability fix, including monotonicity — more noise may never
increase confidence).

---

## [v1.2.0] — 2026-08-30

Mobile. The console was unusable at 375px: six distinct layout failures
including a Continue button clipped off-screen. The gauge list and map overlays
now collapse on phones so the map is above the fold.

Also: a version marker, and private keys plus deployment notes moved into
`.gitignore` — `*.pem`, `*.key`, `id_rsa*` are ignored wherever they land.

---

## [v1.1.0] — 2026-08-30

Machine learning scaffolding and the analytics ladder.

`GradientBoosting`, `RandomForest` and `RidgeLinear` registered behind a
`Forecaster` protocol, with `/api/models/bakeoff` training and scoring every
model on the same live data. **None are enabled**: none beat persistence, and
shipping a model that loses to "assume no change" would be worse than shipping
nothing.

Live backtesting via `/api/forecast/skill`, the full cleaning → descriptive →
diagnostic → predictive → prescriptive pipeline, and CodeQL on push, PR and
weekly with `security-extended`.

---

## [v1.0.0] — 2026-08-30

First public release.

309 DHM river gauges on a 12-minute cycle. Flood Severity Index
(`0.50·level + 0.25·rise + 0.18·rain + 0.07·corroboration`) with five severity
bands. Damped Holt forecasting with signal-to-noise trend shrinkage — added
after straight-line extrapolation measured **72% worse than persistence** across
169 gauges.

Landslide- and moraine-dammed lake outburst physics (Froehlich 1995b, Costa &
Schuster 1988, Ermini & Casagli DBI), modelled on the Rasuwa 2025 event. The
length-of-day calculation for the claim that Chinese dams affect Earth's
rotation. Verified emergency numbers, 16,295 health facilities, official relief
channels. Leaflet map, SSE live push, six-sheet Excel export, FastAPI + SQLite,
no build step.

---

[v1.3.0]: https://github.com/Adwrells/nepal-flood-watch/releases/tag/v1.3.0
[v1.2.0]: https://github.com/Adwrells/nepal-flood-watch/releases/tag/v1.2.0
[v1.1.0]: https://github.com/Adwrells/nepal-flood-watch/releases/tag/v1.1.0
[v1.0.0]: https://github.com/Adwrells/nepal-flood-watch/releases/tag/v1.0.0
