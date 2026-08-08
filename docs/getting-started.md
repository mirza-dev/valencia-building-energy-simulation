# Getting started

Valencia Stock Energy Workbench is a local, English-only desktop-style web
application. Its operational flow is deliberately small: **Files → Run →
Outputs**. The simulation stays on your computer; the browser is only the user
interface.

## 1. Get the correct package

Use the complete Valencia Workbench research distribution supplied by the
project team. A plain source-code clone does **not** contain the licensed UPV
inputs or the 4.4 GB completed Benicalap evidence run.

After unpacking, the project must contain:

- Valencia building GIS, including the `.shp`, `.dbf`, `.shx` and projection
  sidecars;
- `Tipo15_soloV(in).csv`;
- the Valencia IWEC `.epw` and matching `.ddy`;
- `PlantillaOS_v2.osm`;
- `out/stock/benicalap_v8/`, including its aggregate, ledger, models and
  preserved EnergyPlus outputs.

The installer checks the published SHA-256 values and stops if this payload is
missing or changed. Maintainers can verify it independently with:

```bash
python3.13 scripts/verify_distribution.py --require
```

## 2. Install the required tools

Install these exact/runtime-compatible versions before running the installer:

1. [Python 3.13](https://www.python.org/downloads/) — do not use Python 3.14.
2. [OpenStudio 3.11.0](https://github.com/NREL/OpenStudio/releases/tag/v3.11.0),
   which bundles EnergyPlus 25.2.0.
3. [Node.js 20.19 or newer](https://nodejs.org/en/download).
4. [Git](https://git-scm.com/downloads), used by the safe updater.

If OpenStudio is installed in a non-standard location, set
`VALENCIA_EPLUS_DIR` to its `EnergyPlus` directory before installing or
starting. The installer executes EnergyPlus and rejects a version other than
25.2.0.

### macOS or Linux

Open Terminal in the unpacked project directory:

```bash
chmod +x scripts/install.sh scripts/start.sh scripts/update.sh
./scripts/install.sh
```

### Windows

Open PowerShell in the unpacked project directory. If local scripts are blocked,
allow them for this one PowerShell process, then install:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\install.ps1
```

The installer creates `.venv`, installs the pinned Python packages, installs the
locked frontend packages, builds the interface, exercises the OpenStudio SDK,
checks EnergyPlus 25.2.0 and verifies the distribution payload.

## 3. Start and stop

On macOS or Linux:

```bash
./scripts/start.sh
```

On Windows:

```powershell
.\scripts\start.ps1
```

The app opens at `http://127.0.0.1:8765/#/files`. Keep the terminal open. Press
`Ctrl+C` once for a clean stop. The durable JSONL ledger is flushed per building,
so an interrupted stock run can be resumed.

The default mutable state is kept in `var/`, while operational stock output is
kept in `out/ui_runs/`. Advanced installations may place these on a larger disk:

```bash
VALENCIA_STATE_DIR=/path/to/state VALENCIA_RUN_DIR=/path/to/runs ./scripts/start.sh
```

PowerShell uses the same `VALENCIA_STATE_DIR` and `VALENCIA_RUN_DIR` environment
variables.

## 4. First-run walkthrough

### Files

Confirm that all four cards are marked **ACTIVE** and the page says **RUN
READY**. Each input is copied into managed, content-addressed storage and shown
with its snapshot hash. To replace an input, upload it on the corresponding
card; invalid files are rejected before activation.

The four inputs are Building GIS, Tipo15 companion, the EPW + DDY climate pair,
and the PlantillaOS template. A run freezes their resolved fingerprints at
start, so later input changes do not rewrite earlier evidence.

### Run

For a quick first check, leave **Selected buildings** active and use the pilot
reference `4252702YJ2745A`. Give the run a unique name and select **Run
preflight**. Review the in-scope, runnable and excluded counts, estimated time
and protected-storage estimate; then select **Start run**.

District and full-Valencia runs use the same engine. Full Valencia requires an
extra acknowledgement because it is long-running and storage-intensive. Use
**Stop safely** when needed, and later use **Resume same run** with the same run
name and scope. Resume refuses a changed input/profile identity.

### Outputs

Choose a finished run to see aggregate energy, geometric and cadastral EUI,
carbon, coverage, QA and cluster comparisons. Search/filter the building ledger
and select a row to open its preserved model, EnergyPlus table, QA report and
error file.

- **Building CSV** downloads the searchable ledger as a table.
- **Signed building package** exports one building’s model, preserved outputs
  and ledger evidence.
- **Full signed ZIP** first calculates the real file count and uncompressed
  size. After confirmation it creates a ZIP64 archive with an Ed25519-signed
  manifest.

The distribution already includes the completed `benicalap_v8` run, so Outputs
is useful before starting a new simulation.

For what every number on that screen means, and for what the product can and
cannot do, read the user guide: [`Valencia Workbench - User Guide.pdf`](Valencia%20Workbench%20-%20User%20Guide.pdf)
(the same document as [`user-guide.html`](user-guide.html), which is what the
PDF is rendered from).

## 5. Update safely

The updater never deletes `data/`, `var/` or `out/`. It also refuses to run when
tracked code has local changes.

macOS or Linux:

```bash
./scripts/update.sh
```

Windows:

```powershell
.\scripts\update.ps1
```

It performs a fast-forward-only Git update, reinstalls pinned dependencies,
rebuilds the frontend, then repeats the toolchain and payload checks. Back up
research results independently as part of normal laboratory data management.

## Troubleshooting

- **OpenStudio or EnergyPlus not found:** install OpenStudio 3.11.0 or point
  `VALENCIA_EPLUS_DIR` at its `EnergyPlus` directory.
- **Distribution payload incomplete:** use the complete research distribution,
  or upload the four authoritative inputs from Files. The bundled example is
  required for the installer’s full release check.
- **Port 8765 is already in use:** stop the existing Workbench process, or set
  `WORKBENCH_PORT` to another local port before starting.
- **A run is blocked:** read the preflight exclusion reasons. The engine records
  unsupported data instead of inventing a default.

For scientific scope, frozen physics and validation results, see the main
[`README.md`](../README.md).
