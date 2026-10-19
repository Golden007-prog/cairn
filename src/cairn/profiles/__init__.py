"""cairn.profiles — substrate-shipped reference profiles.

These are the profile TOML files the substrate ships so deployments don't
have to wire every backend choice from scratch. ``local-dev.toml`` is the
zero-config developer profile; production deployments are expected to
ship their own profile that ``extends`` one of these or replaces them
outright.

The directory is importable so callers can resolve the absolute path of
a shipped profile without hardcoding it:

    from pathlib import Path
    import cairn.profiles as profiles_pkg

    local_dev_path = Path(profiles_pkg.__file__).parent / "local-dev.toml"
"""
from cairn.core.settings import getenv


def local_dev_path():
    """The packaged ``local-dev.toml`` — the zero-config developer profile."""
    from pathlib import Path

    return Path(__file__).parent / "local-dev.toml"


def default_dev_profile() -> None:
    """Dev-loop CLI commands compile under ``local-dev.toml`` when no profile is set (#2241 D).

    A v1 ``capability:`` operation resolves only through ``[profile.bindings]`` (ADR-0111
    D10, no fallback provider), so an unprofiled local compile of a migrated template would
    refuse. Deployed processes never call this: they must name their profile (#2296).
    """
    import os

    if not getenv("CAIRN_PROFILE", "").strip():
        os.environ["CAIRN_PROFILE"] = str(local_dev_path())
