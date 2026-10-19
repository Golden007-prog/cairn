"""Regenerate `readiness_baseline.json` (#2242 V1-B/V1-C, decision C2a).

Run this ONLY for a deliberate behaviour change of `dataset_readiness/readiness` — never to
turn a red `test_readiness_baseline` green (the migrations V1-B and V1-C must leave it
unchanged). Commit the regenerated fixture on its own and say why it changed.

    python tests/core/fixtures/regen_readiness_baseline.py
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))

from tests._platform_layout import DEV_PROFILE  # noqa: E402
from tests.core.test_readiness_baseline import GOLDEN, run_baseline  # noqa: E402


def _dump(value, indent: int = 0) -> str:
    """indent=1 JSON with every container that fits on one line kept on one line."""
    flat = json.dumps(value, sort_keys=True)
    if len(flat) <= 100 or not isinstance(value, (dict, list)) or not value:
        return flat
    pad = " " * (indent + 1)
    if isinstance(value, dict):
        body = ",\n".join(f"{pad}{json.dumps(k)}: {_dump(v, indent + 1)}" for k, v in sorted(value.items()))
        return "{\n" + body + "\n" + " " * indent + "}"
    return "[\n" + ",\n".join(pad + _dump(v, indent + 1) for v in value) + "\n" + " " * indent + "]"


def main() -> None:
    os.environ["OBS_PROFILE"] = str(DEV_PROFILE)  # what the test's `platform_dev_profile` sets
    with tempfile.TemporaryDirectory() as tmp:
        out = run_baseline(pathlib.Path(tmp))
    GOLDEN.write_text(_dump(out) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
