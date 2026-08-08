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
individually: **967 buildings, 109.76 GWh site energy**, at **50.87 kWh/m²** of
cadastral dwelling area.

**Which square metres.** Three different areas appear below and they are not
interchangeable:

| | m² | what it is |
|---|---:|---|
| cadastral dwelling | 2 157 843 | `442_sfc` summed over the parcel's dwelling records — the basis the reference constants are defined on |
| geometric residential | 2 340 150 | footprint × the storeys simulated as housing (+8.4 % on the cadastral one) |
| conditioned | 2 757 102 | every zone the model actually heats and lights, commercial storeys included |

The reference divides **all** of a building's energy — its ground commercial
storey included — by dwelling area alone. That is its convention, so every
comparison here follows it. It is an accounting ratio, not a physical intensity;
the physical one is `residential_total_site_kwh_m2` = **42.23 kWh/m²**.

```
                 n     geo   cadastral    ref    Δ vs ref   energy
BlocPluriP04   460   50.71      54.42   46.55     +16.9 %    1.17×
BlocPluriP05   209   45.00      46.70   46.59      +0.2 %    1.00×
BlocPluriP03    83   51.70      60.62   51.71     +17.2 %    1.17×
VivUniP02       55   51.49      73.62   55.82     +31.9 %    1.32×
BlocPluriP06    33   39.12      46.99   45.00      +4.4 %    1.04×
EdiPluriP02     24   51.91      65.52   52.02     +26.0 %    1.26×
VivUniP05       23   35.89      36.73   42.77     −14.1 %    0.86×
EdiPluriP03     23   46.05      62.33   52.04     +19.8 %    1.20×
VivUniP03       22   50.89      66.17   54.56     +21.3 %    1.21×
```

`energy` is the ratio of our kWh to the kWh the reference method would assign to
exactly these buildings (`constant × cadastral area`). It is basis-free, and it
is the honest headline: **district-wide we are 1.09× the reference**.

### A second retracted claim: the gap was mostly ours

An earlier version of this page reported **1.37×** district-wide and, for the
largest cluster, **+27.7 %** — adding that an even earlier **+1.0 %** had been an
"intensity coincidence" and that "the gap was always there; the denominator hid
it." Measured against the engine's own output, that reading was wrong in the same
way the number it replaced was: it attributed to the method a gap that was
this pipeline's own accounting.

Two defects were behind it, and they compound.

* **The model conditioned floor area the cadastre does not record.** The builder
  extrudes footprint × `altura_max`. On a parcel covering a whole block those two
  inputs describe different things — the footprint is the block's, `altura_max`
  is its tallest point — so the product is floor that does not exist. District
  wide the model conditioned **1.78×** the recorded dwelling area.
* **The numerator and the denominator described different buildings.** All of
  that floor's energy was divided by dwelling area alone.

Sorting the buildings by how much more floor the model conditions than the
cadastre records shows the whole effect, and it is close to monotonic:

| conditioned ÷ cadastral | n | cadastral EUI | ref | Δ |
|---|---:|---:|---:|---:|
| ≤ 1.00 | 76 | 39.29 | 46.45 | **−15.4 %** |
| 1.00–1.35 *(the reference's own regime)* | 488 | 49.10 | 46.72 | **+5.1 %** |
| 1.35–1.8 | 304 | 55.09 | 46.37 | +18.8 % |
| 1.8–3.0 | 83 | 77.01 | 47.27 | +62.9 % |
| > 3.0 | 16 | 125.79 | 54.69 | +130.0 % |

The one reference building whose EnergyPlus report is published — the
EdiPluriP04 model this pipeline is verified against — is four dwelling storeys
over one commercial one, a ratio of **1.25**. Half this district now sits in the
band around it, and there the deviation is **+5.1 %**. Where the model still
carries more floor than that regime implies, it still reads high: the residual
deviation correlates with the ratio at Spearman **+0.795** (p ≈ 10⁻²¹²). That is
a remaining geometric mismatch. It is not evidence about the envelope, and this
page no longer claims otherwise.

(The thesis gives the three reference geometries as 2, 2 and 6 storeys but does
not say whether they carry a tertiary ground floor, so 1.25 is read off the one
model that was published in full, not assumed for the others.)

For completeness: dividing only the dwellings' own lights, equipment, HVAC and
hot water by dwelling area gives **45.80** against the reference's 46.61,
i.e. **−1.7 %**. That figure is *not* like-for-like — the reference constant has
its own tertiary storey inside it — so it is reported, not led with.

Two more things in that table are worth stating rather than smoothing over.

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
| v6 run · shapefile · no VivUni | −0.648 (p = 0.031) | −0.154 (p = 0.65) |
| v6 run · thesis reference · all clusters | −0.224 (p = 0.37) | −0.032 (p = 0.90) |
| **v8 run · thesis reference · all clusters** | **−0.550 (p = 0.018)** | **+0.025 (p = 0.92)** |

The published −0.80 reproduces (−0.832), so the original measurement was sound.
It does not survive two corrections to **our own** work:

* **The ground floor.** 37 % of Valencia's buildings hold a dwelling at ground
  level, and they are disproportionately the low-rise ones. Excluding that floor
  from the area basis inflated exactly the low-rise clusters' intensity — which
  is the pattern the −0.80 was measuring.
* **Seven missing clusters.** The reference table omitted all VivUni clusters —
  5 262 buildings, 20 % of the stock — not for any reason, but because they were
  never entered.

Across every configuration, the correlation with the quantity the claimed
mechanism actually predicts — the gap between a cluster's height and its
reference model's — stays inside noise: **−0.07, −0.15, −0.03, +0.03**. The
attribution is withdrawn.

The correlation with *absolute* storey count is real and now has a mechanism that
does not involve the reference method at all. The tertiary ground storey is
conditioned but outside the area basis, so it is a larger share of a short
building than of a tall one: median conditioned-to-cadastral ratio runs **1.52**
at one dwelling storey and **1.22** at six. Deviation tracks that ratio at
**+0.795** and storey count at only −0.550 — the height correlation is what the
ratio looks like when sorted by height.

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

The first attempt at this re-typed those storeys as conditioned tertiary space
and took them out of the area basis. That fixed the denominator — modelled
residential area came within **8.4 %** of the cadastral dwelling area, against
58.9 % before — and it fixed nothing else: floor area fell 31.7 % and **energy
fell 2.8 %**. The storeys were renamed, not removed. They were still lit, heated
and conditioned, and their energy still landed in the district total.

The extreme case makes the point. `3748901YJ2734H` is a 17 272 m² parcel at
`altura_max` 15 holding 342 dwellings — 38 158 m² of housing. Extruded whole it
conditions **259 000 m²**, and on its own it carried **5.6 %** of the district's
energy. That is not a mixed-use block; it is a city block whose footprint and
tallest point were multiplied together.

The cap now reaches the geometry: storeys the cadastre cannot fill are never
extruded. **399 buildings, 687 storeys**, and the effect is what one would expect
of removing floor that was being conditioned:

| | v7 (re-typed only) | v8 (not built) |
|---|---:|---:|
| Site energy | 138.15 GWh | **109.76 GWh** |
| Conditioned area | 3 835 288 m² | **2 757 102 m²** |
| Cadastral-basis EUI | 64.02 kWh/m² | **50.87 kWh/m²** |
| vs reference | 1.37× | **1.09×** |

521 of the 967 buildings come back bit-identical — those are the ones with no
cadastral evidence to cut, or already short enough. Where there is no Tipo15
join, nothing is cut at all: the verification replica and the single-building CLI
are unchanged, and `--verify` still scores 13/13 bit-identical.

**What it cannot fix.** The smallest model the frozen builder can extrude is a
ground storey plus one, so a two-level building whose cadastre supports one is
re-typed as before rather than shrunk — 74 buildings still condition more than
twice their cadastral dwelling area. And a capped block is a flatter box than the
tower it stands for, so its surface-to-volume ratio is wrong even though its
floor area is now right. Heating and cooling are 8.6 % of site energy here and
lighting and equipment scale linearly with floor, so the area correction
dominates — but the shape error is real and is not corrected.

**Coverage is stated, not implied.** 967 of 1 012 buildings in scope — 95.6 % of
buildings and **98.1 %** of the footprint. So the 109.76 GWh figure is the energy
of the modellable district, not of the district. The intensity is not neutral to
that gap either: footprint anti-correlates with EUI among the buildings that did
run, so excluding large buildings biases the subset's intensity upward. Nine
buildings above the single-zone threshold carry 9.2 % of the area and 8.0 % of
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

### How much of a mixed-use block is shops

The template gives dwellings and tertiary space their own lighting and equipment
objects, but both reported into the same `Interior Lighting:General` row, so a
block's electricity could not be split by use. They now carry separate end-use
subcategories, the way the hot-water boiler already did. District-wide the
tertiary storeys are **10.9 GWh, 10.0 %** of site energy.

Heating, cooling, fans and pumps are metered per end use rather than per zone, so
they cannot be attributed this way and stay with the dwellings.
`residential_site_kwh` is therefore an upper bound on the dwellings, not a clean
sub-total, and the field says so.

## What the fixes were worth

| | shared envelope | per-cluster | + warmup | + ceiling & ground *(v6)* | + supervisor's envelope *(v7)* | + storey cap *(v8)* |
|---|---:|---:|---:|---:|---:|---:|
| Buildings | 957 | 947 | 958 | 967 | 967 | **967** |
| Site energy (GWh) | 120.67 | 113.89 | 118.75 | 137.44 | 138.15 | **109.76** |
| Heating (GWh) | 5.66 | 3.78 | 3.81 | 3.92 | 4.31 | **4.83** |
| Cadastral EUI (kWh/m²) | — | — | — | 63.69 | 64.02 | **50.87** |
| vs reference | — | — | — | 1.37× | 1.37× | **1.09×** |
| Footprint coverage | 85.73 % | 83.16 % | 86.33 % | 98.1 % | 98.1 % | **98.1 %** |

The supervisor's own envelope raised heating 10 % and left the total almost
unmoved: his BlocPluriP04 roof is 2.479 W/m²K against the 1.92 derived from the
typology brochure, and 562 m² at +0.559 is roughly eight times what the wall
difference contributes. The storey cap is the change that moves the total, and it
moves it by removing floor that was never there.

Connecting the cluster envelopes cut district **heating by a third** — Benicalap
is mostly post-1980 insulated stock that had been running on a 1974 envelope.

It also broke 10 buildings: heavy pre-war masonry stopped converging inside
EnergyPlus's 25-day warmup ceiling. Raising it to 60 recovered 11 of the 12
failures. That change cannot move a result that already converged — EnergyPlus
stops warming up as soon as the tolerances are met — and the claim was measured
rather than assumed: of the 947 buildings common to both runs, 925 came back
bit-identical and the other 22 moved by at most 0.02 kWh/m².
