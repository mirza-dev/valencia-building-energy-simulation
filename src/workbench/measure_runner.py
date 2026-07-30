"""Session-scoped OpenStudio ModelMeasure catalog, import, and execution."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree

import openstudio

from workbench import integrity


BUILTIN_ROOT = Path(__file__).with_name("measures")
DEFAULT_CLI = Path("/Applications/OpenStudio-3.11.0/bin/openstudio")
MAX_ARCHIVE_BYTES = 25 * 1024 * 1024
MAX_EXPANDED_BYTES = 75 * 1024 * 1024
MAX_ARCHIVE_FILES = 500
MEASURE_TIMEOUT_SECONDS = int(os.environ.get("WORKBENCH_MEASURE_TIMEOUT_SECONDS", "300"))


class MeasureRejected(ValueError):
    """A measure package or execution that cannot safely produce an authored OSM."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _text(parent: ElementTree.Element, path: str, default: str = "") -> str:
    node = parent.find(path)
    return default if node is None or node.text is None else node.text.strip()


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(bytes.fromhex(integrity.sha256_file(path)))
    return digest.hexdigest()


def _measure_type(root: ElementTree.Element) -> str:
    for attribute in root.findall("./attributes/attribute"):
        if _text(attribute, "name") == "Measure Type":
            return _text(attribute, "value")
    return ""


def _argument(argument: ElementTree.Element) -> dict[str, Any]:
    choices = []
    for choice in argument.findall("./choices/choice"):
        value = _text(choice, "value")
        choices.append({"value": value, "display_name": _text(choice, "display_name", value)})
    default = _text(argument, "default_value")
    return {
        "name": _text(argument, "name"),
        "display_name": _text(argument, "display_name", _text(argument, "name")),
        "description": _text(argument, "description"),
        "type": _text(argument, "type", "String"),
        "units": _text(argument, "units") or None,
        "required": _text(argument, "required", "false").casefold() == "true",
        "model_dependent": _text(argument, "model_dependent", "false").casefold() == "true",
        "default_value": default if default != "" else None,
        "choices": choices,
    }


def inspect_measure(path: Path, measure_id: str, source: str) -> dict[str, Any]:
    xml_path = path / "measure.xml"
    if not xml_path.is_file():
        raise MeasureRejected("measure_xml", "The measure package has no measure.xml")
    try:
        root = ElementTree.parse(xml_path).getroot()
    except (ElementTree.ParseError, OSError) as exc:
        raise MeasureRejected("measure_xml", f"measure.xml is invalid: {exc}") from exc
    measure_type = _measure_type(root)
    if measure_type != "ModelMeasure":
        raise MeasureRejected("measure_type", "Phase 3 applies OpenStudio ModelMeasure packages only")
    language = "Ruby" if (path / "measure.rb").is_file() else "Python" if (path / "measure.py").is_file() else ""
    if not language:
        raise MeasureRejected("measure_script", "The ModelMeasure has neither measure.rb nor measure.py")
    digest = _tree_hash(path)
    return {
        "id": measure_id,
        "name": _text(root, "name", path.name),
        "display_name": _text(root, "display_name", _text(root, "name", path.name)),
        "description": _text(root, "description"),
        "modeler_description": _text(root, "modeler_description"),
        "measure_type": measure_type,
        "language": language,
        "source": source,
        "sha256": digest,
        "arguments": [_argument(item) for item in root.findall("./arguments/argument")],
    }


def _measure_dirs(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted({path.parent for path in root.rglob("measure.xml")}, key=lambda item: item.as_posix())


def measure_catalog(session_root: Path) -> list[dict[str, Any]]:
    items = [inspect_measure(path, f"builtin:{path.name}", "builtin") for path in _measure_dirs(BUILTIN_ROOT)]
    uploaded = session_root / "uploaded_measures"
    for path in _measure_dirs(uploaded):
        digest = _tree_hash(path)
        items.append(inspect_measure(path, f"uploaded:{digest[:16]}", "uploaded"))
    return sorted(items, key=lambda item: (item["source"], item["display_name"].casefold(), item["id"]))


def resolve_measure(session_root: Path, measure_id: str) -> tuple[Path, dict[str, Any]]:
    for item in measure_catalog(session_root):
        if item["id"] != measure_id:
            continue
        roots = BUILTIN_ROOT if item["source"] == "builtin" else session_root / "uploaded_measures"
        path = next((candidate for candidate in _measure_dirs(roots) if _tree_hash(candidate) == item["sha256"]), None)
        if path is not None:
            return path, item
    raise MeasureRejected("measure_missing", f"Unknown session measure: {measure_id}")


def _validated_archive_entries(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    entries = archive.infolist()
    if len(entries) > MAX_ARCHIVE_FILES:
        raise MeasureRejected("measure_archive", f"Measure ZIP exceeds {MAX_ARCHIVE_FILES} entries")
    if sum(item.file_size for item in entries) > MAX_EXPANDED_BYTES:
        raise MeasureRejected("measure_archive", "Expanded measure ZIP exceeds 75 MB")
    for item in entries:
        path = PurePosixPath(item.filename)
        mode = (item.external_attr >> 16) & 0o170000
        if path.is_absolute() or ".." in path.parts or mode == stat.S_IFLNK:
            raise MeasureRejected("measure_archive", f"Unsafe ZIP entry: {item.filename}")
    return entries


def import_measure_archive(session_root: Path, archive_path: Path) -> dict[str, Any]:
    if archive_path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise MeasureRejected("measure_archive", "Measure ZIP exceeds 25 MB")
    staging = session_root / f"measure-import-{uuid.uuid4().hex}"
    staging.mkdir(parents=True, exist_ok=False)
    try:
        try:
            with zipfile.ZipFile(archive_path) as archive:
                _validated_archive_entries(archive)
                archive.extractall(staging)
        except zipfile.BadZipFile as exc:
            raise MeasureRejected("measure_archive", "Uploaded measure is not a valid ZIP") from exc
        candidates = _measure_dirs(staging)
        if len(candidates) != 1:
            raise MeasureRejected("measure_archive", "Measure ZIP must contain exactly one measure.xml")
        candidate = candidates[0]
        digest = _tree_hash(candidate)
        destination = session_root / "uploaded_measures" / digest[:16] / candidate.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            shutil.copytree(candidate, destination)
        return inspect_measure(destination, f"uploaded:{digest[:16]}", "uploaded")
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _source_archive(root: Path, session_root: Path, digest: str) -> Path:
    archive_path = session_root / "measure_sources" / f"measure-{digest[:16]}.zip"
    if archive_path.exists():
        return archive_path
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = archive_path.with_suffix(".tmp")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            info = zipfile.ZipInfo(path.relative_to(root).as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())
    temporary.replace(archive_path)
    return archive_path


def _openstudio_cli() -> Path:
    configured = os.environ.get("WORKBENCH_OPENSTUDIO_CLI")
    candidate = Path(configured).expanduser() if configured else DEFAULT_CLI
    if candidate.is_file():
        return candidate.resolve()
    found = shutil.which("openstudio")
    if found:
        return Path(found).resolve()
    raise MeasureRejected("measure_runtime", "OpenStudio CLI was not found")


def apply_model_measure_local(osm_path: Path, output_path: Path, session_root: Path,
                              measure_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
    measure_dir, info = resolve_measure(session_root, measure_id)
    known = {item["name"] for item in info["arguments"]}
    unknown = sorted(set(arguments) - known)
    if unknown:
        raise MeasureRejected("measure_arguments", "Unknown measure argument(s): " + ", ".join(unknown))
    missing = [item["name"] for item in info["arguments"] if item["required"] and item["default_value"] is None and item["name"] not in arguments]
    if missing:
        raise MeasureRejected("measure_arguments", "Required measure argument(s) missing: " + ", ".join(missing))

    run_root = session_root / "measure_runs" / uuid.uuid4().hex
    run_root.mkdir(parents=True, exist_ok=False)
    osw = {
        "seed_file": str(Path(osm_path).resolve()),
        "measure_paths": [str(measure_dir.parent.resolve())],
        "steps": [{"measure_dir_name": measure_dir.name, "arguments": arguments}],
    }
    workflow_path = run_root / "workflow.osw"
    workflow_path.write_text(json.dumps(osw, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        completed = subprocess.run(
            [str(_openstudio_cli()), "run", "-w", str(workflow_path)],
            cwd=run_root, capture_output=True, text=True, timeout=MEASURE_TIMEOUT_SECONDS, check=False,
        )
        result_osw_path = run_root / "out.osw"
        result_osw = json.loads(result_osw_path.read_text(encoding="utf-8")) if result_osw_path.is_file() else {}
        if completed.returncode != 0 or result_osw.get("completed_status") not in {"Success", None}:
            message = result_osw.get("completed_status") or completed.stderr[-2000:] or completed.stdout[-2000:]
            raise MeasureRejected("measure_failed", f"OpenStudio Measure failed: {message}")
        produced = next((path for path in (
            run_root / "run" / "in.osm", run_root / "run" / "out.osm", run_root / "in.osm",
        ) if path.is_file()), None)
        if produced is None:
            raise MeasureRejected("measure_output", "ModelMeasure completed without a materialized OSM")
        translated = openstudio.osversion.VersionTranslator().loadModel(openstudio.toPath(str(produced)))
        if translated.isNull():
            raise MeasureRejected("measure_output", "ModelMeasure output cannot be loaded by OpenStudio")
        shutil.copy2(produced, output_path)
        archive = _source_archive(measure_dir, session_root, info["sha256"])
        step_values = result_osw.get("steps") or []
        step_result = step_values[-1].get("result", {}) if step_values else {}
        return {
            "measure": info,
            "arguments": arguments,
            "openstudio_cli": str(_openstudio_cli()),
            "openstudio_version": openstudio.openStudioVersion(),
            "exit_code": completed.returncode,
            "step_result": step_result,
            "stdout_tail": completed.stdout[-4000:],
            "stderr_tail": completed.stderr[-4000:],
            "source_archive": str(archive),
            "source_archive_sha256": integrity.sha256_file(archive),
            "output_osm_sha256": integrity.sha256_file(output_path),
        }
    except subprocess.TimeoutExpired as exc:
        raise MeasureRejected("measure_timeout", f"OpenStudio Measure exceeded {MEASURE_TIMEOUT_SECONDS} seconds") from exc
    finally:
        shutil.rmtree(run_root, ignore_errors=True)


def apply_model_measure(osm_path: Path, output_path: Path, session_root: Path,
                        measure_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Run a ModelMeasure in a clean helper process so CLI failures cannot drop Workbench."""
    worker_root = session_root / "measure_workers" / uuid.uuid4().hex
    worker_root.mkdir(parents=True, exist_ok=False)
    request_path = worker_root / "request.json"
    response_path = worker_root / "response.json"
    request_path.write_text(json.dumps({
        "osm_path": str(Path(osm_path).resolve()),
        "output_path": str(Path(output_path).resolve()),
        "session_root": str(session_root.resolve()),
        "measure_id": measure_id,
        "arguments": arguments,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    environment = os.environ.copy()
    source_root = str(Path(__file__).resolve().parents[1])
    environment["PYTHONPATH"] = source_root + (os.pathsep + environment["PYTHONPATH"] if environment.get("PYTHONPATH") else "")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        completed = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("measure_worker.py")), str(request_path), str(response_path)],
            capture_output=True, text=True, timeout=MEASURE_TIMEOUT_SECONDS + 45, check=False, env=environment,
        )
        if not response_path.is_file():
            evidence = completed.stderr[-2000:] or completed.stdout[-2000:] or f"exit {completed.returncode}"
            raise MeasureRejected("measure_worker", f"Isolated Measure worker failed: {evidence}")
        response = json.loads(response_path.read_text(encoding="utf-8"))
        if not response.get("ok"):
            raise MeasureRejected(str(response.get("code") or "measure_failed"), str(response.get("message") or "Measure failed"))
        if completed.returncode != 0:
            raise MeasureRejected("measure_worker", f"Isolated Measure worker exited {completed.returncode}")
        return response["result"]
    except subprocess.TimeoutExpired as exc:
        raise MeasureRejected("measure_timeout", f"Isolated Measure worker exceeded {MEASURE_TIMEOUT_SECONDS + 45} seconds") from exc
    finally:
        shutil.rmtree(worker_root, ignore_errors=True)
