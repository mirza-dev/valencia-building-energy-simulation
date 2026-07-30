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

## District results

The Benicalap district run (957 buildings) is being regenerated after the
envelope fix above. The earlier figures were produced when every building shared
one envelope and are not comparable.
