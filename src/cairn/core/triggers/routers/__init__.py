"""APIRouter modules for the trigger server (#1191, ADR-0085 D4).

Each module exposes a ``build_<surface>_router(...)`` factory taking the
shared dependencies (dispatcher, runner, …) it needs. ``server.build_app``
includes each router at exactly the position the inline block used to occupy,
so route order — and therefore the committed OpenAPI contract — is unchanged.
Route paths, auth behavior, and handler logic are byte-identical to the
pre-split server.py; NO behavior changes ride along with the split.
"""
