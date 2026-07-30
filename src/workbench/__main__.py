from __future__ import annotations

import os
import uvicorn

from workbench.environment import validate_runtime_environment


if __name__ == "__main__":
    environment = validate_runtime_environment()
    uvicorn.run(
        "workbench.api:app", host="127.0.0.1",
        port=int(environment["port"]), reload=False,
    )
