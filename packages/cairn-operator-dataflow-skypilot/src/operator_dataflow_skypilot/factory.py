"""The ``skypilot`` entry of ``cairn.dataflow_engines``: core selects the engine by NAME
(#1512), this factory builds it from the adapter's own ``skypilot_*`` backend keys.

Moved out of ``cairn-operator-dataflow`` with the engine (ADR-0134 D3a). The selection read
goes through ``cairn.core.operator.selected_backend`` (ADR-0134 D3b) and falls back to the
environment where no control-plane core is importable.
"""
from __future__ import annotations

from cairn.core.operator import getenv

#: backend key → the env var a box receives it as (ADR-0104 D5, #2073).
_SELECTION_ENV = {
    "skypilot_image": "CAIRN_SKYPILOT_IMAGE",
    "skypilot_use_spot": "CAIRN_SKYPILOT_USE_SPOT",
    "skypilot_accelerator": "CAIRN_SKYPILOT_ACCELERATOR",
}


def _selected(key: str, default: str = "") -> str:
    """One backend-selection plane (#1093): profile record → env var → default."""
    try:
        from cairn.core.operator import selected_backend as selected  # noqa: PLC0415 - absent on a data-plane box
    except Exception:  # noqa: BLE001 — no control-plane core here: env is the contract
        name = _SELECTION_ENV.get(key)
        return (getenv(name, default) if name else default) or default
    return selected(key, default=default) or default


def skypilot_engine(*, kind: str = "data", target: str = ""):
    """The SkyPilot adapter (``cairn.dataflow_engines`` entry ``skypilot``).

    ADR-0104 D5 (#2073): the engine that ACQUIRES the machine. Like the other
    factories it reads its own knobs here rather than in core — the target is
    the compute connection the environment names, and the three
    ``CAIRN_SKYPILOT_*`` keys below are this adapter's alone.
    """
    from operator_dataflow_skypilot.engines_skypilot import SkyPilotEngine  # noqa: PLC0415 - optional [skypilot] extra

    spot = _selected("skypilot_use_spot", "").strip().lower() in ("1", "true", "yes", "on")
    return SkyPilotEngine(
        target,
        use_spot=spot,
        accelerator=_selected("skypilot_accelerator", "").strip(),
        image=_selected("skypilot_image", "").strip(),
        setup=getenv("CAIRN_SKYPILOT_SETUP", "").strip(),
    )
