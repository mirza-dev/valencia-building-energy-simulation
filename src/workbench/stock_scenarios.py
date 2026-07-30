"""Validated orchestration inputs shared by Part C and Part D stock runs."""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Any

from workbench import db, integrity
from workbench.simulation_service import _materialize_snapshot


SOURCE_TYPES = {"human_judgement", "dataset", "publication", "supervisor", "other"}


def weather_datasets() -> list[dict[str, Any]]:
    output = []
    for dataset in db.list_datasets():
        if dataset.get("kind") != "weather" or dataset.get("verification_status") != "VERIFIED":
            continue
        snapshot_hash = dataset.get("snapshot_hash")
        if not snapshot_hash:
            continue
        try:
            snapshot = integrity.load_snapshot(snapshot_hash)
            files = integrity.snapshot_files(snapshot_hash)
        except (OSError, ValueError):
            continue
        source_name = str(snapshot.get("source_name", ""))
        if len(files) != 1 or Path(source_name).suffix.lower() != ".epw":
            continue
        output.append({
            "id": dataset["id"],
            "name": dataset["name"],
            "snapshot_hash": snapshot_hash,
            "source_name": source_name,
        })
    return output


def _delta(value: Any, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not math.isfinite(result) or result < -3.0 or result > 3.0:
        raise ValueError(f"{label} must be between -3.0 and +3.0 K")
    return 0.0 if result == 0 else result


def _weather(dataset_id: str | None) -> dict[str, Any] | None:
    if dataset_id is None:
        return None
    match = next((item for item in weather_datasets() if item["id"] == dataset_id), None)
    if match is None:
        raise ValueError("Stock scenario weather must be a VERIFIED EPW dataset")
    snapshot = integrity.load_snapshot(match["snapshot_hash"])
    return match | {"kind": "weather", "components": snapshot["components"]}


def validate_request(request: dict[str, Any], *, scope: str) -> dict[str, Any]:
    mode = str(request.get("mode", "baseline"))
    if mode not in {"baseline", "scenario"}:
        raise ValueError("Run mode must be baseline or scenario")
    heat_delta = _delta(request.get("heat_delta_c", 0.0), "Heating offset")
    cool_delta = _delta(request.get("cool_delta_c", 0.0), "Cooling offset")
    weather = _weather(request.get("weather_dataset_id"))
    if mode == "baseline":
        if heat_delta or cool_delta or weather:
            raise ValueError("Baseline stock runs cannot contain scenario overrides")
        return {
            "run_mode": "full_baseline",
            "scenario": None,
            "selected_weather": None,
        }

    name = str(request.get("name") or "").strip()
    reason = str(request.get("reason") or "").strip()
    source_type = str(request.get("source_type") or "")
    source_ref = str(request.get("source_ref") or "").strip() or None
    if len(name) < 2 or len(name) > 80:
        raise ValueError("Scenario name must contain 2 to 80 characters")
    if len(reason) < 3 or len(reason) > 500:
        raise ValueError("Scenario rationale must contain 3 to 500 characters")
    if source_type not in SOURCE_TYPES:
        raise ValueError("Scenario source type is invalid")
    if not heat_delta and not cool_delta and weather is None:
        raise ValueError("A stock scenario must change an offset or the EPW snapshot")
    fingerprint_payload = {
        "scope": scope,
        "heat_delta_c": heat_delta,
        "cool_delta_c": cool_delta,
        "weather_snapshot_hash": weather.get("snapshot_hash") if weather else None,
    }
    fingerprint = hashlib.sha256(integrity.canonical_json_bytes(fingerprint_payload)).hexdigest()
    return {
        "run_mode": "full_scenario",
        "scenario": {
            "name": name,
            "heat_delta_c": heat_delta,
            "cool_delta_c": cool_delta,
            "weather_dataset_id": weather.get("id") if weather else None,
            "weather_dataset_name": weather.get("name") if weather else None,
            "weather_snapshot_hash": weather.get("snapshot_hash") if weather else None,
            "reason": reason,
            "source_type": source_type,
            "source_ref": source_ref,
            "fingerprint": fingerprint,
        },
        "selected_weather": weather,
    }


def domain_kwargs(settings: dict[str, Any], scratch: Path) -> dict[str, Any]:
    scenario = settings.get("scenario") or {}
    output: dict[str, Any] = {}
    if scenario.get("heat_delta_c"):
        output["heat_delta"] = float(scenario["heat_delta_c"])
    if scenario.get("cool_delta_c"):
        output["cool_delta"] = float(scenario["cool_delta_c"])
    weather = settings.get("selected_weather")
    if weather:
        output["epw"] = _materialize_snapshot(
            weather["snapshot_hash"], Path(scratch) / "selected_weather",
        )
    return output
