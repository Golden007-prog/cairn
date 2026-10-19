"""The ``metaflow`` entry of ``cairn.dataflow_engines``: core selects the engine by NAME
(#1512), this factory builds it from the engine's own ``metaflow_*`` backend keys.

Moved out of ``cairn-operator-dataflow`` with the engine (ADR-0134 D2). The selection read
goes through ``cairn.core.operator.selected_backend`` (ADR-0134 D3b) — the operator contract —
and falls back to the environment where no control-plane core is importable (a worker box
receives the selection as env, never as a profile).
"""
from __future__ import annotations

from cairn.core.operator import getenv

#: backend key → the env var a box receives it as; the same names the profile schema declares,
#: so box-side env and control-side profile agree (ADR-0089, #1345).
_SELECTION_ENV = {
    "metaflow_metadata_root": "CAIRN_METAFLOW_ROOT",
    "metaflow_datastore": "CAIRN_METAFLOW_DATASTORE",
    "metaflow_compute": "CAIRN_METAFLOW_COMPUTE",
    "metaflow_compute_image": "CAIRN_METAFLOW_IMAGE",
    "metaflow_compute_queue": "CAIRN_METAFLOW_QUEUE",
    "metaflow_retry_times": "CAIRN_METAFLOW_RETRY",
    "metaflow_secrets": "CAIRN_METAFLOW_SECRETS",
    "metaflow_env_isolation": "CAIRN_METAFLOW_ISOLATION",
    "metaflow_datastore_root": "CAIRN_METAFLOW_DATASTORE_ROOT",
    "metaflow_metadata_service": "CAIRN_METAFLOW_SERVICE_URL",
}


def _selected(key: str, default: str = "") -> str:
    """One backend-selection plane (#1093): profile record → env var → default."""
    try:
        from cairn.core.operator import selected_backend as selected  # noqa: PLC0415 - absent on a data-plane box
    except Exception:  # noqa: BLE001 — no control-plane core here: env is the contract
        name = _SELECTION_ENV.get(key)
        return (getenv(name, default) if name else default) or default
    return selected(key, default=default) or default


def metaflow_engine(*, kind: str = "data", target: str = ""):
    """The Metaflow adapter (``cairn.dataflow_engines`` entry ``metaflow``).

    #1078 — ONE adapter serves BOTH kinds (ADR-0083): the processing power is in
    the kernels; Metaflow supplies the SUBSTRATE (ADR-0089 — remote scheduling,
    @resources/@retry, datastore), attached at run time so the bridge flow stays
    the one static CairnFlow (D1).

    This factory reads its own eight ``metaflow_*`` backend keys, which is
    exactly why construction stayed in the pack when selection moved to core
    (#1512): core would otherwise have to know every adapter's configuration.
    """
    from operator_dataflow_metaflow.engines_metaflow import MetaflowEngine  # noqa: PLC0415 - optional [metaflow] extra

    # Identity/compute split (#1345): metadata root has its own key now; a bare
    # `<kind>_target` still works as the root (pre-ADR-0089 profiles overloaded
    # it — honored so existing deployments keep re-attaching).
    mf_root = _selected("metaflow_metadata_root", "").strip() or (target or "").strip()
    compute_kind = _selected("metaflow_compute", "local").strip().lower() or "local"
    compute = {
        "kind": compute_kind,
        "image": _selected("metaflow_compute_image", "").strip(),
        "queue": _selected("metaflow_compute_queue", "").strip(),
    }
    retry_raw = _selected("metaflow_retry_times", "").strip()
    try:
        retry_times = int(retry_raw) if retry_raw else 0
    except ValueError:
        # a typo'd retry count must not silently drop retries OR crash the
        # dispatch path — surface it through validate() at submit instead.
        retry_times = -1
    secrets = [s.strip() for s in _selected("metaflow_secrets", "").split(",") if s.strip()]
    return MetaflowEngine(
        metadata_root=mf_root or None,
        datastore=_selected("metaflow_datastore", "").strip() or None,
        compute=compute,
        retry_times=retry_times,
        # #1344 — which connections a flow may use, and how much of the control
        # plane's env it inherits.
        secrets=secrets,
        env_isolation=_selected("metaflow_env_isolation", "auto").strip().lower() or "auto",
        # #1349 — what makes a run readable/resumable from ANOTHER box.
        datastore_root=_selected("metaflow_datastore_root", "").strip() or None,
        metadata_service=_selected("metaflow_metadata_service", "").strip() or None,
    )
