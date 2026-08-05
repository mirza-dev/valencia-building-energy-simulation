# Results

These are outputs of this pipeline, not the input datasets. The inputs belong to
the UPV research group and are not redistributed here — see the root README.

## `verification_report.json`

The 13-gate scoring of our reconstruction of the supervisor's own building
against the numbers he published, together with the profile fingerprint and the
SHA-256 of every locked source file that produced it.

Reproduce with:

```bash
python src/verified_model.py --verify
```

The three differences that were measured and deliberately left open — the
EnergyPlus version gap, roof solar reflectance, and window distribution — are
recorded in `src/verified_model.py` under `KNOWN_DIFFERENCES_FROM_RAI`, each with
the measurement behind it.

## `cluster_representatives.json`

One median-sized building simulated from each of the 21 TABULA typology clusters
that make up the Valencia stock — the staged gate that runs before committing
hours to a district.

This is also the evidence for a defect worth describing. Before the fix, this run
produced **1 distinct wall construction and 1 distinct roof** across all 21
clusters: the envelope resolver existed and was correct, but only the UI called
it, so the stock chain handed every building the same default. The clusters
differed by label, geometry and occupancy — but not by period envelope.

After the fix: **11 distinct walls, 12 distinct roofs**. The remaining duplicates
are the typology table's own (two periods share values, and P07 maps onto P06).
The physics now separates by construction period, insulated post-1980 clusters
landing at 34–46 kWh/m² against 50–103 for bare pre-war masonry.

Every row carries the run identity that produced it: profile fingerprint,
climate, template, data policy, source-data hash and runner schema.

## `benicalap_district.json`

Every modellable building in the Benicalap district of Valencia, simulated
individually: **967 buildings, 137.44 GWh site energy**, at **58.73 kWh/m²** of
the floor area the model conditions.

**Which square metres.** That intensity is per *geometric residential storey
area* — footprint × the storeys simulated as housing. The reference city model's
per-cluster constants are per *cadastral dwelling area* (its own code sums
`442_sfc` over the dwelling records of each parcel), and the two are not the same
quantity. On the cadastral basis the same energy is **63.69 kWh/m²**. Every
comparison below is on the cadastral basis, because that is the basis the
reference is defined on.

```
                 n     geo   cadastral    ref    Δ vs ref   energy
BlocPluriP04   460   55.40      59.45   46.55     +27.7 %    1.28×
BlocPluriP05   209   56.54      58.66   46.59     +25.9 %    1.26×
BlocPluriP03    83   58.04      68.06   51.71     +31.6 %    1.32×
VivUniP02       55   65.94      94.28   55.82     +68.9 %    1.69×
BlocPluriP06    33   75.81      91.08   45.00    +102.4 %    2.02×
EdiPluriP02     24   61.95      78.19   52.02     +50.3 %    1.50×
```

`energy` is the ratio of our kWh to the kWh the reference method would assign to
exactly these buildings (`constant × cadastral area`). It is basis-free, and it
is the honest headline: **district-wide we are 1.37× the reference**.

An earlier version of this page reported the largest cluster at **+1.0 %** of the
reference. That number was real but it was an intensity coincidence, not an
energy agreement: it divided our energy by a floor area 59 % larger than the
cadastral area the reference constant is meant to multiply. Correcting the floor
area (see below) and putting the comparison on the reference's own basis moves
that cluster to +27.7 %. The gap was always there; the denominator hid it.

Two things in that table are worth stating rather than smoothing over.

### A retracted claim: the deviation was mostly ours, not the method's

An earlier version of this page reported that deviation from the reference was
monotonic in storey count (Spearman **−0.80** across 11 clusters) and attributed
it to the representative-model method's surface-to-volume error — a reference
computed from a multi-storey box under-predicting a low-rise cluster. **That
attribution does not survive contact with the reference's own documentation.**

The author's thesis, obtained 2026-08-04, states the reference geometry directly:
there are **three** models, not one per cluster, and the seven periods of a
typology share a geometry.

| Typology | Reference model | Storeys |
|---|---:|---:|
| VivUni | 214 m² | **2** |
| EdiPluri | 435 m² | **2** |
| BlocPluri | 2 310 m² | **6** |

The page previously said the deviation "crosses zero at four to five storeys —
which is where the reference models sit." They sit at 2, 2 and 6. If the
surface-to-volume mechanism were real, the deviation would track the *gap*
between a cluster's storey count and the storeys of the reference model for its
typology. Measured across every configuration we could build:

| Run · reference · clusters | vs absolute storeys | vs **gap to reference** |
|---|---:|---:|
| old run · shapefile · no VivUni *(as published)* | −0.832 (p = 0.001) | −0.070 (p = 0.84) |
| current run · shapefile · no VivUni | −0.648 (p = 0.031) | −0.154 (p = 0.65) |
| **current run · thesis reference · all clusters** | **−0.224 (p = 0.37)** | **−0.032 (p = 0.90)** |

The published −0.80 reproduces (−0.832), so the original measurement was sound.
It does not survive two corrections to **our own** work:

* **The ground floor.** 37 % of Valencia's buildings hold a dwelling at ground
  level, and they are disproportionately the low-rise ones. Excluding that floor
  from the area basis inflated exactly the low-rise clusters' intensity — which
  is the pattern the −0.80 was measuring.
* **Seven missing clusters.** The reference table omitted all VivUni clusters —
  5 262 buildings, 20 % of the stock — not for any reason, but because they were
  never entered. Restoring them halves what is left of the correlation.

The claimed mechanism has a correlation of −0.03 with the quantity it predicts.
What remains is a per-cluster deviation of +26 % to +102 % whose cause is **not
established**; the envelope is now the leading candidate, since the reference's
own wall constructions run 15–60 % lower in U than the ones derived here.

Honest limits: eighteen clusters in this district; the reference constants are
the thesis's own per-cluster figures (Ilustración 31), and two values in the
published shapefile disagree with them and are not used.

### Not every storey under a dwelling is a dwelling

`altura_max` records the highest floor that holds a dwelling. It does not say the
floors beneath it are housing, and this pipeline used to assume it did — every
storey was built as `Espacio Tipo Vivienda CTE`, with dwelling occupancy,
schedules, thermostat and hot water.

The cadastre disagrees, and the disagreement is structured. Among the district's
BlocPluri buildings, 85 of them carry the same median storey count (5) and the
same dwelling size (104 m² of cadastral area per dwelling, against 96 m² in the
rest) — but **0.37 dwellings per 100 m² of modelled floor instead of 0.98**.
Twenty-five dwellings at ~104 m² is ~2 600 m² of housing in a 6 373 m² building.
The remaining floor is commercial or office, and the cadastre is not
under-reporting it; it simply is not housing. Across the district that was
**842 462 m², 24.6 % of the floor area being simulated as dwellings**.

Storeys the recorded dwelling area cannot fill are now typed as conditioned
tertiary space — the same regime the ground floor already gets — and leave the
residential area basis. 449 buildings, 739 storeys. The effect is worth stating
precisely, because it is not what one might expect: floor area fell 31.7 % but
**energy fell only 2.8 %**. The storeys do not disappear; they stay conditioned
and keep their gains. What the correction fixes is the *denominator*: modelled
residential area now sits within **8.4 %** of the cadastral dwelling area,
against 58.9 % before.

**Coverage is stated, not implied.** 967 of 1 012 buildings in scope — 95.6 % of
buildings and **98.1 %** of the footprint. So the 137.44 GWh figure is the energy
of the modellable district, not of the district. The intensity is not neutral to
that gap either: footprint anti-correlates with EUI among the buildings that did
run, so excluding large buildings biases the subset's intensity upward. Nine
buildings above the single-zone threshold carry 9.2 % of the area and 15.0 % of
the energy on a coarser zoning assumption; that share is reported rather than
buried.

The 44 exclusions and the 1 failure are all in the ledger with reasons. The
failure is a cadastral polygon with a degenerate surface: EnergyPlus refuses it,
and so does this pipeline.

### The footprint ceiling, and why it moved

Reading those exclusions by reason rather than by count showed the gap was almost
entirely one threshold. City-wide, the engine's geometry gate refused 1,468
buildings, but they were not equivalent: the 853 rejected for simplification loss
carried **0.71 %** of the floor area, while the 123 over the 5,000 m² footprint
ceiling carried **11.55 %**.

The ceiling is not an engine limit. It guards a modelling assumption — the chain
gives every storey one well-mixed thermal zone, which weakens on a deep plan
where the core is driven by internal gains rather than by the envelope. The guard
is reasonable; sitting at a value nobody had measured was not.

Raised to 20,000 m² on 2026-08-03:

| Ceiling | Buildings above it | Floor area lost |
|---:|---:|---:|
| 5,000 | 123 | 11.55 % |
| 10,000 | 27 | 3.72 % |
| **20,000** | **4** | **0.55 %** |

Runnable stock goes from 24,984 to **25,102 buildings**, and area coverage from
87.50 % to **98.38 %**. The four still excluded are genuine outliers — the
largest is 32,172 m² over two storeys, which is not a dwelling.

Admitting them without saying so would trade one silent error for another, so
every building above 5,000 m² is flagged in the ledger and `aggregate()` reports
a `zoning` block: how many such buildings a total contains, and what share of its
area and energy they account for. In Benicalap that is 9 buildings — under 1 % of
the count, but **19 % of the district's floor area**, one of them 17,272 m² over
15 storeys.

The verification is unaffected and was re-run to prove it: Rai's building is
954.8 m², far from either ceiling, and all 13 gates came back bit-identical.

## What the fixes were worth

| | one shared envelope | per-cluster envelope | + warmup fix |
|---|---:|---:|---:|
| Buildings | 957 | 947 | **958** |
| Site energy (GWh) | 120.67 | 113.89 | **118.75** |
| Heating (GWh) | 5.66 | 3.78 | **3.81** |
| Intensity (kWh/m²) | 46.19 | 45.33 | **45.04** |
| Footprint coverage | 85.73 % | 83.16 % | **86.33 %** |

Connecting the cluster envelopes cut district **heating by a third** — Benicalap
is mostly post-1980 insulated stock that had been running on a 1974 envelope.

It also broke 10 buildings: heavy pre-war masonry stopped converging inside
EnergyPlus's 25-day warmup ceiling. Raising it to 60 recovered 11 of the 12
failures. That change cannot move a result that already converged — EnergyPlus
stops warming up as soon as the tolerances are met — and the claim was measured
rather than assumed: of the 947 buildings common to both runs, 925 came back
bit-identical and the other 22 moved by at most 0.02 kWh/m².
