"""Engine-neutral helpers every remote dataflow engine shares (ADR-0134 D3b, #2678).

They lived in the Metaflow engine and the SkyPilot engine imported them from there. With
the two engines in separate wheels that import would be an operator pack → operator pack
edge, so the pure parts live here, on the contract side both engines already import:

- :func:`gang_workers` and :func:`aggregate_resources` read a :class:`DataflowSpec`'s
  resource hints;
- :func:`essential_env` is the environment allowlist a remote task gets — the dataflow
  spec/lineage contract, the live channel, and the process basics. An engine passes its
  OWN variable prefixes (Metaflow's ``METAFLOW_``), so this module names no engine.
"""
from __future__ import annotations

from cairn.core.dataflow.contract import DataflowSpec


def gang_workers(spec: DataflowSpec) -> int:
    """The coordinated gang size the spec asks for (#1366): the FIRST stage's
    ``resources.workers``, or 1 (not distributed). An engine's ``validate()`` refuses
    workers on any other stage, so first-stage-only is a checked contract, not a silent
    truncation."""
    if not spec.stages:
        return 1
    res = spec.stages[0].resources
    return int(getattr(res, "workers", None) or 1) if res else 1


def aggregate_resources(spec: DataflowSpec) -> dict[str, int]:
    """Collapse per-stage :class:`StageResources` hints to ONE envelope: the max
    cpu/memory/gpu across stages (ADR-0089 #1346).

    The whole dataflow runs as one remote step, so that step must fit its HEAVIEST stage.
    ``memory`` is in MB. ``gpu`` is included only when some stage asked for >0: ``gpu=0``
    means "explicitly none" on the contract, and requesting gpu=0 from a scheduler is noise,
    not a constraint.
    """
    out: dict[str, int] = {}
    for ref in spec.stages:
        r = ref.resources
        if r is None:
            continue
        if r.cpu:
            out["cpu"] = max(out.get("cpu", 0), int(r.cpu))
        if r.memory_mb:
            out["memory"] = max(out.get("memory", 0), int(r.memory_mb))
        if r.gpu:
            out["gpu"] = max(out.get("gpu", 0), int(r.gpu))
    return out


#: What a remote task genuinely cannot run without (interpreter, toolchain, locale) plus the
#: namespaces that ARE the engine contract. Everything else — cloud credentials, tokens, DSNs
#: the control plane happens to hold — is withheld and must arrive as a declared connection.
#:
#: Prefixes every engine forwards. ``CAIRN_CONN_`` is the namespace an engine mints for the
#: connections a submission declares (#1344): nothing ambient can be in it that the engine did
#: not put there for this submission.
#:
#: ``CAIRN_`` as a blanket prefix is NOT safe and is not here: the engine needs the specific
#: names below, and the prefix would admit every cairn-namespaced value the control plane
#: happens to hold — a wildcard hole in the middle of an allowlist (ADR-0089 acceptance run,
#: #1344).
ESSENTIAL_ENV_PREFIXES: tuple[str, ...] = ("CAIRN_CONN_",)
ESSENTIAL_ENV_NAMES: frozenset[str] = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "LC_ALL",
    "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE", "TZ",
    # the engine's spec/lineage contract — enumerated, never prefix-matched
    "CAIRN_DATAFLOW_SPEC", "CAIRN_FIX_SPEC", "CAIRN_SUBMIT_CWD",
    "CAIRN_PARENT_RUN", "CAIRN_RUN_ID", "CAIRN_CHECKPOINT_DIR",
    # #2025 — the live channel a kernel reports on (set per submit); the OBS_ job-channel
    # twins ride beside the CAIRN_ names for pinned box images and go at 1.0 (#2429)
    "CAIRN_NODE", "CAIRN_ENGINE_URL", "CAIRN_ENGINE_TOKEN",
    "CAIRN_JOB_CHANNEL", "CAIRN_JOB_CHANNEL_STREAM", "OBS_JOB_CHANNEL", "OBS_JOB_CHANNEL_STREAM",
})


class EssentialEnv:
    """``k in env`` — the exact names plus the allowed prefixes. Membership is the only
    operation it offers: it is a rule, not a collection, so it does not pretend to be a set
    (two allowlists with different prefixes must never compare equal)."""

    __slots__ = ("prefixes",)

    def __init__(self, prefixes: tuple[str, ...]) -> None:
        self.prefixes = prefixes

    def __contains__(self, key: object) -> bool:
        k = str(key)
        return k in ESSENTIAL_ENV_NAMES or k.startswith(self.prefixes)

    def __repr__(self) -> str:
        return f"EssentialEnv(prefixes={self.prefixes!r})"


def essential_env(extra_prefixes: tuple[str, ...] = ()) -> EssentialEnv:
    """The allowlist a remote task's environment is filtered through: the engine-neutral
    names and prefixes, plus the engine's own ``extra_prefixes`` (Metaflow passes
    ``METAFLOW_`` and its cairn spellings). A bare string is refused: iterated, it would
    become one-letter prefixes and admit nearly everything."""
    if isinstance(extra_prefixes, str) or not all(isinstance(p, str) and p for p in extra_prefixes):
        raise TypeError("extra_prefixes must be a tuple of non-empty prefix strings")
    return EssentialEnv((*ESSENTIAL_ENV_PREFIXES, *extra_prefixes))


__all__ = [
    "ESSENTIAL_ENV_NAMES",
    "ESSENTIAL_ENV_PREFIXES",
    "EssentialEnv",
    "aggregate_resources",
    "essential_env",
    "gang_workers",
]
