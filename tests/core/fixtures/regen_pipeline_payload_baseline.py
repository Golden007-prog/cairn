"""Regenerate `pipeline_payload_baseline.json` (ADR-0107 slice 0, #2145).

Run this ONLY when a pipeline's shape changed deliberately — never to turn a red
`test_pipeline_payload_baseline` green. Commit the regenerated fixture on its own
and say in the message which pipeline changed and why.

    python tests/core/fixtures/regen_pipeline_payload_baseline.py
"""
from __future__ import annotations

import json
import os
import pathlib

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

from cairn.core.engine.pipeline import plan_pipeline
from cairn.core.engine.pipeline_resolve import all_pipeline_defs
from cairn.core.operator import load_all_operators
from cairn.core.pack.loader import load_all_packs


def main() -> None:
    load_all_operators(strict=False)
    load_all_packs(strict=False)
    out: dict[str, dict] = {}
    for d in all_pipeline_defs():
        try:
            steps = [{"id": s.id, "ref": s.ref, "depends_on": list(s.depends_on),
                      "gate": s.gate or "", "when": s.when or "",
                      "inputs": s.inputs} for s in plan_pipeline(d).steps]
        except Exception as e:  # noqa: BLE001
            steps = [{"__plan_error__": str(e)}]
        out[f"{d.pack}/{d.name}"] = {
            # #1255 — the identity every drift pin is keyed on. Captured
            # because `content_hash` is a hash of `model_dump()`, so ANY new
            # model field that is not `exclude=True` moves it for EVERY
            # pipeline, including ones that declare nothing new.
            "content_hash": d.content_hash,
            "description": getattr(d, "description", "") or "",
            "visibility": getattr(d, "visibility", "public"),
            "inputs": d.inputs or {},
            "governance": d.governance,
            "outputs": d.outputs,
            "plan_steps": steps,
        }
    p = pathlib.Path(__file__).parent / "pipeline_payload_baseline.json"
    with p.open("w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, sort_keys=True, default=str)
        f.write("\n")
    print(f"wrote {len(out)} pipelines -> {p}")


if __name__ == "__main__":
    main()
