"""App logging + the structured decision log.

Two outputs:
- normal app log (console + rotating file) for operations
- decisions.jsonl: one JSON object per screening decision, append-only —
  this is the on-disk audit trail required by the spec and the calibration
  data source together with the `detections` table.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import time
from pathlib import Path

_decision_logger: logging.Logger | None = None


def setup(log_dir: str, level: str = "INFO") -> None:
    global _decision_logger
    Path(log_dir).mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level.upper())
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    app_file = logging.handlers.RotatingFileHandler(
        Path(log_dir) / "gatekeeper.log", maxBytes=10_000_000, backupCount=5,
        encoding="utf-8",
    )
    app_file.setFormatter(fmt)
    root.addHandler(app_file)

    _decision_logger = logging.getLogger("gatekeeper.decisions")
    _decision_logger.setLevel(logging.INFO)
    _decision_logger.propagate = False
    decision_file = logging.handlers.RotatingFileHandler(
        Path(log_dir) / "decisions.jsonl", maxBytes=50_000_000, backupCount=10,
        encoding="utf-8",
    )
    decision_file.setFormatter(logging.Formatter("%(message)s"))
    _decision_logger.addHandler(decision_file)


def log_decision(record: dict) -> None:
    record = {"ts": time.time(), **record}
    if _decision_logger is not None:
        _decision_logger.info(json.dumps(record, ensure_ascii=False, default=str))
    else:  # decisions must never be silently lost, even if setup() wasn't called
        logging.getLogger(__name__).info("DECISION %s", record)
