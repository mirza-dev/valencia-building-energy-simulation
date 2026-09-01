# BUILDING STOCK ENERGY WORKBENCH
## Installation Guide

**Author:** Mirza Saribiyik  
**Publication date:** 28 August 2026  

Building Stock Energy Workbench (BSEW) is a local web application for reproducible building-stock energy simulation. This guide installs the verified research release, checks the scientific toolchain, and explains how to start, stop, update, back up, and diagnose the application. The release bundle is the primary installation route because a plain source checkout does not include licensed or third-party research inputs.

> **Scientific boundary.** Installation success means that the software, dependency versions, input payload and local service pass their technical checks. It does not validate a particular building against measured consumption and does not make a run suitable for citation by itself.

### Suggested citation

Mirza Saribiyik. *Building Stock Energy Workbench: Installation Guide*. 28 August 2026. Software commit `0836bdb1c689a34d13d1af76f764ed21f42a112b`.

<!-- pagebreak -->

# 1. Installation scope and release model

## 1.1 What the release contains

Use the complete, verified BSEW research release supplied by the project. The release should contain the application source, locked dependency definitions, installation and start scripts, the authorised research input payload, and any explicitly bundled evidence runs. A Git clone alone contains the MIT-licensed code but not necessarily the data required to reproduce the Valencia application.

The verified Valencia payload includes four input classes:

- building GIS with required geometry and attribute sidecars;
- the cadastral Tipo15 companion table;
- an EPW weather file and its matching DDY design-day file;
- the OpenStudio template used by the verified model profile.

The distribution verifier checks the published SHA-256 identities and fails closed when required content is absent or changed. Do not replace a failed verification with a manually copied file unless its provenance and expected identity are known.

## 1.2 Software and data licensing

The application code is licensed under MIT. That licence does not extend to UPV research-group material, cadastral data, weather files, templates, or other third-party inputs. Store and redistribute each input only under its own terms. A release recipient should record who supplied the payload, when it was received, and the hash of the received package.

## 1.3 Supported installation paths

The supported research path is the verified release bundle. Developers who already hold authorised inputs may work from a Git checkout, but a checkout alone is not a complete research release. The same dependency and payload checks still apply; only the way source and authorised data are obtained differs.

<!-- pagebreak -->

# 2. System requirements

## 2.1 Required versions

| Component | Required version | Why it is constrained |
|---|---:|---|
| Python | 3.13.x | The pinned Python and OpenStudio binding set is validated on this interpreter line. Python 3.14 is not accepted. |
| OpenStudio | 3.11.0 | Supplies the OpenStudio SDK used to construct and inspect models. |
| EnergyPlus | 25.2.0 | Bundled with the supported OpenStudio release and recorded in the simulation evidence. |
| Node.js | 20.19 or newer | Required to install the locked frontend packages and build the interface. |
| Git | Current supported client | Required for a Git checkout and the safe updater; not required when installing a verified archive-only release. |

Install from the official providers listed in References [1]–[5]. The BSEW installer executes version checks; a command merely being present on `PATH` is not sufficient.

## 2.2 Hardware and storage planning

BSEW runs locally and stores durable per-building evidence. Capacity depends strongly on the selected retention level. A full-retention city run can preserve OpenStudio models, EnergyPlus tables, error logs and additional evidence for every successful building; plan for hundreds of gigabytes rather than assuming that the aggregate file represents total storage.

Before a large run:

1. use Run preflight to review scope, runnable count and protected-storage estimate;
2. place the complete release on a reliable disk if the system volume is too small, because stock evidence is currently written below the release's `out/stock/` directory;
3. keep enough free space for the run plus exports and independent backup;
4. prevent sleep and unplanned disk disconnection during long simulations.

## 2.3 Network and security model

The production launcher binds to `127.0.0.1`. The browser is the interface; simulation and evidence remain on the local machine. Do not expose the port to a public network without an independently reviewed authentication, transport-security and access-control design.

<!-- pagebreak -->

# 3. Install on macOS

## 3.1 Prerequisites

Install Python 3.13, OpenStudio 3.11.0, Node.js 20.19 or newer, and Git. If EnergyPlus is not found in the standard OpenStudio location, set `VALENCIA_EPLUS_DIR` to the EnergyPlus directory. The historical variable name is retained for compatibility and does not change the BSEW product name.

## 3.2 Install the release

Open Terminal in the unpacked release directory and run:

```bash
chmod +x scripts/install.sh scripts/start.sh scripts/update.sh
./scripts/install.sh
```

The installer creates `.venv`, upgrades `pip`, installs the pinned Python requirements, installs the locked frontend packages with `npm ci`, builds the interface, exercises the OpenStudio toolchain, checks EnergyPlus 25.2.0, and verifies the release payload. Treat any non-zero exit as an incomplete installation.

## 3.3 Verify explicitly

The installer already performs the required checks. Maintainers may rerun the payload check without starting the application:

```bash
python3.13 scripts/verify_distribution.py --require
```

Record the terminal output, release package hash, operating-system version and installation date in the project log. Do not record a local absolute path in a publication.

The macOS commands above were checked against the supplied scripts and the release verifier. A clean-release acceptance record should retain the command output while excluding usernames and local absolute paths from any publication.

<!-- pagebreak -->

# 4. Install on Windows

## 4.1 Supported Windows route

Use a native 64-bit Windows 10 or Windows 11 installation. Run the supplied `.ps1` files in Windows PowerShell 5.1 or PowerShell 7; do not paste them into Command Prompt and do not run the native-Windows procedure from WSL. The release should be stored on a local NTFS volume in a short, user-writable path such as `C:\BSEW`. Avoid OneDrive, SharePoint, network drives and removable media during installation or simulation. The OpenStudio 3.11.0 Windows package is an x64 release; Windows on ARM and emulated toolchains are not qualified by this guide.

Installers may request elevation, but the BSEW installation script itself should normally run in a non-administrator PowerShell window. Do not disable antivirus, Windows Defender or institutional Group Policy to complete an installation.

## 4.2 Install and verify the prerequisites

Install the prerequisites in the order below. Download them only from the official sources in References [2], [4], [5] and [7]. Close and reopen PowerShell after installing them so that the updated commands are visible.

1. Install the x64 build of Python 3.13. The BSEW installer selects `py -3.13` whenever the `py` command exists, so that exact command must succeed; merely having another Python version on `PATH` is insufficient.
2. Install `OpenStudio-3.11.0+241b8abb4d-Windows.exe`. OpenStudio 3.11.0 includes the required EnergyPlus 25.2.0 engine; do not install a separate, mismatched EnergyPlus release.
3. Install an official x64 Node.js LTS release that satisfies the enforced minimum of 20.19.0. npm is installed with Node.js.
4. Install Git for Windows. The current `install.ps1` checks for Git even when installing a supplied release bundle; the updater also requires it.

Open a new PowerShell window and run this pre-installation check:

```powershell
$ErrorActionPreference = 'Stop'
py -3.13 --version
py -3.13 -c "import platform,sys; print(sys.executable); print(platform.architecture()[0])"
node --version
npm --version
git --version
Get-Command py,node,npm,git | Select-Object Name,Source
```

Accept the result only when Python reports `3.13.x` and `64bit`, Node reports `20.19.0` or newer, and all four commands resolve to intentional installations. If `py` exists but `py -3.13` fails, repair the Python registration before continuing; `install.ps1` will not fall back to `python` in that situation.

## 4.3 Verify and unpack the BSEW release

Keep the downloaded archive until installation acceptance is complete. In PowerShell, calculate its SHA-256 value before extraction:

```powershell
Get-FileHash -Algorithm SHA256 .\BSEW-release.zip
```

Compare the complete 64-character result with the release SHA-256 supplied through the project handoff. A value printed by the same untrusted archive is not an independent check. Stop if the value differs. After a successful comparison, unblock only that reviewed archive, extract it to a short local path and enter the directory containing `distribution-manifest.json`:

```powershell
Unblock-File .\BSEW-release.zip
New-Item -ItemType Directory -Force C:\BSEW | Out-Null
Expand-Archive -LiteralPath .\BSEW-release.zip -DestinationPath C:\BSEW
Set-Location -LiteralPath C:\BSEW\Building-Stock-Energy-Workbench
Test-Path .\distribution-manifest.json
Test-Path .\scripts\install.ps1
```

Both `Test-Path` commands must print `True`. The folder name in the `Set-Location` command is illustrative: use the actual single release folder created by extraction. Do not continue from a parent directory or from inside the ZIP viewer.

## 4.4 Run the installer

First display the effective PowerShell policies. If the reviewed local scripts are blocked, allow them for the current PowerShell process only. `Process` scope disappears when that window closes and does not modify the machine-wide or current-user policy [6]. An institutional `MachinePolicy` or `UserPolicy` can still take precedence; do not attempt to override it without authorisation.

```powershell
Get-ExecutionPolicy -List
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
.\scripts\install.ps1
```

Do not close the window while installation is running. The script performs these fail-closed stages:

1. verifies Git, Node.js, npm and Python 3.13;
2. creates `.venv` with the selected Python interpreter when it is absent; when an existing environment is not Python 3.13, the installer moves it to a dated backup and creates a compliant replacement;
3. installs the pinned Python requirements and locked frontend packages;
4. builds the local interface;
5. exercises the OpenStudio SDK, model translation, EnergyPlus executable and `pyenergyplus` API; and
6. checks every required release payload item against `distribution-manifest.json`.

Installation succeeds only when the command exits without a red error and its final output contains all three completion signatures:

- `>>> TOOLCHAIN OK <<<`;
- `DISTRIBUTION READY`; and
- `Installation complete. Start with: .\scripts\start.ps1`.

Do not treat an earlier successful dependency download as completion.

## 4.5 Start and check the local service

Start BSEW from the same release directory:

```powershell
.\scripts\start.ps1
```

Keep this PowerShell window open. The launcher should print the loopback address `http://127.0.0.1:8765/#/files` and open it in the default browser. The `127.0.0.1` address is local to the workstation; do not replace it with a public bind address. If Windows Firewall displays a prompt, do not enable public-network access merely to use this loopback service.

In a second PowerShell window, confirm that the service answers without starting a simulation:

```powershell
Invoke-RestMethod http://127.0.0.1:8765/api/health
```

Then use the **Files** page to confirm the four authorised inputs and perform the read-only preflight described in Section 7. Do not start a city run as an installation test. To stop an idle installation, return to the launcher window and press `Ctrl+C` once. If a stock run is active, use **Stop safely** in the Run page and wait for its recorded state before closing the service.

## 4.6 Windows installation acceptance record

Record the following evidence locally. Redact usernames and absolute paths before using a screenshot in a publication.

| Check | Required evidence | Acceptance rule |
|---|---|---|
| Release identity | Archive SHA-256 and receipt date | Exactly matches the independently supplied release value. |
| Host | Windows edition, version and x64 architecture | Native supported Windows 10/11 x64 host. |
| Python | `py -3.13 --version` and architecture output | Python 3.13.x, 64-bit. |
| Frontend toolchain | `node --version` and `npm --version` | Node ≥20.19.0; npm resolves from the intended Node installation. |
| Simulation toolchain | Installer log | OpenStudio 3.11.0, EnergyPlus 25.2.0 and `>>> TOOLCHAIN OK <<<`. |
| Payload | Installer log | `DISTRIBUTION READY`; no missing or changed item. |
| Local service | Printed URL and `/api/health` response | Loopback service responds; no city simulation was started. |
| Qualification status | Release manifest or test record | `executed on Windows` only after a real Windows host passes every check; otherwise `controlled static verification`. |

## 4.7 Windows troubleshooting without weakening controls

- **Python launcher does not select 3.13:** if `py` works but `py -3.13` fails, inspect `py --list`, install/register Python 3.13, reopen PowerShell and repeat Section 4.2. Do not edit the version check.
- **An old virtual environment uses the wrong Python:** stop BSEW, rename `.venv` to a clearly dated backup, and rerun `install.ps1`. Never reuse a virtual environment copied from macOS or Linux.
- **Scripts remain blocked:** inspect `Get-ExecutionPolicy -List`. A Group Policy can override `Process`; request institutional approval rather than changing `LocalMachine` or disabling policy controls.
- **OpenStudio or EnergyPlus is not found:** confirm that OpenStudio 3.11.0 is installed. For a non-standard location, set the compatibility variable for the current window before reinstalling: `$env:VALENCIA_EPLUS_DIR = 'C:\path\to\OpenStudio-3.11.0\EnergyPlus'`.
- **The npm PowerShell shim is blocked:** ensure the Section 4.4 process-scoped policy was applied in the same PowerShell window. Do not download an alternative npm script from an unofficial site.
- **Installation cannot write files:** move the release to a short, user-writable local path such as `C:\BSEW`; do not disable Controlled Folder Access globally.
- **Port 8765 is occupied:** set `$env:WORKBENCH_PORT = '8766'` before `start.ps1`, then use the printed URL.
- **The browser does not open:** set `$env:VALENCIA_NO_BROWSER = '1'`, rerun `start.ps1`, and open its printed loopback URL manually.
- **Interactive jobs belong on another local disk:** before starting, set `$env:VALENCIA_STATE_DIR` and `$env:VALENCIA_RUN_DIR` to stable absolute paths. These variables relocate Workbench state and interactive UI-job output; they do not relocate stock evidence under `out/stock/`. Place the complete release on the intended stock-evidence disk before starting a stock run. Do not disconnect that disk, allow the workstation to sleep, or point two BSEW instances at the same directories.

## 4.8 Controlled verification status

The commands above were checked against the committed `install.ps1`, `start.ps1`, `update.ps1`, `verify_toolchain.py` and `verify_distribution.py` implementations and against the official prerequisite documentation. PowerShell syntax was reviewed statically because this macOS host does not provide a native Windows runtime. Until a Windows host and its evidence are named in the final release manifest, the academically correct status is **controlled static verification — not executed live on Windows**.

# 5. Install on Linux

## 5.1 Prerequisites and compatibility

Install Python 3.13, Node.js 20.19 or newer, Git, and an OpenStudio 3.11.0 package compatible with the distribution. Confirm that the bundled or selected EnergyPlus executable reports 25.2.0. Package names differ by distribution, so the authoritative test is the BSEW toolchain verifier, not the package-manager label.

## 5.2 Install and verify

From the unpacked release directory:

```bash
chmod +x scripts/install.sh scripts/start.sh scripts/update.sh
./scripts/install.sh
```

If OpenStudio is installed in a non-standard location:

```bash
VALENCIA_EPLUS_DIR=/path/to/EnergyPlus ./scripts/install.sh
```

The Linux path uses the same script as macOS and therefore the same fail-closed checks. The final release manifest will state whether Linux was executed live or only controlled against script syntax and official prerequisites.

## 5.3 Display and browser behaviour

The launcher uses `xdg-open` when available. If the browser cannot be opened automatically, navigate to the printed local URL. Set `VALENCIA_NO_BROWSER=1` to suppress automatic opening in managed or headless environments; this does not change the local binding.

> **Platform boundary.** Successful installation on one Linux distribution does not establish binary compatibility for all distributions. Record distribution, architecture, kernel and OpenStudio package source when qualifying a new host.

<!-- pagebreak -->

# 6. Start, stop and configure Workbench storage

## 6.1 Start BSEW

On macOS or Linux:

```bash
./scripts/start.sh
```

On Windows:

```powershell
.\scripts\start.ps1
```

The launcher opens `http://127.0.0.1:8765/#/files` unless automatic browser opening is disabled. Keep the terminal open. The final release covered by this guide will display the product name Building Stock Energy Workbench; technical paths and compatibility variables retain their existing names.

## 6.2 Stop cleanly

Press `Ctrl+C` once in the launcher terminal. This stops the local web service. A stock simulation started by the runner may have its own lifecycle, so use the Run page's **Stop safely** control when you intend to pause a simulation. Do not terminate processes by guessing at a PID.

## 6.3 Move mutable state or interactive job output

The default mutable state is `var/`; operational UI-job output is `out/ui_runs/`. Advanced installations can set existing compatibility variables:

```bash
VALENCIA_STATE_DIR=/path/to/state \
VALENCIA_RUN_DIR=/path/to/runs \
./scripts/start.sh
```

The same variable names are supported in PowerShell. Use stable, absolute destinations on a locally mounted disk. These variables do not relocate stock-run evidence, which the current runner writes below the project's `out/stock/` directory. Provision that directory by locating the complete release on the intended disk before a stock run. Do not point two simultaneously running BSEW instances at the same mutable state directory.

## 6.4 Change the local port

Set `WORKBENCH_PORT` before starting when port 8765 is occupied. Keep the bind local unless an independent security review authorises another topology.

<!-- pagebreak -->

# 7. First launch and installation acceptance

## 7.1 Files page

Open Files and confirm that the required input cards are active. Each imported input is copied to managed content-addressed storage and displayed with its snapshot identity. A run freezes the resolved fingerprints at start; changing an input later does not rewrite existing evidence.

## 7.2 Health and profile readiness

The top bar should report the verified model profile and the environment should show the supported OpenStudio version. Run preflight should be available only when required inputs and profile checks are ready. Investigate warnings; do not reinterpret a blocked state as a cosmetic problem.

## 7.3 Minimal acceptance checklist

- the browser opens only a loopback URL;
- Files reports the expected inputs and snapshot identities;
- the environment reports OpenStudio 3.11.0 and EnergyPlus 25.2.0;
- the verified profile is ready;
- preflight can inspect a selected-building scope without starting a run;
- no unexpected files were created outside the configured state and run roots.

Do not start a city simulation as an installation test. A read-only preflight is sufficient to verify the UI-to-backend contract. A later scientific acceptance run must use its own protocol.

<!-- pagebreak -->

# 8. Update, backup and recovery

## 8.1 Update safely

The updater refuses to continue when tracked code has local changes. It performs a fast-forward-only Git pull, reruns installation, rebuilds the interface and repeats the toolchain and payload checks.

macOS or Linux:

```bash
./scripts/update.sh
```

Windows:

```powershell
.\scripts\update.ps1
```

Never update, rebuild or restart while an active stock run depends on the loaded code and profile. First confirm that the run is complete, its process is gone, and its final aggregate exists.

## 8.2 Backup policy

Back up source, configuration, input fingerprints, run configuration, durable ledger, final aggregate, derived spatial outputs and document manifest. For large full-retention runs, a verified copy of the run root may be more practical than repeatedly generating a full ZIP. Compare SHA-256 values after copying.

## 8.3 Resume and recovery

The stock ledger is flushed per building. When a run is interrupted, use **Resume same run** with the identical run name, scope, inputs and profile. A mismatch refusal protects provenance; do not bypass it. Process absence without a final aggregate indicates an interrupted run, not a completed one.

## 8.4 Retention

Separate immutable research evidence from replaceable caches. Do not delete a run merely because an export exists; verify that the export contains the intended evidence, its manifest signature and checksums validate, and an independent backup is readable.

<!-- pagebreak -->

# 9. Troubleshooting and reporting

| Symptom | Meaning | Correct response |
|---|---|---|
| Python version rejected | The active interpreter is not Python 3.13.x. | Install/select Python 3.13; do not relax the check. |
| OpenStudio or EnergyPlus not found | The supported toolchain is absent or outside discovery paths. | Install OpenStudio 3.11.0 or set `VALENCIA_EPLUS_DIR`; rerun verification. |
| Release payload incomplete | Required data are absent or do not match published identities. | Obtain the authorised full release or import authorised inputs; do not invent replacements. |
| Port 8765 in use | Another service already owns the local port. | Stop the intended service or set `WORKBENCH_PORT`. |
| Preflight blocked | Input, geometry, profile or storage rules rejected the proposed run. | Read the recorded reason and correct the underlying evidence. |
| Resume refused | The run identity, inputs, profile or scope no longer match. | Restore the original identity or start a clearly named new run. |
| Browser does not open | Automatic browser launch failed or was disabled. | Open the printed loopback URL manually. |

When reporting a problem, include operating system, architecture, BSEW commit, Python/OpenStudio/EnergyPlus/Node versions, the failing command, exit code, and a short redacted log excerpt. Do not attach licensed inputs, absolute home-directory paths, access tokens, or an entire run unless authorised.

## References

[1] Python Software Foundation, “Python 3.13 documentation,” https://docs.python.org/3.13/  
[2] NREL, “OpenStudio 3.11.0 release,” https://github.com/NREL/OpenStudio/releases/tag/v3.11.0  
[3] EnergyPlus, “EnergyPlus 25.2 documentation,” https://energyplus.readthedocs.io/en/v25.2.0/  
[4] OpenJS Foundation, “Node.js downloads,” https://nodejs.org/en/download  
[5] Git project, “Git documentation,” https://git-scm.com/doc  
[6] Microsoft, “PowerShell execution policies,” https://learn.microsoft.com/powershell/module/microsoft.powershell.core/about/about_execution_policies  
[7] Python Software Foundation, “Using Python on Windows,” https://docs.python.org/3/using/windows.html  
