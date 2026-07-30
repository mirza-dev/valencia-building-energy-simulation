"""Entry point for one isolated model-build job."""

from __future__ import annotations

import sys
import traceback

from workbench import db
from workbench.service import run_build_job
from workbench.simulation_service import run_simulation_job
from workbench.neighborhood_service import run_neighborhood_job
from workbench.city_service import run_city_job
from workbench.lhs_service import run_lhs_job
from workbench.scenario_service import run_scenario_job


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python -m workbench.worker <job-id>", file=sys.stderr)
        return 2
    db.init_db()
    try:
        job = db.get_job(sys.argv[1])
        if job is None:
            raise KeyError(sys.argv[1])
        if job["kind"] == "simulation":
            run_simulation_job(job["id"])
        elif job["kind"] == "scenario":
            run_scenario_job(job["id"])
        elif job["kind"] == "neighborhood":
            run_neighborhood_job(job["id"])
        elif job["kind"] == "city":
            run_city_job(job["id"])
        elif job["kind"] == "lhs":
            run_lhs_job(job["id"])
        else:
            run_build_job(job["id"])
        return 0
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
