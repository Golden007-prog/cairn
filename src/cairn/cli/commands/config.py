"""``cairn config`` — operator/workflow configuration tooling (ADR-0068 / ADR-0070).

``cairn config workflow <template-ref>`` prints the COMPOSED config surface for a
workflow: every tunable knob across its reachable operators, grouped by operator,
each labelled **governance** (env-owned, read-only for a workflow/run) or
**operational** (tunable per workflow/run). The surface a workflow author edits
instead of hunting operator-by-operator in a profile (ADR-0070, #748).
"""
from __future__ import annotations

import json as _json

import typer

app = typer.Typer(help="Operator / workflow configuration.", no_args_is_help=True)


@app.command("workflow")
def workflow_cmd(
    template_ref: str = typer.Argument(..., help="Template ref, e.g. training/finetune-remote."),
    as_json: bool = typer.Option(False, "--json", help="Emit the composed surface as JSON."),
) -> None:
    """Show the composed config surface for a workflow (its reachable operators'
    knobs, governance vs operational)."""
    from cairn.core.engine.workflow_config import compose_workflow_config

    composed = compose_workflow_config(template_ref)
    if as_json:
        typer.echo(_json.dumps(composed, indent=2, default=str))
        return

    if not composed:
        typer.echo(f"No configurable operators reachable from {template_ref!r}.")
        return

    typer.echo(f"Config surface for workflow: {template_ref}\n")
    for op_name in sorted(composed):
        part = composed[op_name]
        gov, ops = part.get("governance", []), part.get("operational", [])
        typer.echo(f"  {op_name}")
        for label, knobs in (("governance (env-owned)", gov), ("operational (tunable)", ops)):
            if not knobs:
                continue
            typer.echo(f"    {label}:")
            for k in knobs:
                default = k.get("default")
                dtxt = "" if default is None else f"  [default: {default}]"
                title = k.get("title") or k["path"]
                typer.echo(f"      - {k['path']}  — {title}{dtxt}")
        typer.echo("")
