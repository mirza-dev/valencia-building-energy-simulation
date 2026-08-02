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

**The EdiPluri deviation did not close.** Before the envelope fix, these clusters
also ran +27…+50 % high, and the obvious reading was that the shared envelope was
to blame. It was not: with each cluster now carrying its own period envelope, the
deviation is still there. That makes it evidence about the *cluster labelling*
rather than about the physics — the reference model's `EdiPluriP04` is a five
storey building, while no building in the cadastre's EdiPluriP04 cluster has more
than two. That question is with the supervisors.

**Coverage is stated, not implied.** 958 of 1,012 buildings in scope, which is
94.66 % of buildings but **86.33 %** of the footprint — the geometry gates fall
hardest on large, complex buildings. So the 118.75 GWh figure is the energy of
the modellable district, not of the district. The intensity is a ratio and is
unaffected, which is why the comparison above still holds.

The 53 exclusions and the 1 failure are all in the ledger with reasons. The
failure is a cadastral polygon with a degenerate surface: EnergyPlus refuses it,
and so does this pipeline.

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
