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
individually: **958 buildings, 118.75 GWh site energy, 45.04 kWh/m²**.

The area-weighted intensity is what can be compared against the reference city
model, and the largest cluster — 460 buildings, 40 % of the district's floor area
— lands at **+1.0 %** of the published figure for that typology.

```
BlocPluriP04   460 buildings   47.48 kWh/m²   ref 47   +1.0 %
BlocPluriP03    83             50.93          ref 52   −2.0 %
BlocPluriP05   206             42.72          ref 47   −9.1 %
BlocPluriP06    27             36.12          ref 45   −19.7 %
EdiPluriP03     23             67.08          ref 52   +29.0 %
EdiPluriP01      3             79.90          ref 54   +48.0 %
```

Two things in that table are worth stating rather than smoothing over.

### The deviation is the representative method's own error, and it is measurable

The clusters do not deviate at random. Sorted by the median storey count of the
buildings in them, the deviation from the reference constant is monotonic:

| Median storeys | Clusters | Deviation |
|---:|---|---:|
| 1 | EdiPluriP01 | **+54.1 %** |
| 2 | EdiPluriP03, P05 | +33 … +38 % |
| 3–5 | BlocPluriP02, P03, P04, P05 | −3 … +3 % |
| 6 | BlocPluriP06, P07 | −15 … −23 % |

Correlation between storey count and deviation across the 11 clusters that have
a reference value: **−0.80**.

Envelope quality is not the explanation, and a natural experiment in the data
rules it out. Two clusters share the same wall U of 2.56 W/m²K:

```
BlocPluriP02   wall U 2.56   3 storeys    +0.9 %
EdiPluriP01    wall U 2.56   1 storey    +54.1 %
```

Same envelope, 53 percentage points apart. A second pair at U ≈ 0.5 points the
same way: 6 storeys −15.4 %, 2 storeys −2.5 %.

What separates them is surface-to-volume. A single-storey building has far more
envelope per square metre of floor than a five-storey block, so a reference value
computed from a multi-storey representative under-predicts a low-rise cluster and
over-predicts a high-rise one. The deviation crosses zero at four to five
storeys — which is where the reference models sit. The supervisor has since
confirmed that the reference model behind one of these clusters was a five-storey
test box with no counterpart in the cadastre at all.

So this is not an error in the per-building results. It is the size and sign of
the error the representative-model method carries *within* a cluster, made
visible by simulating the buildings individually. That is precisely what the
per-building approach was built to find.

Honest limits: eleven clusters; the representative geometry is confirmed for one
of them and inferred for the rest; and that the reference column holds
representative-model output at all is an inference from its structure — thirteen
distinct values across 26,452 buildings, one constant per cluster — not a
statement from its authors.

**Coverage is stated, not implied.** 958 of 1,012 buildings in scope, which is
94.66 % of buildings but **86.33 %** of the footprint — the geometry gates fall
hardest on large, complex buildings. So the 118.75 GWh figure is the energy of
the modellable district, not of the district. The intensity is a ratio and is
unaffected, which is why the comparison above still holds.

The 53 exclusions and the 1 failure are all in the ledger with reasons. The
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
