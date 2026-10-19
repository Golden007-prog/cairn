"""CLI command modules.

One file per command. Each module exposes a Typer-compatible function
(decorated/registered in :mod:`cairn.cli.main`) and any helpers it needs.
Command modules MUST NOT contain business logic — they wire args → call into
:mod:`cairn.core` → render a result. The single exception is the doctor
module, whose probe logic lived in core/ before the CLI existed and was moved
here intact as part of the CLI-layer extraction.
"""
