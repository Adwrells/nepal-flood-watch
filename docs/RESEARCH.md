# Research notes

Techniques from the literature that Nepal Flood Watch could adopt, and what
adopting them would cost.

## What belongs here

A paper earns an entry by offering something this system could **actually use**:
a method, a threshold, a validation trick, or a failure mode worth guarding
against. This is not a bibliography and not a literature review. If the takeaway
is "interesting but inapplicable here", it does not go in.

Each entry follows the same three-part shape, because a technique without its
cost is not a decision:

> **Technique** — what the paper proposes, in one or two sentences.
> **What it would change here** — the specific file, constant or model it touches.
> **What it would cost** — the data, compute or complexity it demands, and
> honestly, whether Nepal Flood Watch has that data at all.

## How this file gets written

**On request only.** It is not topped up as a side effect of feature work.
Sections stay empty until there is something worth putting in them.

## Where the current choices came from

The modelling decisions already in the codebase were reasoned from first
principles and from backtests against DHM's own readings, not sourced from
papers. They are listed here as the things the literature should be used to
challenge:

| Decision | Where | Why it is currently this way |
|---|---|---|
| Damped Holt, φ = 0.50, with SNR trend shrinkage | `backend/app/analytics.py` | Undamped extrapolation backtested **72% worse than persistence** (MAE 0.0597 vs 0.0343) across 169 gauges |
| FSI weights 0.50 level / 0.25 rise / 0.18 rain / 0.07 corroboration | `backend/app/scoring.py` | Hand-set so stage dominates; never fitted to outcomes, because there is no labelled flood/no-flood record to fit against |
| Froehlich (1995b) and Costa & Schuster (1988) peak discharge | `backend/app/hazards/outburst.py` | Standard empirical breach relations; both reported so their disagreement is visible |
| Ermini & Casagli DBI for barrier stability | `backend/app/hazards/outburst.py` | Pinned in tests to Tangjiashan (Wenchuan 2008), which really did need an emergency spillway |
| Impoundment floors: 0.50 m baseline, 0.30 m drop | `backend/app/hazards/outburst.py` | Added after the detector fired on a 0.108 m stream |
| 80% prediction interval widened by √h | `backend/app/analytics.py` | In-sample residuals; the simplest defensible growth in uncertainty with lead time |

## Open questions the literature might answer

Written down now so the search has a target when it happens:

1. **Gauge-sparse forecasting.** 188 of 309 gauges carry enough history to plot;
   many report irregularly. What do operational systems do for a gauge with
   four readings a day?
2. **Landslide-dam outburst timing.** The physics here estimates peak discharge
   and travel time, but not *when* a barrier fails. Is a defensible lead-time
   estimate possible from stage behaviour alone?
3. **Verification for rare events.** Skill score against persistence is the
   current yardstick. What scores are actually used where the event of interest
   is in the tail?
4. **Corroboration weighting.** Basin coherence currently requires ≥2 agreeing
   gauges. Is there a principled way to weight that by distance and travel time?
5. **Whether ML is justified at all here.** Gradient boosting and random forest
   are implemented and registered but **not enabled**, because neither has beaten
   persistence on this data. What sample size and feature set would change that?

## Entries

*First pass, 2026-09-01. Five techniques, each of which touches a decision
already made in this codebase.*

### 1. Anomaly persistence is a harder benchmark than the one we use

Ghimire & Krajewski, *Exploring Persistence in Streamflow Forecasting*, JAWRA
2020. 140 USGS gauges, 15-minute data, 2008–2017, basins from 7 to 37,000 km².

> **Technique** — compare three persistence baselines, not one: *simple*
> (tomorrow equals today), *gradient* (today's rate of change continues), and
> *anomaly* (the departure from the seasonal norm persists, the norm itself does
> not). Anomaly persistence scored highest, **especially on basins under
> ~500 km²**. Both persistence schemes beat climatology out to about four days,
> after which the gap closes.
>
> **What it would change here** — `backend/app/models/estimators.py` has a
> `Persistence` forecaster and `_backtest` scores everything against it. That is
> *simple* persistence, the weakest of the three. Our headline claim — "damped
> Holt beats persistence" — is measured against the easiest bar available. Adding
> anomaly persistence to `REGISTRY` would re-test that claim honestly, and most
> of Nepal's gauged catchments are small, which is exactly where the paper says
> the difference bites.
>
> **What it would cost** — an anomaly baseline needs a seasonal norm per gauge.
> We hold 90 days of readings, nowhere near the multi-year record needed to
> estimate one. Implementable as written only for the handful of gauges with a
> long DHM archive; otherwise it needs a climatology we do not have. **Worth
> doing as an honesty check on the benchmark, not as a shipped model.**

### 2. Our skill score degenerates on exactly the events we care about

Ferro & Stephenson, *Extremal Dependence Indices*, Weather and Forecasting 26(5),
2011.

> **Technique** — conventional binary skill scores collapse toward trivial
> values as an event gets rarer. The **Symmetric Extremal Dependence Index
> (SEDI)** is built from hit rate and false-alarm rate only, making it
> *base-rate independent*, non-degenerating, and hard to hedge. It stays
> discriminating below ~2.5% prevalence, where TSS decays into the hit rate and
> MCC suffers denominator suppression.
>
> **What it would change here** — `forecast_skill` in `backend/app/analytics.py`
> reports `1 − MAE_model/MAE_persistence`. MAE over all readings is dominated by
> the ordinary calm majority: a model can post a good skill score while missing
> every band crossing. SEDI over the binary event "crossed the warning mark
> within 6 h" would measure the thing the console is actually for.
>
> **What it would cost** — cheap arithmetic, but it needs enough observed
> crossings to be meaningful. In the current window there are almost none, which
> is itself the finding: **we cannot presently verify the alerts, only the
> levels.** That gap belongs in `docs/ANALYTICS.md` regardless of whether SEDI
> is implemented.

### 3. Transboundary travel times may be far shorter than we assume

Zheng et al., *Glacial lake outburst flood hazard under current and future
conditions: worst-case scenarios in a transboundary Himalayan basin*, NHESS 22,
3765–3788, 2022. Simulated with `r.avaflow`.

> **Technique** — model the whole chain (rock/ice avalanche → lake impact →
> breach → downstream propagation) as a multi-phase mass flow rather than a
> breach hydrograph routed downstream.
>
> **What it would change here** — the reported numbers are a direct challenge to
> `backend/app/hazards/outburst.py`. Arrival at Nyalam in **5–11 minutes**; the
> Nepal border in roughly **30 minutes**; worst-case peak discharge at Zhangmu of
> **35,000–170,000 m³/s**, which the authors note is *more than 15 times* earlier
> estimates. Our Manning celerity `c = (5/3)V` and exponential attenuation
> `Q(x) = Qp·e^(−0.008x)` were never checked against a cascading multi-phase
> event, and the paper's central point is operational: for a transboundary
> basin, the warning has to cross an international boundary before anyone can
> act, and minutes decide it.
>
> **What it would cost** — running `r.avaflow` is far outside this project's
> scope. The cheap and worthwhile part is **using these figures as a sanity
> bound**: if our model returns a travel time for a Bhote Koshi / Trishuli
> headwater scenario that is wildly longer than ~30 minutes to the border, our
> model is wrong, and the reference scenario should say so.

### 4. Landslide-dam longevity is predictable, which our model does not attempt

Multiple recent papers, most directly *Longevity prediction and influencing
factor analysis of landslide dams*, Engineering Geology, 2023, and *Machine
learning-based risk level prediction of landslide dams considering stability,
longevity and breach peak flow*, Landslides, 2025.

> **Technique** — beyond a stability index, fit **longevity** (how long the
> barrier survives) on catchment area, dam height, length, width, and peak
> breach discharge. The 2025 paper chains it: stability probability first, then
> longevity and breach peak flow for the dams judged unstable.
>
> **What it would change here** — `stability_index` returns an Ermini & Casagli
> DBI verdict, i.e. *whether* a barrier is likely to fail, never *when*. Open
> question 2 in this file asks exactly that. A longevity estimate would turn the
> impoundment watch from a standing flag into a countdown.
>
> **What it would cost** — every published formula needs dam geometry (height,
> width, length, lake volume). We infer impoundment from a **stage anomaly at a
> downstream gauge** and have none of those dimensions; nobody has surveyed the
> barrier. Honest position: **not implementable from our inputs**, and worth
> recording as a reason the impoundment module stops where it does.

### 5. Satellite precipitation is the standard answer to our gauge sparsity

SERVIR-HKH / ICIMOD operational work, and *Quantifying the Added Values of a
Merged Precipitation Product in Streamflow Prediction over the Central
Himalayas*, Remote Sensing 17, 2170, 2025.

> **Technique** — where gauge networks are too sparse to represent rainfall over
> complex terrain, substitute or merge **satellite precipitation products**
> (SPPs), then bias-correct against whatever gauges exist. The merged-product
> paper reports measurable gains in streamflow prediction over the Central
> Himalayas specifically.
>
> **What it would change here** — rainfall enters the FSI at weight 0.18 from
> DHM station values, so gauges with no nearby rain station carry a rainfall
> component built on very little. An SPP would give every catchment a rainfall
> estimate rather than only the well-instrumented ones.
>
> **What it would cost** — a new external dependency with its own latency and
> failure modes, on a `t3.micro` with ~913 Mi of RAM, plus real bias-correction
> work. SPPs are also weakest for short-duration convective rain in steep
> terrain, which is a substantial share of what causes flash floods here. **Not
> a free win**; worth a spike, not a rewrite.

## Sources

- [Exploring Persistence in Streamflow Forecasting — JAWRA 2020](https://onlinelibrary.wiley.com/doi/abs/10.1111/1752-1688.12821)
- [Benchmarking Real-Time Streamflow Forecast Skill in the Himalayan Region — Forecasting 2020](https://www.mdpi.com/2571-9394/2/3/13)
- [Extremal Dependence Indices — Weather and Forecasting 2011](https://journals.ametsoc.org/view/journals/wefo/26/5/waf-d-10-05030_1.xml)
- [GLOF worst-case scenarios in a transboundary Himalayan basin — NHESS 2022](https://nhess.copernicus.org/articles/22/3765/2022/nhess-22-3765-2022.html)
- [Longevity prediction and influencing factor analysis of landslide dams — Engineering Geology 2023](https://www.sciencedirect.com/science/article/abs/pii/S0013795223003526)
- [ML-based risk level prediction of landslide dams — Landslides 2025](https://link.springer.com/article/10.1007/s10346-025-02517-8)
- [Recent catastrophic landslide lake outburst floods in the Himalayan mountain range — Progress in Physical Geography 2017](https://journals.sagepub.com/doi/10.1177/0309133316658614)
- [Merged Precipitation Product in Streamflow Prediction over the Central Himalayas — Remote Sensing 2025](https://doi.org/10.3390/rs17132170)
- [Combining ground and satellite data to forecast flood in Nepal — SERVIR-HKH](https://servir.icimod.org/news/combining-ground-and-satellite-data-to-forecast-flood-in-nepal/)
