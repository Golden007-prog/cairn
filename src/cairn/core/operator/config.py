"""Operator config resolution (ADR-0068, #742).

Layered, per-operator **deployment** config — distinct from per-run wired input.

An operator declares an optional ``config_schema`` (a Pydantic model whose fields
are a *subset* of its ``input_schema`` — same names and nested shape — naming the
deployment-tunable knobs). A profile supplies values under
``[profile.config."<op>"]``; a single run may override via the input key
``config_overrides``. This module resolves the precedence and merges the result
**under** the YAML-resolved operator input, so an explicitly bound ``$input``
always wins and config only supplies the keys the binding left unset.

Precedence (highest wins): explicit bound input → run ``config_overrides`` →
profile ``[profile.config]`` (already ``extends``-merged by the profile loader) →
operator ``config_schema`` / ``input_schema`` field default.

Pure functions, no I/O — the injection site (``invoke_operator_inprocess``) reads
the profile layer from the seeded ``operator_config`` state channel and the run
layer from ``template_input['config_overrides']``.
"""
from __future__ import annotations

import copy
from typing import Any


class OperatorConfigError(ValueError):
    """One or more ``[profile.config."<op>"]`` entries are invalid.

    Raised **fail-closed** by the run-submit preflight (#744) so a malformed
    deployment knob aborts the run *before* node 1 executes — never mid-graph
    after side effects (the enforcement gap Dagster closes by validating the whole
    run config at launch). Carries the per-operator error list for the caller
    (HTTP → 409, trigger routes → 400) and the doctor.
    """

    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__(
            "invalid [profile.config]: " + "; ".join(self.errors)
        )


def validate_operator_configs(profile_config: dict[str, Any] | None) -> list[str]:
    """Validate each ``[profile.config."<op>"]`` table against that operator's
    declared ``config_schema``. Returns a list of human-readable error strings
    (empty ⇒ all valid).

    The single source of truth shared by two callers: the doctor
    (``check_operator_config``, advisory) and the dispatcher run-submit preflight
    (fail-closed, raising :class:`OperatorConfigError`). An unknown operator, an
    operator with no ``config_schema``, or a schema-validation failure (unknown
    key / wrong type — config surfaces forbid extras) each yields one error naming
    the operator and its accepted fields.
    """
    if not profile_config:
        return []
    # Lazy: the loader pulls the pack/entry-point stack — keep it off import.
    from cairn.core.operator import load_all_operators  # noqa: PLC0415
    from cairn.core.operator.registry import default_registry  # noqa: PLC0415

    load_all_operators(strict=False)
    errors: list[str] = []
    for op_name, values in profile_config.items():
        if not default_registry.has(op_name):
            errors.append(
                f'[profile.config."{op_name}"] names an operator that is not installed'
            )
            continue
        schema = getattr(default_registry.get(op_name), "config_schema", None)
        if schema is None:
            errors.append(
                f"operator '{op_name}' declares no config_schema (no tunable knobs) — "
                f'remove [profile.config."{op_name}"]'
            )
            continue
        try:
            schema.model_validate(values)
        except Exception as e:  # noqa: BLE001 — the validation error IS the finding
            fields = ", ".join(sorted(schema.model_fields.keys())) or "(none)"
            errors.append(
                f'[profile.config."{op_name}"] invalid: {e}. Accepts: {fields}'
            )
    return errors


def preflight_operator_configs(profile_config: dict[str, Any] | None) -> None:
    """Run-submit gate: raise :class:`OperatorConfigError` if any
    ``[profile.config]`` entry is invalid. No-op when the profile has no config
    (back-compat). Fail-closed — a misconfigured deployment runs nothing until
    fixed, and the error names the operator + field so the doctor's remediation and
    the run rejection agree."""
    errors = validate_operator_configs(profile_config)
    if errors:
        raise OperatorConfigError(errors)


def deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``over`` onto ``base``; ``over`` wins at each leaf.

    Nested dicts merge key-wise; every other value (including lists) replaces
    wholesale. Returns a new dict — neither input is mutated.
    """
    out = dict(base)
    for k, v in over.items():
        existing = out.get(k)
        if isinstance(v, dict) and isinstance(existing, dict):
            out[k] = deep_merge(existing, v)
        else:
            out[k] = v
    return out


def resolve_operator_config(
    op_name: str,
    *,
    profile_config: dict[str, Any] | None,
    workflow_config: dict[str, Any] | None = None,
    run_overrides: dict[str, Any] | None,
) -> dict[str, Any]:
    """The effective config dict for ``op_name``, resolved across the layers.

    Precedence for OPERATIONAL knobs (ADR-0070): ``run > workflow > profile``
    (most-specific context wins). The env ``profile`` layer is the base; a saved
    ``workflow`` config deep-merges on top; the run ``config_overrides`` on top of
    that. (Governance knobs are only ever set at the env/profile layer — the save
    path rejects them at workflow/run, so ``workflow``/``run`` carry operational
    values only.) All three are keyed by operator name. Returns an empty dict when
    no layer names this operator (the common case — zero overhead → injection skips).
    """
    prof = (profile_config or {}).get(op_name) or {}
    wf = (workflow_config or {}).get(op_name) or {}
    run = (run_overrides or {}).get(op_name) or {}
    if not prof and not wf and not run:
        return {}
    return deep_merge(deep_merge(prof, wf), run)  # run > workflow > profile


def apply_operator_config(
    resolved_input: Any, config: dict[str, Any]
) -> Any:
    """Deep-merge ``config`` **under** ``resolved_input``.

    The explicitly-bound input wins at every leaf; config supplies only the keys
    the binding left unset. A non-dict ``resolved_input`` (shouldn't happen for a
    real operator, whose input is a mapping) is returned unchanged.
    """
    if not config or not isinstance(resolved_input, dict):
        return resolved_input
    return deep_merge(config, resolved_input)  # input over config


# ─── scope: governance vs operational (ADR-0070, #748) ─────────────────────

GOVERNANCE = "governance"
OPERATIONAL = "operational"


def _extra_scope(extra: Any) -> str | None:
    """Read a ``json_schema_extra`` (field- or model-level) for a valid scope."""
    if isinstance(extra, dict):
        s = extra.get("scope")
        if s in (GOVERNANCE, OPERATIONAL):
            return s
    return None


def _extra_recommended(extra: Any) -> Any:
    """Read a field's author-declared ``recommended`` value (#791) from its
    ``json_schema_extra`` — the optimal setting, distinct from the safe
    ``default``. ``None`` when the author declared none (no suggestion shown)."""
    if isinstance(extra, dict) and "recommended" in extra:
        return extra.get("recommended")
    return None


def _model_scope(model_cls: Any) -> str | None:
    cfg = getattr(model_cls, "model_config", None) or {}
    return _extra_scope(cfg.get("json_schema_extra"))


def _as_model(annotation: Any) -> Any | None:
    """Return the ``BaseModel`` subclass an annotation names (unwrapping
    ``Optional[Model]``), else ``None`` — so the walk recurses nested config."""
    import typing  # noqa: PLC0415 — stdlib, one caller

    from pydantic import BaseModel  # noqa: PLC0415 — base dep

    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    for arg in typing.get_args(annotation):
        if isinstance(arg, type) and issubclass(arg, BaseModel):
            return arg
    return None


def _leaf_default(field_info: Any) -> Any:
    from pydantic_core import PydanticUndefined  # noqa: PLC0415 — base dep

    d = getattr(field_info, "default", PydanticUndefined)
    if d is not PydanticUndefined:
        return d
    fac = getattr(field_info, "default_factory", None)
    if fac is not None:
        try:
            return fac()
        except Exception:  # noqa: BLE001 — descriptor only, never fail on it
            return None
    return None


def describe_config_surface(
    schema: Any, *, _prefix: str = "", _inherited: str | None = None
) -> list[dict[str, Any]]:
    """Flatten a ``config_schema`` into leaf knob descriptors — one per tunable
    field (recursing nested models like ``GateConfig.thresholds.*``). Each is
    ``{path, scope, title, description, default}``. A leaf's scope is its field
    ``json_schema_extra['scope']`` → else its containing model's → else
    ``operational`` (unmarked knobs stay freely tunable; guardrails opt into
    ``governance`` explicitly)."""
    out: list[dict[str, Any]] = []
    model_scope = _model_scope(schema) or _inherited
    for name, fi in schema.model_fields.items():
        path = f"{_prefix}{name}"
        field_scope = _extra_scope(getattr(fi, "json_schema_extra", None))
        sub = _as_model(getattr(fi, "annotation", None))
        if sub is not None:
            out.extend(
                describe_config_surface(
                    sub, _prefix=f"{path}.", _inherited=field_scope or model_scope
                )
            )
        else:
            out.append({
                "path": path,
                "scope": field_scope or model_scope or OPERATIONAL,
                "title": getattr(fi, "title", None) or name,
                "description": getattr(fi, "description", None) or "",
                "default": _leaf_default(fi),
                # #791 — author-declared recommended value (optimal), distinct from
                # the safe default; None when unset. Surfaced next to default + a
                # one-click apply; never auto-applied.
                "recommended": _extra_recommended(getattr(fi, "json_schema_extra", None)),
            })
    return out


def partition_by_scope(schema: Any) -> dict[str, list[dict[str, Any]]]:
    """Split a ``config_schema``'s leaf knobs into ``{governance, operational}``
    lists — the routing the layered model (ADR-0070) keys off: governance →
    environment (lockable), operational → workflow/run."""
    fields = describe_config_surface(schema)
    return {
        GOVERNANCE: [f for f in fields if f["scope"] == GOVERNANCE],
        OPERATIONAL: [f for f in fields if f["scope"] == OPERATIONAL],
    }


def strip_governance_inputs(
    resolved_input: Any, config_schema: Any
) -> tuple[Any, list[str]]:
    """Drop any GOVERNANCE-scoped keys the BOUND INPUT carried (#905).

    Governance config (ADR-0070) is env-only — set ONLY through the profile
    ``[profile.config]`` layer, never a per-run value. The ``config_overrides``
    path already rejects governance keys, but a governance field ALSO reachable
    via a direct ``$input.<x>`` binding (e.g. model/gate's ``thresholds`` ←
    ``$default($input.model_gate_thresholds, {})``) slipped through, because the
    bound input wins over config in :func:`apply_operator_config`. That let a run
    weaken an env-owned guardrail (turn ``allow_stub`` back on, ``require_benchmark``
    off, …). This removes those keys from the input BEFORE the config merge, so the
    profile (or the schema default) supplies them. Returns ``(cleaned, dropped)`` —
    the caller logs ``dropped`` (an audit breadcrumb) but proceeds fail-closed."""
    if config_schema is None or not isinstance(resolved_input, dict) or not resolved_input:
        return resolved_input, []
    gov_paths = [f["path"] for f in partition_by_scope(config_schema)[GOVERNANCE]]
    if not gov_paths:
        return resolved_input, []
    cleaned = copy.deepcopy(resolved_input)
    dropped: list[str] = []
    for path in gov_paths:
        if _pop_path(cleaned, path):
            dropped.append(path)
    return cleaned, dropped


def _pop_path(d: dict[str, Any], dotted: str) -> bool:
    """Remove a dotted leaf from a nested dict (True if it existed), pruning any
    parent dict left empty so a leftover ``{}`` can't linger into the merge."""
    parts = dotted.split(".")
    stack: list[tuple[dict[str, Any], str]] = []
    node: Any = d
    for p in parts[:-1]:
        if not isinstance(node, dict) or p not in node:
            return False
        stack.append((node, p))
        node = node[p]
    if not isinstance(node, dict) or parts[-1] not in node:
        return False
    del node[parts[-1]]
    for parent, key in reversed(stack):
        if isinstance(parent.get(key), dict) and not parent[key]:
            del parent[key]
    return True


# ── #1542 — typed scope + registration-time declaration checks ──────────────
#
# Scope was read permissively: `field_scope or model_scope or OPERATIONAL`
# means a typo in the scope VALUE ("governence") — or hand-writing the
# json_schema_extra dict with a typo'd KEY — silently demoted the knob the
# author meant to LOCK into a freely run-tunable operational one. The helpers
# make the declaration impossible to mistype; the validator makes any
# remaining mistake a REGISTRATION error instead of a silent fail-open.


def GovernanceField(default: Any = None, **kwargs: Any) -> Any:  # noqa: N802 — Field-style factory
    """A config field only the ENVIRONMENT may set (ADR-0070).

    ``GovernanceField(0.9, description=...)`` — merges ``scope: governance``
    into ``json_schema_extra`` so authors never hand-write the dict.
    """
    from pydantic import Field  # noqa: PLC0415 — base dep, keeps module import light

    extra = dict(kwargs.pop("json_schema_extra", None) or {})
    extra["scope"] = GOVERNANCE
    return Field(default, json_schema_extra=extra, **kwargs)


def OperationalField(default: Any = None, **kwargs: Any) -> Any:  # noqa: N802 — Field-style factory
    """A config field runs may tune (run > workflow > profile precedence)."""
    from pydantic import Field  # noqa: PLC0415 — base dep

    extra = dict(kwargs.pop("json_schema_extra", None) or {})
    extra["scope"] = OPERATIONAL
    return Field(default, json_schema_extra=extra, **kwargs)


def validate_config_declaration(op_cls: Any) -> None:
    """Registration-time gate over an operator's ``config_schema`` (#1542).

    Raises :class:`OperatorConfigError` (registration fails, the pack does not
    load) when:

    - a ``scope`` is declared with an INVALID value — the case that used to
      silently default to operational, turning a meant-to-be-locked knob
      run-tunable;
    - a ``config_schema`` field does not exist on ``input_schema`` — the
      documented ``config_schema ⊂ input_schema`` convention
      (``protocol.py``), previously enforced by nobody: config for a field
      the operator never reads is dead weight that LOOKS governed.

    Type compatibility across the two declarations is deliberately not
    enforced here — the schemas legitimately differ in optionality (config
    fields default, input fields may be required), and a strict annotation
    match would reject valid operators. Presence is the load-bearing check.
    """
    schema = getattr(op_cls, "config_schema", None)
    if schema is None:
        return
    name = getattr(op_cls, "name", op_cls.__name__)

    def _check_scopes(model_cls: Any, prefix: str = "") -> None:
        cfg = getattr(model_cls, "model_config", None) or {}
        extra = cfg.get("json_schema_extra")
        if isinstance(extra, dict) and "scope" in extra and _extra_scope(extra) is None:
            raise OperatorConfigError([
                f"operator {name!r}: config_schema {model_cls.__name__} declares "
                f"model-level scope {extra.get('scope')!r} — must be "
                f"'{GOVERNANCE}' or '{OPERATIONAL}'"
            ])
        for fname, fi in model_cls.model_fields.items():
            fextra = getattr(fi, "json_schema_extra", None)
            if isinstance(fextra, dict) and "scope" in fextra and _extra_scope(fextra) is None:
                raise OperatorConfigError([
                    f"operator {name!r}: config field '{prefix}{fname}' declares "
                    f"scope {fextra.get('scope')!r} — must be '{GOVERNANCE}' or "
                    f"'{OPERATIONAL}'. A typo here used to silently mean "
                    f"operational (freely run-tunable)."
                ])
            if (sub := _as_model(fi.annotation)) is not None:
                _check_scopes(sub, prefix=f"{prefix}{fname}.")

    _check_scopes(schema)

    input_schema = getattr(op_cls, "input_schema", None)
    if input_schema is not None:
        missing = [
            f for f in schema.model_fields
            if f not in input_schema.model_fields
        ]
        if missing:
            raise OperatorConfigError([
                f"operator {name!r}: config_schema field(s) {missing} do not "
                f"exist on input_schema {input_schema.__name__} — config_schema "
                f"must be a subset of input_schema (protocol convention), or the "
                f"declared knob is dead weight that LOOKS governed"
            ])
