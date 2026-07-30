"""Clean-process entry point for one OpenStudio ModelMeasure execution."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from workbench.measure_runner import MeasureRejected, apply_model_measure_local


def main(request_path: Path, response_path: Path) -> int:
    request = json.loads(request_path.read_text(encoding="utf-8"))
    try:
        result = apply_model_measure_local(
            Path(request["osm_path"]), Path(request["output_path"]), Path(request["session_root"]),
            str(request["measure_id"]), dict(request.get("arguments") or {}),
        )
        payload = {"ok": True, "result": result}
    except MeasureRejected as exc:
        payload = {"ok": False, "code": exc.code, "message": str(exc)}
    except Exception as exc:  # pragma: no cover - last-resort worker boundary
        payload = {"ok": False, "code": "measure_worker", "message": str(exc)}
    response_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if payload["ok"] else 2


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: measure_worker.py REQUEST.json RESPONSE.json")
    raise SystemExit(main(Path(sys.argv[1]), Path(sys.argv[2])))
