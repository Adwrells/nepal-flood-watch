# Analytics: methods, evaluation, and error

How the predictions are made, how they are measured, and where they fail. The
last part is the important one — a warning system that cannot state its own
error is asking to be trusted on faith.

Live numbers: `GET /api/forecast/skill`.

---

## 1. What is predicted

| Output | Horizon | Method | Unit |
|---|---|---|---|
| Stage forecast | 1–12 h | Damped Holt, persistence-anchored, shrunk trend | metres |
| 80% prediction band | 1–12 h | Residual σ × 1.28 × √h | metres |
| P(danger breach) | 6 h | Logistic on projected headroom | probability |
| Time to danger | — | Linear extrapolation of rise rate | hours |
| Flood Severity Index | now | Weighted 4-component blend | 0–100 |

---

## 2. Evaluation method

**Backtest by hold-out.** For every gauge with ≥4 stored readings, hide the most
recent reading, forecast it from the rest, compare with what actually happened.

**The baseline is persistence** — "the level will be what it is now". This is
the correct baseline for river stage at short horizons, and it is a genuinely
hard one to beat. Any model that cannot must not ship, because it adds
confident noise to a decision someone may act on.

```
skill = 1 − MAE_model / MAE_persistence
```

`skill > 0` is better than doing nothing. `skill = 0` is parity. **Negative
means the model is actively harmful.**

---

## 3. Measured results

Over **169 gauges**, one-step hold-out:

| Model | MAE | Skill |
|---|---|---|
| Persistence (baseline) | 0.0343 m | — |
| **Holt as originally shipped** | **0.0597 m** | **−72%** |
| Best parameters found by sweep (unanchored) | 0.0353 m | −2% |
| **Current: anchored + shrunk trend** | **0.0344 m** | **−0.2%** |

### The original model was worse than doing nothing

That is not a rounding issue — 72% worse. It shipped, and it was only caught by
backtesting it. Two causes:

1. **Holt forecasts from its smoothed level, which lags the last observation by
   construction.** On river stage the most recent reading is the single best
   estimate of the next one, so starting from a lagged value gives away accuracy
   before the trend term does anything at all.
2. **With 5–10 irregularly spaced readings the estimated trend is mostly
   noise**, and an unshrunk trend term amplifies noise into confident error.

### The fixes

**Anchor on the observation, not the smoothed level:**

```
ŷ(t+h) = y(t) + Σφʰ · trend
```

**Shrink the trend toward zero by its signal-to-noise ratio:**

```
trend_used = trend · trend² / (trend² + σ²)
```

Ordinary shrinkage. A slope large relative to residual scatter passes through
almost untouched; a slope the same size as the scatter is halved; a smaller one
effectively vanishes and the forecast degrades to persistence — which is the
honest answer for a flat river.

### Why parity is the right result, not a failure

This sample is mostly quiet-weather rivers, which is precisely when **there is
no trend to find**. A model that "beat" persistence here would be fitting noise.
The trend term earns its place in the case the system exists for — a gauge
genuinely rising — and the shrinkage is what stops it inventing that case the
rest of the time.

**Parity is the floor, not the goal.** As history accumulates (the database is
days old; median ≈ 8 readings per gauge) the skill number should be re-checked.
If it goes negative, that is a regression and `tests/test_scoring.py` fails.

---

## 4. Calibration of the uncertainty band

The 80% band is built from in-sample one-step residuals widened by √h.
Measured coverage: **89% of outcomes fell inside the nominal 80% band.**

The band is therefore **conservative** — slightly too wide. That is the correct
direction to be wrong in for a warning system: a band that is too narrow
understates risk and invites false confidence. It is honest about being an
empirical band, not a distributional one — there is no normality assumption
worth defending on 8 data points.

---

## 5. Known error sources

| Source | Effect | Handling |
|---|---|---|
| **Irregular sampling** | DHM posts roughly hourly; the cycle runs every 12 min. "One step" is not a fixed duration | Rate is computed in m/h from actual timestamps, not per step. The forecast horizon is nominal |
| **Short history** | Trend estimates are noise-dominated | Shrinkage; `confidence` reported as low/moderate/high by sample size; `insufficient-history` returned below 3 readings |
| **Sensor faults** | Stuck floats, datum resets, debris | Jumps > 3 m/h rejected; basin coherence flags a gauge rising alone |
| **Missing danger marks** | 135 of 309 gauges publish one | P(breach) returns 0 rather than guessing a mark |
| **Telemetry gaps** | Silence during rain reads as calm | `staleness()` and `silence_is_suspicious()` treat a gap during rain as weak positive signal |
| **No rating curve** | Stage is not discharge; the relationship is non-linear and site-specific | Everything is expressed in stage, never converted to flow |
| **Rainfall is modelled, not observed** | Open-Meteo is a reanalysis/forecast product, not the gauge's own rain gauge | Weighted 18%, and forecast rain weighted 1.2× observed since it has not reached the channel |

---

## 6. What is deliberately not modelled

**No machine learning.** With ~300 gauges reporting irregularly and **no
labelled flood outcomes**, a learned model could not be validated — only
demonstrated. Every number here traces to a published relation or a stated
weight, and the backtest above is the whole basis for believing any of it.

**No rating curves.** Converting stage to discharge needs per-site calibration
that is not public. Inventing one would put a plausible-looking number on a
guess.

**No sub-daily rainfall disaggregation.** Open-Meteo gives hourly; that is
already finer than the gauge reporting interval, so refining it would be
false precision.

---

## 7. Reproducing the evaluation

```bash
curl -s localhost:8000/api/forecast/skill | jq
```

```bash
cd backend && python -m pytest tests/ -k "Forecast or Hydrological" -v
```

The regression guard (`test_forecast_never_loses_to_persistence`) fails the
build if a future change makes the model worse than doing nothing again.
