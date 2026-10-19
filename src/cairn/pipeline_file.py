"""``cairn.pipeline_file`` — the one-file pipeline surface (#1092, ADR-0083 D5/D8 phase 2).

Named for what it *is* (#1545, ADR-0092 principle 4). It used to be
``cairn.sdk``, one letter of punctuation away from ``cairn_sdk`` — the
*authoring* projection that compiles classes to pack records (ADR-0087) and
ships as its own wheel. Two unrelated surfaces, near-identical names, and the
`pack build` / `run` split between them was a coin-flip for any reader. This
module's own vocabulary (``load_pipeline_file``, ``PipelineFileError``) already
said what it was; the module name now agrees. The deprecated ``cairn.sdk``
re-export was removed before the first public release (#2425).

The **conversion slice**, deliberately minimal: a declarative :class:`Pipeline`
base a single ``.py`` file can subclass, plus the loader ``cairn run <file.py>``
uses. Its whole job is to make `pip install` → one file → a governed run with a
gate firing take under ten minutes (goal 1 of the SDK design).

**What this is NOT (yet).** No decorators, no materializer, no lowering, no
``flow.native``, no inline ``@task`` lifting. Those are the full authoring
surface, which ADR-0083 puts in the *do-on-pull* tier behind hard prerequisites
(one expression language, the typed state contract, inline bindings — R6). A
pipeline here **references installed capability and never defines new
capability**: `workflow` names a template the packs already ship, so the pack
stays the governed record and the file is a runnable, diffable *projection* of it.

That constraint is what keeps this honest — everything the file runs has already
been risk-scanned, gate-checked, and locked as pack data. The file adds
ergonomics (one place for inputs, environment, series, triggers), not authority.

Round-trip: ``cairn export <workflow> > file.py`` renders one of these;
``cairn run file.py`` loads it and executes the same run the pack path produces —
same run record, same gates, same console visibility (install-then-run, so the
workflow registry and UIContract stay intact — P8).
"""
from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path
from typing import Any, Optional


class Pipeline:
    """Declarative pipeline: subclass it and set class attributes.

    Every attribute is optional except :attr:`workflow`. The class docstring
    becomes the pipeline's description.

    ```python
    class Finetune(Pipeline):
        '''Weekly governed fine-tune.'''
        workflow = "training/finetune"      # a ref into an INSTALLED pack
        env      = "dev"
        series   = "weekly-finetune"

        class Inputs:
            dataset_path: str = "databricks/databricks-dolly-15k"
            base_model: str = "HuggingFaceTB/SmolLM2-135M"
    ```
    """

    #: ``<pack>/<template>`` — the installed workflow this pipeline runs. Required.
    workflow: str = ""

    #: Default policy environment (``dev`` / ``staging`` / ``production``).
    #: ``cairn run --env`` overrides it.
    env: str = "dev"

    #: Optional series id — the grouping runs are attached to for budget +
    #: comparison (ADR-0076 D5).
    series: str = ""

    #: Free-form notes carried for the reader; never interpreted.
    notes: str = ""

    class Inputs:
        """Run inputs as annotated class attributes; values are the defaults.

        Rendered from the workflow's own input JSON-Schema on export, so the
        defaults you see are the ones the pack declares.
        """

    # ── introspection used by the CLI (never override) ───────────────────────

    @classmethod
    def input_values(cls) -> dict[str, Any]:
        """The concrete input dict, from :class:`Inputs`' non-dunder attributes.

        Annotated-but-unset names are omitted rather than sent as ``None`` — a
        required input with no default must come from ``--input``/``--set``, and
        a fabricated ``None`` would fail schema validation with a confusing error.
        """
        out: dict[str, Any] = {}
        inputs = getattr(cls, "Inputs", None)
        if inputs is None:
            return out
        for name in list(getattr(inputs, "__annotations__", {}) or {}) + [
            n for n in vars(inputs) if not n.startswith("_")
        ]:
            if name.startswith("_") or name in out:
                continue
            if hasattr(inputs, name):
                out[name] = getattr(inputs, name)
        return out

    @classmethod
    def description(cls) -> str:
        return inspect.cleandoc(cls.__doc__ or "")


class PipelineFileError(ValueError):
    """A ``.py`` the CLI could not load as exactly one Pipeline."""


def load_pipeline_file(path: str | Path) -> type[Pipeline]:
    """Import ``path`` and return the single :class:`Pipeline` subclass in it.

    Import-time discovery is fine here and only here: this runs in the CLI on a
    developer's machine (or CI), never on the control plane at run time — the
    governed record the run executes is still the installed pack (ADR-0083 D4).

    Raises :class:`PipelineFileError` with an actionable message for the three
    ways this realistically goes wrong: unreadable file, no Pipeline subclass,
    or several (ambiguous).
    """
    p = Path(path).expanduser()
    if not p.is_file():
        raise PipelineFileError(f"no such pipeline file: {p}")

    spec = importlib.util.spec_from_file_location(f"_cairn_pipeline_{p.stem}", p)
    if spec is None or spec.loader is None:
        raise PipelineFileError(f"could not load {p} as a Python module")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as e:  # noqa: BLE001 — surface the author's error verbatim
        raise PipelineFileError(f"{p} raised on import: {type(e).__name__}: {e}") from e

    found = [
        obj for obj in vars(module).values()
        if inspect.isclass(obj) and issubclass(obj, Pipeline) and obj is not Pipeline
    ]
    if not found:
        raise PipelineFileError(
            f"{p} defines no Pipeline subclass — expected e.g.\n"
            f"    from cairn.pipeline_file import Pipeline\n"
            f"    class MyPipeline(Pipeline):\n"
            f'        workflow = "pack/template"'
        )
    if len(found) > 1:
        names = ", ".join(c.__name__ for c in found)
        raise PipelineFileError(
            f"{p} defines {len(found)} Pipeline subclasses ({names}) — keep one per file"
        )

    cls = found[0]
    if not str(getattr(cls, "workflow", "") or "").strip():
        raise PipelineFileError(
            f"{cls.__name__} sets no `workflow` — it must name an installed "
            f'workflow, e.g. workflow = "training/finetune"'
        )
    return cls


def resolve_run_args(
    cls: type[Pipeline],
    *,
    env: Optional[str] = None,
    inputs: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Merge a pipeline's declarations with CLI overrides into run arguments.

    Precedence — CLI over file, matching the SDK design's run-time axis: an
    explicit ``--env``/``--set`` wins, otherwise the file's value applies.
    """
    merged = dict(cls.input_values())
    merged.update(inputs or {})
    return {
        "workflow": str(cls.workflow).strip(),
        "environment": (env or cls.env or "dev"),
        "series": str(getattr(cls, "series", "") or ""),
        "inputs": merged,
    }


__all__ = [
    "Pipeline",
    "PipelineFileError",
    "load_pipeline_file",
    "resolve_run_args",
]
