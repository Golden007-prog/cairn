"""cairn CLI surface.

Top-level dispatcher (:mod:`cairn.cli.main`) registers one Typer command
per file in :mod:`cairn.cli.commands`. The CLI is a thin shell: every
command wires args → calls into :mod:`cairn.core` → renders a result.
Business logic stays out of this layer.

Entry point is wired in ``pyproject.toml`` as the ``cairn`` script —
``pip install -e .`` puts ``cairn`` on PATH.
"""
