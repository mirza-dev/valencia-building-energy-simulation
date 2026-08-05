# Valencia Building Energy Simulation

A verified pipeline that turns cadastral GIS records into EnergyPlus building
energy results — one building at a time, for the whole of Valencia (26,452
buildings).

Built during an Erasmus+ internship at Universitat Politècnica de València.

---

## What the problem actually is

The established method for city-scale building energy is to cluster the stock by
typology, simulate one representative per cluster, and scale by floor area. It
works, and this project started there. But a representative model cannot answer
"what about *this* building", and the error it hides is invisible: every building
in a cluster gets the same shape, the same neighbours, the same occupancy.

So the pipeline was rebuilt to simulate each building individually — its own
cadastral footprint, its own storey count, its own registered residents, its own
neighbours casting real shadows — while staying comparable to the supervisors'
published city model.

That raises a harder problem than performance: **how do you know 26,452
simulations are right?** You cannot inspect them. What this repository is really
about is that the engine has to refuse rather than guess, and every refusal has
to be recorded.

---

## Verification: the supervisor's own building, rebuilt

The engine is verified by reconstructing a building the supervisor had already
modelled and published results for, then scoring 13 gates against his numbers.

| Gate | Ours | Reference | Δ |
|---|---:|---:|---:|
| Residential area (m²) | 954.800 | 954.800 | 0.00 % |
| Gross wall area (m²) | 465.000 | 465.000 | 0.00 % |
| Wall U (W/m²K) | 1.409 | 1.409 | 0.00 % |
| Roof U | 2.496 | 2.495 | +0.04 % |
| Ground slab U | 0.501 | 0.501 | 0.00 % |
| People gain (GJ) | 73.530 | 73.070 | +0.63 % |
| Lighting (GJ) | 65.308 | 65.320 | −0.02 % |
| Equipment (GJ) | 56.543 | 56.560 | −0.03 % |
| Domestic hot water (GJ) | 36.057 | 36.080 | −0.06 % |
| Pumps (GJ) | 1.169 | 1.160 | +0.75 % |
| HVAC sensible heating (GJ) | 26.527 | 27.300 | −2.83 % |
| HVAC sensible cooling (GJ) | −68.403 | −65.030 | +5.19 % |
| **Total site energy (kWh/m²)** | **53.680** | **54.500** | **−1.50 %** |

Three differences were measured and deliberately left open rather than tuned
away: the EnergyPlus version gap (his 9.5.0 against our 25.2.0, which moved the
DX part-load curves), his roof solar reflectance against ours, and his window
distribution. Each is recorded in code with the measurement behind it.

`python src/verified_model.py --verify` reproduces this table.

---

## What it is made of

```
src/verified_model.py     the single call site: 23 locked parameters, a drift
                          guard, and the verification harness
src/deep_building.py      six layers added on top of the frozen geometry builder
src/stock_runner.py       orchestration for thousands of buildings - no physics
src/climate.py            a climate is a validated bundle, not a file path
src/template_contract.py  what the pipeline needs from an OpenStudio template
src/stock_input_policy.py how raw cadastre becomes modellable data
src/workbench/ frontend/  a local research UI over the same engine
```

One building:

```bash
python src/verified_model.py --refparcela 4252702YJ2745A
```

A stock:

```bash
python src/stock_runner.py --scope district --district BENICALAP --workers 6
python src/stock_runner.py --scope all --resume
python src/stock_runner.py --aggregate out/stock/<run>/ledger.jsonl
```

Swappable inputs, each validated before a run starts:

```bash
--climate  climates/valencia_iwec.json      # EPW + design days + site temperatures
--policy   policies/valencia_default.json   # column names and data rules
--template <spec>.json                      # a different OpenStudio template
```

---

## The design idea: refuse rather than guess

| Gate | What it stops |
|---|---|
| **Drift guard** | 23 locked physical constants plus the SHA-256 of 4 source files. If one moves, the run does not start |
| **Climate bundle** | EPW and `.ddy` must describe the same station; design days must agree with the EPW's own header; barometric pressure must match the elevation |
| **Template contract** | All 24 objects the chain binds to by name must exist with the right type. Missing ones are reported together, never defaulted |
| **Geometry screening** | The engine's own gate is called, not a restatement of it. Every excluded building is recorded with a reason |
| **Zoning caveat** | Every storey is one well-mixed zone. Above 5,000 m² of footprint that is the weaker assumption, so those buildings are flagged and a total states what share of itself rests on them |
| **Severe rule** | One unclassified EnergyPlus Severe invalidates the run |
| **QA cross-check** | EnergyPlus's own tables are compared against what the model claims |
| **Run identity** | Profile, climate, template, policy, source data and schema hashed together. A ledger written under one identity cannot be resumed under another |
| **Coverage block** | States what a total actually represents, so an absolute figure is never read as the whole district |

A durable JSONL ledger, fsynced per building, makes a multi-day run resumable
after an interruption — proven in a real one, not a drill.

---

## Bugs this found, and how

The gates above exist because each of them caught something. Three worth
reading, because the method matters more than the fix.

**Design days carrying OpenStudio's defaults.** Barometric pressure sat at
31,000 Pa — about 9,000 m of altitude — with a July cooling day and a sky
clearness of 0.0, i.e. no sun at all. The cooling design load was off by 3.6×.
Found by extracting the supervisor's published HTML tables and comparing row by
row; the correct values were then read from the official ASHRAE design-condition
file rather than guessed. Two errors had been cancelling: undersized coils
under-delivered cooling by roughly what our darker roof over-demanded, so the
gate had been passing at −0.52 % on a coincidence.

**A climate scenario that changed nothing.** EnergyPlus's `--weather` argument
overrides whatever the model carries, so writing a new weather file into the
model left the annual simulation running the original 8,760 hours. Proven by
building an EPW with every hour +5 K and the station renamed: the result came
back byte-identical while `eplusout.eio` still named the original city. After the
fix, the same test moved heating −89 % and cooling +187 %.

**21 typology clusters that were never applied.** The cluster envelope resolver
existed and was correct — but only the UI called it. The stock chain handed every
building the same default, so all 21 clusters came out with one wall and one roof
between them, differing only in geometry and occupancy. The evidence had been
sitting in the ledger the whole time: `wall_construction`, 21 rows, 1 distinct
value.

The lesson was the same each time, and it is now a rule in the codebase: writing
a value into the model does not mean the engine used it. Verify from the engine's
own output.

---

## Results

The Benicalap district of Valencia, every modellable building simulated
individually: **967 buildings, 137.44 GWh**, at **58.73 kWh/m²** of the floor the
model conditions — or **63.69 kWh/m²** per square metre of cadastral dwelling
area, which is the basis the reference city model's constants are defined on.
Against those constants, district-wide, we are **1.37× the reference**.

Coverage is stated rather than implied: **95.6 %** of buildings in scope and
**98.1 %** of the footprint. The total is therefore the energy of the modellable
district, not of the district. Nor is the intensity neutral: footprint
anti-correlates with EUI among the buildings that did run, so excluding the large
ones biases the modellable subset's kWh/m² upward. It is the EUI of the
modellable subset, stated as such. Nine buildings above the single-zone threshold
carry 9.2 % of the area and 15.0 % of the energy on a coarser zoning assumption;
that share is reported, not buried.

Two corrections behind those numbers are worth naming, because both were errors
of ours that the published figures had been hiding.

**The floor area.** `altura_max` records the highest storey holding a dwelling;
the chain treated every storey below it as housing. Among the district's
mid-rise blocks, 85 buildings turned out to have the same storey count and the
same dwelling size as their neighbours but **0.37 dwellings per 100 m² of
modelled floor instead of 0.98** — the rest of the floor is commercial. That was
24.6 % of the area being simulated as dwellings. Those storeys are now
conditioned tertiary space and leave the residential basis. Floor area fell
31.7 %; energy fell **2.8 %**, because the storeys stay conditioned. What it
fixed was the denominator: modelled residential area now sits within 8.4 % of
the cadastral dwelling area, against 58.9 % before.

**A retracted finding.** This page used to report that deviation from the
reference was monotonic in storey count (**−0.80**) and that this measured the
representative-model method's own surface-to-volume error. The author's thesis
documents the reference geometries — three models at 2, 2 and 6 storeys, not the
"four to five" the claim assumed. Tested properly, deviation correlates
**−0.03** with the gap between a cluster's storeys and its reference model's.
The original −0.80 reproduces, then falls to −0.22 (n.s.) once the ground-floor
area basis is fixed and the seven VivUni clusters missing from our reference
table are restored. The pattern was substantially ours. The remaining +26 % to
+102 % per-cluster gap has no established cause yet; the envelope is the leading
candidate.

[`docs/results/`](docs/results/) holds the verification report, the district
output, and what each fix was worth.

---

## Running it

Requires OpenStudio 3.11 (which bundles EnergyPlus 25.2) and Python 3.13.
OpenStudio's Python bindings are unstable on 3.14.

```bash
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest tests/          # 347 tests
```

**The input data is not in this repository.** It belongs to the UPV research
group and to third parties, and is not mine to redistribute:

| Input | What it is | Where it comes from |
|---|---|---|
| `data/gis/DatosRai_ciudadValencia.shp` | 26,452 building footprints with typology, storeys, dwellings and registered residents | UPV research group, unpublished |
| `data/reference/Tipo15_soloV(in).csv` | Cadastral dwelling register, 411,273 records | Spanish cadastre, via UPV |
| `data/templates/PlantillaOS_v2.osm` | OpenStudio library: constructions, schedules, space types | UPV research group |
| `data/weather/ESP_Valencia.082840_IWEC.epw` + `.ddy` | Hourly weather and ASHRAE design conditions | [energyplus.net/weather](https://energyplus.net/weather) — ASHRAE IWEC |

Most tests run without them, on synthetic models; a handful marked `integration`
need a real building.

---

## Status

The engine is verified and frozen, and the district-scale results above were
produced with it. The footprint ceiling was then raised (see Results), taking the
runnable stock to **25,102 buildings and 98.38 % of the city's floor area**; the
district is being re-run on it before the full city run.

## Licence

Code: MIT. The input data is not covered and is not included.
