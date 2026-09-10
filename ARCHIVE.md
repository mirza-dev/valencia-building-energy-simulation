# BSEW archive record

- **Project:** BSEW — Building Stock Energy Workbench
- **Archive date:** 10 September 2026
- **Frozen version:** `bsew-final-2026-09`
- **Canonical branch:** `main`

## Status

This repository is a read-only research record. Active development, hosted
operation and routine support have ended. The archive preserves the final
public software, documentation and derived Valencia publication evidence; it
does not certify that a running BSEW service or a private copy of every input
and simulation artifact still exists.

The frozen Git tag identifies the final public source state. Earlier history is
retained without squashing or rewriting. At closure there were no open GitHub
issues or pull requests.

## Final Valencia evidence

The settled annual run is `ALL_VALENC-A_REAL`.

| Measure | Final value |
| --- | ---: |
| Buildings in scope | 26,445 |
| Successful latest records | 26,406 |
| Excluded before simulation | 38 |
| Runtime failed / QA rejected | 1 / 0 |
| Successful-record coverage | 99.85% |
| Cadastral residential-area coverage | 99.684% |
| Total site energy | 2,084.03719 GWh/year |
| Whole-site / cadastral residential area | 47.08 kWh/m²·year |
| Whole-site / geometric residential area | 43.774 kWh/m²·year |
| Operational carbon | 661,312.3 tCO₂/year |

These values are modelled operational-energy and emissions results under the
recorded assumptions. They are not measured consumption, building-level
calibration, certification or a guaranteed forecast. The one runtime failure
and 38 pre-simulation exclusions remain part of the published coverage record.

Canonical evidence:

- [Valencia Simulation Report](docs/guides/published/Building%20Stock%20Energy%20Workbench%20-%20Valencia%20Simulation%20Report.pdf)
- [Valencia publication evidence](docs/guides/evidence/valencia-publication-evidence.json)
- [Publication acceptance record](docs/guides/evidence/publication-acceptance.json)
- [Published document manifest](docs/guides/published/document-manifest.json)
- [Final Valencia heat map](docs/results/ALL_VALENC-A_REAL/results_heatmap.png)

The acceptance record contains SHA-256 identities for the settled aggregate,
ledger, curated CSV and dictionary, GeoPackage and style, heat-map data and
image, run configuration, process record and publication evidence. The
published-document manifest separately identifies every final DOCX, PDF and
HTML guide artifact.

## What is and is not preserved here

The public archive includes:

- the BSEW application and verification code;
- deterministic documentation sources and final published guides;
- derived, public-facing Valencia evidence and the final heat map;
- manifests and acceptance records needed to identify the reviewed artifacts;
- synthetic and unit tests that do not require restricted inputs.

The public archive does **not** include the licensed or third-party cadastral,
dwelling, weather and OpenStudio-template payloads, nor the complete
`out/stock/ALL_VALENC-A_REAL/` simulation tree. Those materials were never
licensed for public redistribution. Their public manifests and hashes identify
expected artifacts but are not substitutes for an authorised private backup.
Do not claim exact rerun capability unless those private files have been
located and independently matched to the recorded identities.

The repository also has no standalone `LICENSE` file. Archiving it does not
grant permissions that are not otherwise provided by applicable law or the
rights holders.

## Restoring a working research environment

1. Clone the frozen tag rather than an arbitrary branch tip:

   ```bash
   git clone --branch bsew-final-2026-09 --depth 1 \
     https://github.com/mirza-dev/valencia-building-energy-simulation.git
   ```

2. Obtain the authorised research distribution and independently verify its
   files against `distribution-manifest.json` before use.
3. Follow the [Installation Guide](docs/guides/published/Building%20Stock%20Energy%20Workbench%20-%20Installation%20Guide.pdf)
   with Python 3.13, OpenStudio 3.11.0 and EnergyPlus 25.2.0.
4. Confirm the loopback health endpoint and all Files/Run preflight identities
   before starting any simulation. Never treat an old output directory as
   current merely because its name matches.
5. Reconcile any recovered final-run artifacts with the SHA-256 values in the
   [publication acceptance record](docs/guides/evidence/publication-acceptance.json).

The Windows procedure received controlled static verification against the
committed scripts and official prerequisites; it was **not executed natively on
Windows**. Any future restoration on Windows therefore requires a new native
acceptance record.

## Interpretation and future work

- Use `ALL_VALENC-A_REAL` for the final city result. Benicalap and other older
  runs are demonstrations or regression evidence, not substitutes for it.
- Cite the Valencia Simulation Report rather than isolated README values.
- Treat any future physics, input, dependency or schema change as a new study
  version with new provenance and validation evidence.
- If development resumes, unarchive the repository deliberately, document the
  new maintenance status, and create a version after `bsew-final-2026-09`.

> **Türkçe not:** Bu arşiv projenin silindiği anlamına gelmez. Son kamuya açık
> yazılım, belgeler ve Valencia kanıtı korunur; ancak servis artık işletilmez ve
> lisanslı ham girdiler ile tam koşu klasörü bu GitHub deposunda bulunmaz.
