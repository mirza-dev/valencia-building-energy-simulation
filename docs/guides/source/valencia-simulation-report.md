# BUILDING STOCK ENERGY WORKBENCH
## Valencia Simulation Report

**Author:** Mirza Saribiyik  
**Publication date:** 28 August 2026  
**Run:** `ALL_VALENC-A_REAL`  

This report documents the annual Valencia building-stock simulation executed in Building Stock Energy Workbench (BSEW). It is designed to answer four questions: what evidence entered the run, which modelling and execution parameters were applied, which quality gates controlled acceptance, and what the settled results support academically.

> **SETTLED EVIDENCE — COVERAGE QUALIFIED.** Process absence, final aggregate settlement, single-identity reconciliation, export integrity and document review passed. Results describe the represented successful stock; failed and excluded references remain explicit in coverage.

### Suggested citation

Mirza Saribiyik. *Building Stock Energy Workbench: Valencia Simulation Report — ALL_VALENC-A_REAL*. 28 August 2026. Software commit `0836bdb1c689a34d13d1af76f764ed21f42a112b`.

<!-- pagebreak -->

# 1. Executive interpretation

`ALL_VALENC-A_REAL` is a full-scope annual simulation of the supplied Valencia building stock under one recorded verified model profile. BSEW converts cadastral and dwelling evidence into simplified OpenStudio models, executes EnergyPlus, records terminal evidence per building and derives aggregate energy, operational-carbon, coverage, geometry-quality and QA summaries.

This edition publishes final values after all latest ledger records reconciled with the settled scope. The principal result classes will be total site energy, whole-site energy intensity under explicitly named residential-area denominators, end-use energy, operational carbon, cluster/district summaries, coverage, and diagnostic status.

The results are simulations under recorded assumptions. They are not measured utility consumption, per-building calibration, an energy certificate, regulatory compliance, embodied carbon or a guaranteed forecast of future operation. The verified profile establishes method consistency against the project's reference evidence; it does not establish measurement validation for every stock building.

## 1.1 Publication decision

This edition assigns **PUBLISHABLE WITH EXPLICIT COVERAGE LIMITS** after reconciling successful, failed, QA-rejected and excluded records. Building-level records remain individually classifiable as PASS, ACCEPTED WITH CLASSIFIED DIAGNOSTICS, FAILED / DO NOT USE, NOT ASSESSED or INCOMPLETE EVIDENCE — DO NOT CITE.

## 1.2 Final evidence gate

| Gate | Required final evidence | Final state |
| --- | --- | --- |
| Process | Run reports false and recorded PID is absent | PASS |
| Scope | Final terminal records reconcile to recorded scope | PASS |
| Aggregate | Final aggregate exists, is readable and is not partial | PASS |
| Identity | Latest accepted rows share intended run/profile/input identity | PASS |
| Exports | CSV, GIS, heat map and manifest reconcile to ledger | PASS |
| Review | QA, diagnostics, limitations and selected buildings reviewed | PASS |

<!-- pagebreak -->

# 2. Research objective and analytical boundary

The simulation provides reproducible evidence for the spatial and typological distribution of modelled final site energy across the supplied Valencia stock. It supports comparison among recorded building clusters and declared area bases, inspection of preparation and EnergyPlus evidence at building level, and production of traceable row-level and spatial exports.

The analytical unit is the building or cadastral reference represented by the latest terminal ledger record. Aggregate values include only records admitted by the stated result and QA contract. Excluded and failed records remain visible in coverage evidence and are not interpreted as zero energy.

## 2.1 Questions the report can answer

- What final site energy and operational emissions were simulated for the represented stock?
- How do results distribute across building clusters and, where supported, districts?
- How do geometric and cadastral residential-area denominators change intensity interpretation?
- Which buildings failed or were excluded, and what bias can or cannot be quantified?
- Which preparation, geometry, occupancy and QA assumptions constrain interpretation?

## 2.2 Questions it cannot answer alone

- Whether a specific building matches its utility bills or actual occupancy.
- Whether the stock is calibrated against measured consumption.
- Whether a simulated difference is causally attributable to an intervention.
- Whether operational factors represent embodied or whole-life carbon.
- Whether simplified one-zone-per-storey geometry captures all local thermal diversity.

<!-- pagebreak -->

# 3. Run identity and execution record

This edition reads identity from the settled run directory and reconciled ledger/configuration evidence, not from mutable Files-page state. The run name is a human-recognisable label; cryptographic fingerprints establish the resolved scientific identity.

| Identity element | Authoritative source | Final value |
| --- | --- | --- |
| Run name | Run directory and API route | `ALL_VALENC-A_REAL` |
| Run identity | Configuration / latest ledger | `8def48ce840d8a1e…` (full value in evidence manifest) |
| Verified profile | Configuration / latest ledger | `cbf17543cfee3ff1…` (full value in evidence manifest) |
| Climate fingerprint | Configuration / latest ledger | `79c11a5992edeee5…` (full value in evidence manifest) |
| Template fingerprint | Configuration / latest ledger | `a5d08275f02c9d76…` (full value in evidence manifest) |
| Policy fingerprint | Configuration / latest ledger | `6b730d4946e772c1…` (full value in evidence manifest) |
| Stock-source fingerprint | Configuration / latest ledger | `05736c0028799d84…` (full value in evidence manifest) |
| Runner schema | Configuration / latest ledger | 2 |
| Execution segments | Preserved process records | segment 1: 8 workers, `full` retention, resume |

The production interface submits six workers and full per-building retention for a new UI request; these are not user-editable controls on the Run page. This run has an interruption/resume history, so execution provenance is reported by preserved segment rather than inferred from the UI default. Worker count affects throughput rather than frozen model inputs. Timing claims are limited to preserved process and ledger evidence.

## 3.1 Period

The Valencia run is annual. Final energy is reported as kWh/year or GWh/year; intensity as kWh/m²·year; operational carbon as kgCO₂/m²·year or tCO₂/year where the corresponding denominator is explicit. No microclimate event values will be mixed into this annual ledger.

<!-- pagebreak -->

# 4. Authoritative inputs

The run resolves its active input set once and records content identities. Absolute workstation paths are excluded from this report. Logical dataset role, preserved filename, validation contract and SHA-256 evidence are retained.

## 4.1 Valencia cadastre

The building GIS supplies parcel/building geometry and attributes used for identity, scope, storeys, population, dwellings, cluster, district/context and footprint preparation. It is an administrative/research source, not a geometric survey. The evidence manifest records the managed filename, component hashes, prepared-stock identity and field-role policy; spatial export acceptance records CRS and feature reconciliation.

## 4.2 Tipo15 dwelling ledger

The companion semicolon-delimited Latin-1 CSV supplies `31_pc`, `252_planta`, `428_uso` and `442_sup_Residencial`. It supports parcel join, floor/use interpretation and the cadastral residential-area denominator. Multiple dwelling records per parcel are expected. Missing joins and fallback use are quantified rather than converted to zero.

## 4.3 EPW and DDY climate pair

The annual EPW supplies hourly weather and site metadata; the matched DDY supplies winter and summer design days. The pair is validated and activated atomically. Slab-contact ground temperature and water-mains temperature are recorded separately because an EPW does not authoritatively provide both model inputs.

## 4.4 OpenStudio template

The OSM template provides role-bound constructions, schedules, thermostats, space types and required system objects. Its fingerprint identifies the source library used to build every accepted model; it is not a measured representation of each building.

| Dataset role | Preserved filename | Contract | SHA-256 / fingerprint |
| --- | --- | --- | --- |
| Building GIS | `DatosRai_ciudadValencia.shp` | Shapefile collection + active policy | 5 component SHA-256 values in manifest; `05736c0028799d84…` (full value in evidence manifest) |
| Tipo15 | `2_daire_kutugu_Tipo15_soloV(in).csv` | `tipo15-v1` | `c6e85c677c32596b…` (full value in evidence manifest) |
| Annual EPW | `3_iklim_ESP_Valencia.082840_IWEC.epw` | `epw-annual-v1` | `09dd4adf2bf25ba5…` (full value in evidence manifest) |
| DDY | `4_tasarim_gunleri_ESP_Valencia.082840_IWEC.ddy` | `ddy-design-days-v1` | `3ddb6f02ca132080…` (full value in evidence manifest) |
| OpenStudio template | `PlantillaOS_v2.osm` | `template-roles-v1` | `292dcf0fceac1b98…` (full value in evidence manifest) |

## 4.5 Extracted data

This section states exactly which data were taken out of the supplied sources and which were produced by the run. A field that exists in a source file is not evidence that the model consumed it.

### 4.5.1 Fields extracted from the inputs

The Valencia cadastre carries 35 attribute fields plus polygon geometry. The preparation policy carries **seven** of them into the engine-ready stock; the remainder are not read during simulation.

| Source | Extracted and used | Role in the model |
| --- | --- | --- |
| Cadastre | `refparcela` | Building identity, ledger key, run scope |
| Cadastre | `altura_max` | Recorded storeys, before the cadastral storey rule |
| Cadastre | `cluster` | TABULA family/period, from which envelope U-values resolve |
| Cadastre | `nombre` | District grouping for aggregate reporting |
| Cadastre | `pob_total` | Resident population driving occupancy and hot-water draw |
| Cadastre | `num_vivend` | Dwelling count for per-dwelling intensities |
| Cadastre | polygon geometry | Footprint preparation, adjacency, party surfaces, context shading |
| Tipo15 | `31_pc` | Join key from dwelling record to cadastral parcel |
| Tipo15 | `252_planta` | Floor label used for ground-use interpretation |
| Tipo15 | `428_uso` | Use code separating residential from other floors |
| Tipo15 | `442_sup_Residencial` | Positive residential area forming the cadastral denominator |
| EPW | 8,760 hourly weather rows and the site record | Annual boundary conditions and site identity |
| DDY | Winter and summer design days | Equipment sizing conditions |
| Template | Role-bound constructions, schedules, thermostats and space types | Operating regime applied to every building |

The prepared stock written from these sources holds sixteen columns: the seven cadastral fields above plus nine derived ones (`family`, `period`, `footprint_area_m2`, `imputed_floors`, `res_area_proxy`, `dup_refparcela`, `ground_use`, `ground_use_source`, `tipo15_res_area_m2`). Derived fields are transparent preparation results, not new measurements.

**Fields deliberately not extracted.** `coorx`, `coory`, `pob_0_14`, `pob_15_65`, `pob_66_mas`, `the_geom_L`, `the_geom_A`, `Shape_Leng`, `Shape_Area`, `referencia`, `codigo_ine`, `nombre_mun`, `codigo_pro`, `zona_clima`, `uso_princi`, `altura_m_1`, `tipologia_`, `ano_constr`, `ano_cons_1`, `numero_viv`, `coddistrit`, `demanda_ca`, `demanda__1`, `calificaci`, `califica_1`, `coste_inte`, `coste_in_1`, `ConsumE` and `ConsumETot` are present in the source but are not read by the simulation. Three of these deserve an explicit note:

- `ano_constr` is not read directly because the construction period already reaches the model through `cluster`.
- `numero_viv` and `num_vivend` are two dwelling counts in the same file; only `num_vivend` is used, and the choice is recorded rather than inferred.
- District grouping is keyed on `nombre` rather than `coddistrit` because the two disagree for at least one record in this source; the name is the field the aggregate uses.

`demanda_ca` (an external certificate heating demand) and `ConsumE` (a per-cluster reference consumption) are **reference material, not model input**. The Rai comparison column shown in the interface is resolved from the published per-cluster reference table, not by reading the shapefile column at run time.

The Tipo15 ledger carries seventeen columns; the four listed above are read and the remaining thirteen (`29_cn`, `126_nm`, `201_tv`, `206_nv`, `231_numPolicia`, `246_bloque`, `250_escalera`, `255_puerta`, `283_codigoPostal`, `288_distrito`, `368_numOrden`, `372_ant`, `452_supSolar`) are not.

### 4.5.2 Fields produced by the run

The run writes one durable ledger record per building and one final aggregate. The curated Building CSV publishes **88 fields per building**, grouped as follows.

| Group | Fields | Examples |
| --- | --- | --- |
| Identity and status | 5 | `refparcela`, `status`, `reason`, `message`, `cluster` |
| Period and event | 5 | `run_mode`, `event_days`, `event_window`, `delta_peak_k`, `delta_base_k` |
| Execution | 2 | `seconds`, `pruned_bytes` |
| Energy | 24 | `total_site_kwh`, `total_site_kwh_m2`, `space_heating_kwh_m2`, `cooling_kwh_m2`, `dhw_kwh_m2`, `site_elec_kwh_m2` |
| Operational carbon | 4 | `total_site_co2_kg_m2`, `hvac_co2_kg_m2`, `total_site_co2_t`, `hvac_co2_t` |
| Geometry and area | 24 | `footprint_m2`, `res_area_m2`, `tipo15_res_area_m2`, `built_storeys`, `top_storey_fraction`, `footprint_fidelity`, `n_windows` |
| Occupancy and use | 9 | `pob_total`, `num_vivend`, `occupants_applied`, `occupants_source`, `occupancy_plausibility`, `ground_use` |
| Construction | 2 | `wall_construction`, `roof_construction` |
| Quality assurance | 6 | `qa_all_passed`, `warnings`, `severes`, `severes_benign_shading_ems`, `severes_unexplained`, `fatals` |
| Provenance | 7 | `profile_fingerprint`, `climate_fingerprint`, `template_fingerprint`, `policy_fingerprint`, `stock_source_fingerprint`, `runner_schema`, `zero_policy` |

The exported data dictionary names every published field beside its source, and the User Guide reproduces the same ordered list.

The final aggregate carries **21 blocks**: `totals`, `by_cluster`, `by_district`, `coverage`, `energy_period`, `buildings_ok`, `buildings_failed`, `buildings_failed_qa`, `buildings_excluded`, `qa_failed`, `unexplained_severes`, `implausible_occupancy`, `geometry_quality`, `floor_area_allocation`, `fragmentation`, `zoning`, `results_layer`, `ledger`, `provenance`, `elapsed_minutes` and `seconds_per_building`. Absence of a block means the quantity was not measured for this run; it never means zero.

<!-- pagebreak -->

# 5. Software and verified profile

The documented toolchain is Python 3.13, OpenStudio 3.11.0 and EnergyPlus 25.2.0 [1]–[3]. The publication records software commit `0836bdb1c689a34d13d1af76f764ed21f42a112b`; release/toolchain verification retains the available patch-level version evidence. Dependency presence is a reproducibility condition, not evidence of physical accuracy.

The verified profile pins the model-building sources, template identity, policy and acceptance evidence. Profile verification means that the modelling method passed the project's fixed reference gates. It does not mean that Valencia's individual buildings were calibrated to measurements.

## 5.1 Profile-integrity rule

Every accepted ledger row must carry the intended profile fingerprint. Source drift during execution is not silently accepted. A mechanically combined ledger containing more than one profile cannot be published as one homogeneous run without explicit separation and warning.

## 5.2 Frozen and derived evidence

Physical model outputs are produced by the pinned simulation chain. Reporting may format units and labels, select latest records, create deterministic aggregates and check duplicate evidence for conflicts. It must not silently recompute historical physical results with today's changed model rules.

<!-- pagebreak -->

# 6. Building preparation and geometry

The pipeline transforms supplied polygons into tractable building masses, identifies context and party surfaces, applies storey rules, assigns ground use, creates openings where exterior walls exist, and records fidelity evidence.

## 6.1 Footprint preparation

Simplification fidelity is measured as symmetric difference divided by raw area. The configured bound and selected tolerance are recorded per building. A refined tolerance or as-drawn fallback protects geometry when the default tolerance would exceed the fidelity bound. A high fidelity score means closeness to the supplied polygon under this transformation; it does not prove that the source polygon is survey-accurate.

## 6.2 Storeys and top-storey allocation

The report distinguishes total levels, residential levels above ground, effective residential storeys, built storeys and top-storey fraction. A recorded storey snap may remove a resolution-scale sliver near an integer boundary. Dwelling loads on a partial top storey are scaled according to the recorded fraction; the physical model still uses whole geometric storeys.

## 6.3 Zoning and context

The production scheme uses one well-mixed thermal zone per storey. Large buildings above the recorded threshold do not receive a core/perimeter subdivision. Party surfaces, context shading and openings are counted per accepted model. A ground storey with no exterior wall can legitimately have zero glazed subsurfaces.

| Geometry evidence | Final statistic |
| --- | --- |
| Buildings with measured fidelity | 26,406 |
| Median / P95 / maximum fidelity | 0.000587 / 0.002898 / 0.009982 |
| Configured / refined / coarser / as-drawn | 26,327 / 44 / 35 / 0 |
| Storey-snapped buildings and area share | 579; 3.19% |
| Large single-zone buildings and energy share | 122; 5.76% |
| Party surfaces — median / P95 / maximum | 10.00 / 35.00 / 160.00 (26,406 measured) |
| Context shading surfaces — median / P95 / maximum | 82.00 / 204.00 / 437.00 (26,406 measured) |
| Windows — median / P95 / maximum | 18.00 / 138.00 / 1,506.00 (26,406 measured) |

<!-- pagebreak -->

# 7. Use, occupancy and domestic hot water

Ground-storey use is conditional on the final recorded building evidence. Residential ground levels are treated as dwelling space and included in the residential basis. Commercial ground levels follow the verified tertiary regime and are excluded from a residential-only denominator. Mixed or unresolved evidence is not forced into either narrative.

Occupancy reporting separates administrative Padrón population, occupants applied to the model, source/fallback, density cap and plausibility status. Padrón is not live presence. A measured zero, an imputed value and a capped value are different evidence states.

Domestic hot water follows the recorded occupancy and model policy. The cited CTE reference demand of 28 litres/person/day is a 60 °C reference [6]; the model's 50 °C implementation is not claimed to be directly equivalent. This edition states both temperatures and the modelling interpretation.

## 7.1 Mixed-use energy attribution

Commercial lighting and equipment can be separated by user-defined subcategory. HVAC and DHW are not fully separable by space type under the preserved meter contract. Therefore `residential_site_kwh` is a **residential-attributed upper bound**, not exact residential consumption.

<!-- pagebreak -->

# 8. HVAC, schedules and climate application

The generated models use the constructions, thermostat schedules, internal-load schedules, space types and HVAC objects bound in the verified template. The methods table identifies the relevant evidence families and relies on the preserved model/template records rather than retyping unsupported assumptions.

Annual weather is read from the recorded Valencia EPW. Design-day objects are selected from the matched DDY. Barometric pressure and site metadata are derived from the active climate contract. The annual Valencia report does not include or annualise the Lecco PALM microclimate slice; that evidence belongs to the separate `LECCO_1` event run.

## 8.1 Parameters to publish

| Parameter family | Publication evidence |
|---|---|
| Heating/cooling thermostat regimes | Template roles and generated OSM |
| Infiltration and ventilation assumptions | Verified profile/template evidence |
| Lighting/equipment schedules | Template roles and subcategory evidence |
| HVAC system and fuel | Generated model and EnergyPlus tables |
| DHW demand and temperature | Model preparation record |
| EPW/DDY site and design days | Climate manifest |
| Ground and water-mains temperatures | Run climate record |

<!-- pagebreak -->

# 9. Result and denominator contract

Every final metric is presented with visible name, authoritative field, unit, period, denominator, precision and interpretation limit. Missing optional evidence is an em dash; a measured zero remains zero. Boolean, NaN, infinity and wrong-type values are never formatted as valid measurements.

## 9.1 Principal energy quantities

Total site energy is the sum of simulated final energy admitted by the result contract. End-use reporting includes space heating, cooling, domestic hot water, fans, pumps, lighting and equipment where recorded. EnergyPlus ABUPS values originate in output meters; user-defined subcategories provide applicable splits [4].

## 9.2 Area denominators

- **Geometric residential area:** modelled residential-storey basis used by the comparison convention.
- **Cadastral residential area:** Tipo15 administrative area where available.
- **Conditioned area:** all conditioned model area, including applicable non-residential space.
- **Residential-attributed upper bound:** energy remaining after separable commercial categories are removed; not exact attribution.

Whole-site energy divided by residential-only area is a comparison convention. It can include commercial ground-floor energy in the numerator while excluding that floor from the denominator. This is not the physical intensity of all conditioned space.

<!-- pagebreak -->

# 10. Operational carbon

Operational emissions are derived from simulated final energy using the recorded project factors: 0.331 kgCO₂/kWh_final for electricity and 0.252 kgCO₂/kWh_final for natural gas [5]. The table below reconciles settled aggregate carbon with named presentation denominators.

The result excludes embodied carbon and is not a measured emissions inventory. Factors may not represent another reporting year, electricity supplier or marginal-emissions question. Carbon figures remain tied to the annual period and the stated energy basis.

| Carbon quantity | Unit | Denominator | Final value |
| --- | --- | --- | --- |
| Total operational carbon | tCO₂/year | None | 661,312.30 |
| Operational carbon intensity | kgCO₂/m²·year | Geometric residential area | 13.89 |
| Operational carbon intensity | kgCO₂/m²·year | Conditioned area | 11.73 |
| HVAC operational carbon | tCO₂/year | None | 76,279.90 |

<!-- pagebreak -->

# 11. Coverage, failures and exclusions

Coverage is reported as successful result count divided by buildings in scope, together with failed, QA-rejected and excluded counts. A high percentage does not prove absence of bias. Missing buildings may differ systematically in size, cluster, geometry or use.

When a common cadastral-area basis exists for represented and missing records, the report may size potential truncation under the recorded convention. Otherwise the correct statement is that truncation is unquantified; it is never “no bias.”

| Scope accounting | Final count |
| --- | ---: |
| Buildings in scope | 26,445 |
| Runnable after preflight | 26,407 |
| Successful latest records | 26,406 |
| Runtime failed | 1 |
| QA rejected | 0 |
| Pre-simulation excluded | 38 |
| Coverage percentage | 99.85% |

Failure and exclusion reasons are grouped without erasing their references. Repeated attempts are resolved by latest-record semantics; attempts are not double-counted as buildings.

<!-- pagebreak -->

# 12. Quality assurance and diagnostics

The building QA matrix compares expected model evidence, observed EnergyPlus evidence or accepted method interval, criterion and result. Stored ratio tolerances are reported in reader-facing units: `0.005` as ±0.5%, `0.02` as ±2%, and annual unmet hours as ≤500 h.

Some checks compare the built model with EnergyPlus output. A plausibility-band check evaluates the method against a research range and is not mislabelled as an EnergyPlus cross-check.

## 12.1 Diagnostic classification

EnergyPlus Severe messages generally require correction [7]. BSEW can accept only a narrowly recorded project-classified benign pattern when all QA checks pass and no Fatal or unexplained Severe remains. Such a building is **ACCEPTED WITH CLASSIFIED DIAGNOSTICS**, not PASS. This edition discloses classified-message counts and the project-specific acceptance rule; row-level markers remain in preserved evidence.

| QA/diagnostic evidence | Final result |
| --- | ---: |
| QA rejected buildings | 0 |
| Accepted rows with classified benign Severe | 26,294 |
| Project-classified benign Severe messages | 1,042,465 |
| Unexplained Severe messages | 0 |
| Fatal messages | 0 |
| Occupancy plausibility flags | 3,954 |

<!-- pagebreak -->

# 13. Aggregate results and cluster interpretation

The results table reports energy and operational carbon from settled evidence. Cluster rows include building count, residential area, total site energy and the available geometric/cadastral intensities. Rai comparison columns are shown only where the published comparison exists and the denominator is aligned.

Cluster intensity is energy divided by the corresponding total area. It is not the arithmetic mean of building intensity. A difference from the Rai reference is a method/reference comparison, not calibration against utility observations.

| Result | Unit and basis | Final value |
| --- | --- | ---: |
| Total site energy | GWh/year, represented stock | 2,084.037 |
| Space heating | GWh/year | 122.330 |
| Cooling | GWh/year | 91.624 |
| Domestic hot water | GWh/year | 360.792 |
| Whole-site / geometric residential area | kWh/m²·year | 43.77 |
| Whole-site / cadastral residential area | kWh/m²·year | 47.08 |
| Whole-site / conditioned area | kWh/m²·year | 36.95 |
| Operational carbon | tCO₂/year | 661,312.30 |

<!-- pagebreak -->

## 13.1 Cluster results

| Cluster | Buildings | Residential area [m²] | Site energy [GWh/year] | Geometric [kWh/m²·year] | Cadastral [kWh/m²·year] |
| --- | ---: | ---: | ---: | ---: | ---: |
| BlocPluriP04 | 8,092 | 20,461,540.5 | 933.805 | 45.64 | 48.16 |
| BlocPluriP05 | 3,759 | 14,125,743.2 | 593.298 | 42.00 | 43.91 |
| BlocPluriP03 | 3,124 | 5,023,841.7 | 230.041 | 45.79 | 49.59 |
| BlocPluriP02 | 2,048 | 2,479,648.1 | 109.694 | 44.24 | 49.49 |
| BlocPluriP06 | 537 | 1,729,270.3 | 62.551 | 36.17 | 39.89 |
| EdiPluriP05 | 363 | 631,992.4 | 25.817 | 40.85 | 50.02 |
| BlocPluriP01 | 664 | 457,992.9 | 19.990 | 43.65 | 49.09 |
| EdiPluriP02 | 1,404 | 427,119.7 | 18.124 | 42.43 | 52.15 |
| VivUniP02 | 1,697 | 392,508.8 | 17.133 | 43.65 | 65.95 |
| VivUniP04 | 1,085 | 358,854.7 | 14.854 | 41.39 | 70.22 |
| VivUniP05 | 940 | 383,970.1 | 13.684 | 35.64 | 48.61 |
| VivUniP03 | 949 | 255,594.2 | 10.756 | 42.08 | 61.71 |
| EdiPluriP03 | 656 | 241,656.7 | 10.314 | 42.68 | 53.15 |
| VivUniP06 | 373 | 225,566.6 | 6.971 | 30.91 | 36.53 |
| EdiPluriP04 | 298 | 136,012.0 | 6.624 | 48.70 | 54.81 |
| VivUniP01 | 183 | 65,279.6 | 2.774 | 42.50 | 43.91 |
| BlocPluriP07 | 14 | 86,453.9 | 2.736 | 31.64 | 37.02 |
| EdiPluriP01 | 139 | 55,018.7 | 2.450 | 44.53 | 59.06 |
| EdiPluriP06 | 71 | 65,882.4 | 2.300 | 34.91 | 42.48 |
| VivUniP07 | 9 | 5,108.3 | 0.112 | 21.87 | 43.95 |
| EdiPluriP07 | 1 | 351.0 | 0.009 | 25.80 | 35.10 |

## 13.2 District results

| District | Buildings | Residential area [m²] | Site energy [GWh/year] | Geometric [kWh/m²·year] |
| --- | ---: | ---: | ---: | ---: |
| QUATRE CARRERES | 1,699 | 3,797,972.0 | 171.352 | 45.12 |
| CAMINS AL GRAU | 1,288 | 3,637,549.5 | 157.793 | 43.38 |
| L'EIXAMPLE | 1,969 | 3,464,625.2 | 148.206 | 42.78 |
| EXTRAMURS | 1,844 | 3,449,296.1 | 148.200 | 42.97 |
| POBLATS MARITIMS | 4,057 | 3,120,382.5 | 140.266 | 44.95 |
| PATRAIX | 1,014 | 3,125,435.7 | 137.320 | 43.94 |
| JESUS | 1,132 | 2,640,860.9 | 118.673 | 44.94 |
| RASCANYA | 1,166 | 2,548,294.7 | 115.772 | 45.43 |
| LA SAIDIA | 1,225 | 2,549,779.0 | 114.220 | 44.80 |
| L'OLIVERETA | 1,238 | 2,364,385.2 | 109.391 | 46.27 |
| CIUTAT VELLA | 2,329 | 2,561,117.6 | 107.964 | 42.16 |
| BENICALAP | 1,009 | 2,354,263.0 | 103.998 | 44.17 |
| ALGIROS | 630 | 2,246,242.0 | 97.308 | 43.32 |
| CAMPANAR | 688 | 2,246,628.5 | 96.090 | 42.77 |
| EL PLA DEL REAL | 539 | 2,176,879.5 | 90.933 | 41.77 |
| POBLATS DEL SUD | 1,674 | 2,002,802.3 | 83.794 | 41.84 |
| BENIMACLET | 950 | 1,728,044.2 | 74.437 | 43.08 |
| POBLATS DE L'OEST | 1,164 | 955,900.8 | 41.509 | 43.42 |
| POBLATS DEL NORD | 791 | 638,947.1 | 26.810 | 41.96 |

<!-- pagebreak -->

# 14. Spatial evidence and exports

The final GeoPackage passed the accepted export checks for CRS, feature count, one feature per latest building reference, null-energy representation of failed/excluded records, context fields, derived per-person/per-dwelling fields, cluster/district layers and supplied QGIS style [8].

The heat map is a publication-oriented image. Its caption identifies run, annual period, metric, denominator, coverage, colour-scale clipping and the simulated-not-measured boundary. Buildings without results will remain visible in a separate status treatment. Distant valid geometries will not be silently cropped from every view.

The Building CSV is the authoritative row-level analysis export after schema correction. Final acceptance compared its headers and latest-record contents with the grouped User Guide field appendix. Absolute local paths will not appear in the curated publication schema.

## 14.1 Export integrity

Every accepted DOCX/PDF/HTML report, CSV, GeoPackage, heat map and evidence manifest has a recorded SHA-256 in the publication manifest. Signed packages contain an Ed25519 manifest. Hash/signature verification establishes integrity, not scientific validity.

## 14.2 Accepted artifact hashes

| Accepted artifact | SHA-256 |
| --- | --- |
| aggregate.json | `d4dee63a1e10f2ce16c8ac57b174f21eecd91a41abfa20fa1eb42bdf0c585f89` |
| buildings.csv | `9ae87afcfb41bda199002cf55d708c9450dc37b2df318c624a55d536cf9f5897` |
| buildings_dictionary.csv | `3b284be41bba7615de38e8a910c79ef0f61877d667820ed9c9200327ec5e2778` |
| ledger.jsonl | `244e0f1ff407e54f3edaab347537ce60a3fdb9d43d496dea396dea547c876338` |
| results_buildings.gpkg | `34c235c8b04ac648fb75a9aeeb955cb3d3ffa44d871e6a51e455085a36f43da2` |
| results_buildings.qml | `1410137416eac5024aac6e27c583a4b6b1eb2f7d4badba51ad31ed0c0268c422` |
| results_heatmap.json | `99ba02a400aa10c113f323afaa7b0db0144164cb048169dec3323373d938ced5` |
| results_heatmap.png | `9c92626a8868be3b4c34b0348507c5299f3b68cd0a68b6c2b4615bbf4fb39e53` |
| run_config.json | `30f5da100bd69ab03f821f94241c6b6ebd38923c4b4973228355d0f66c5e7f61` |
| run_process.json | `4b13c6610bc97d2b157646b876692f1c6d46ace922a0866b000c9c26bc8bcf63` |
| valencia-publication-evidence.json | `6bfc1cf434e591ccc194be3fc4c8b042ffe3bcb2c5798b6d3af1ea3b58b12793` |

<!-- pagebreak -->

# 15. Reproducible representative-building selection

This edition selects up to six references by deterministic rules applied to settled latest records. Duplicate selections are removed in the order below and the exact algorithm, source hash and selected references are preserved in the evidence manifest.

1. Successful building nearest the median valid total-site intensity.
2. Successful building nearest the fifth percentile.
3. Successful building nearest the ninety-fifth percentile.
4. Successful building with the greatest total-site energy contribution.
5. Accepted building carrying a recorded occupancy or classified-diagnostic flag, if available.
6. One failed or excluded reference representing the most frequent non-success reason, if available.

For successful examples, the evidence manifest preserves identity, cluster, area bases, energy, carbon, occupancy, geometry, QA and diagnostic fields; the table below provides the citation-oriented summary. The failed/excluded example will not be assigned energy; it will document the failure stage and reason.

| Selection role | Reference | Evidence status |
| --- | --- | --- |
| median-nearest | `5327403YJ2752G` | ACCEPTED WITH CLASSIFIED DIAGNOSTICS |
| p5-nearest | `9504916YJ2790D` | ACCEPTED WITH CLASSIFIED DIAGNOSTICS |
| p95-nearest | `9826309YJ2792F` | ACCEPTED WITH CLASSIFIED DIAGNOSTICS |
| highest-total-contribution | `4889401YJ2648H` | ACCEPTED WITH CLASSIFIED DIAGNOSTICS |
| flagged-accepted | `0022301YJ3702C` | ACCEPTED WITH CLASSIFIED DIAGNOSTICS |
| failed-or-excluded | `0023911YJ3702C` | EXCLUDED — footprint_outside_range |

<!-- pagebreak -->

# 16. Limitations and responsible use

The final interpretation retains the following limits beside affected findings:

- cadastral geometry, use and area are administrative evidence with measurement/allocation limits;
- one parcel is not always one physical building;
- simplified zoning and integer-storey geometry affect area representation;
- large one-zone-per-storey buildings do not represent core/perimeter diversity;
- difficult/courtyard geometry can be excluded by preparation gates;
- occupancy uses administrative evidence, caps and recorded imputation;
- HVAC and DHW are not exactly separable between residential and commercial spaces;
- operational carbon excludes embodied carbon;
- profile verification is not per-building measurement calibration;
- pilot-building LHS evidence is not a city-total uncertainty band;
- failed/excluded records can create unquantified truncation.

These limitations do not automatically invalidate the study. They define the claims the evidence can support and the additional measurements or sensitivity analyses needed for stronger claims.

<!-- pagebreak -->

# 17. Provenance, reproducibility and publication package

The final publication package preserves logical filenames and hashes for the run configuration, process record, ledger, final aggregate, corrected Building CSV, GeoPackage/style, heat map, selected readable reports, EnergyPlus tables, technical evidence and document outputs. Absolute workstation paths will be removed from publication-facing documents and curated exports.

No result will be accepted when duplicate authoritative evidence blocks conflict. A building identity mismatch prevents report generation. A successful-looking row without essential period, result, QA or diagnostic evidence is INCOMPLETE EVIDENCE — DO NOT CITE.

## 17.1 Evidence manifest

The machine-readable JSON/CSV manifest contains:

- run, profile, climate, template, policy, stock-source and runner-schema identities;
- final scope/count/period reconciliation;
- source and output SHA-256 values;
- key method parameters and units;
- selected-building algorithm and references;
- figure/table source files;
- software commit and publication date;
- review gates and their PASS/FAIL state.


# 18. References

[1] Python Software Foundation, “Python 3.13 documentation,” https://docs.python.org/3.13/  
[2] NREL, “OpenStudio 3.11.0 release,” https://github.com/NREL/OpenStudio/releases/tag/v3.11.0  
[3] EnergyPlus, “EnergyPlus 25.2 documentation,” https://energyplus.readthedocs.io/en/v25.2.0/  
[4] U.S. Department of Energy, *EnergyPlus Input Output Reference*, version 25.2, https://bigladdersoftware.com/epx/docs/25-2/input-output-reference/index.html  
[5] Instituto para la Diversificación y Ahorro de la Energía, *La bomba de calor en la rehabilitación energética de edificios*, 2023, https://www.idae.es/sites/default/files/documentos/publicaciones_idae/Guias_IDAE_La_Bomba_de_calor_2023_V11.pdf  
[6] Gobierno de España, *Código Técnico de la Edificación, DB-HE*, 2017 edition, https://www.codigotecnico.org/pdf/Documentos/HE/DBHE_201706.pdf  
[7] U.S. Department of Energy, *EnergyPlus Essentials*, version 25.2, https://energyplus.readthedocs.io/en/v25.2.0/essentials/essentials.html  
[8] Open Geospatial Consortium, “GeoPackage Encoding Standard,” https://www.ogc.org/standard/geopackage/  
