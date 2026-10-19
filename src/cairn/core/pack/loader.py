"""Pack discovery via Python entry-points.

Packs declare themselves under the ``cairn.packs`` entry-point group:

    [project.entry-points."cairn.packs"]
    incident = "cairn_pack_incident:pack"

in their OWN ``pyproject.toml`` (#2420 / ADR-0121: a domain pack is a ``cairn-pack-<name>``
data wheel; the engine's pyproject declares no pack).

``load_all_packs()`` walks that group, loads each ``Pack`` object, registers it,
and returns the list. It is idempotent: calling it again re-registers the same
packs (last-writer-wins in the registry), so it is safe to call lazily from
multiple consumption points. Called once explicitly at app startup; consumption
points (tool adapter, settings, doctor) also call ``ensure_loaded()`` so the
engine works even if startup wiring forgot to.

#1545 (ADR-0092 principle 4) — DEV MODE. An entry-point is an *install*-time
fact, so a freshly-scaffolded pack (``cairn init pack``) was invisible to the
engine until its author edited the root ``pyproject.toml`` and reinstalled the
workspace. ``CAIRN_PACK_PATH`` (``os.pathsep``-separated dirs, ``:`` on POSIX)
is the zero-install loop: after the entry-point pass the loader scans each
listed dir for pack directories and registers them through the SAME path
installed packs take. Installed packs WIN every name collision — a dev copy
must never silently shadow what is actually deployed — and a broken dev pack is
skipped with a warning, because a half-written pack in a scratch dir must not
brick the engine (``cairn validate`` is where it gets diagnosed).

§24.3 composition (P2.b) — after every pack is registered, the loader runs
:func:`validate_composition` and caches the report on the registry. The
default mode is **lenient**: conflicts are logged but the loader returns.
Callers that mutate state (``cairn run``, ``eval``, services boot)
should pass ``strict=True`` so a misconfigured deployment fails loud
instead of running with the wrong provider. doctor reads the cached
report and renders it as part of the ``[packs]`` category (P2.c).
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
from typing import Any
from importlib.metadata import PackageNotFoundError, entry_points, version as _dist_version
from pathlib import Path

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from cairn.core.pack.composition import (
    CompositionConflict,
    load_profile_from_env,
    validate_composition,
)
from cairn.core.pack.protocol import Pack
from cairn.core.pack.registry import (
    get_composition_report,
    get_packs,
    register_pack,
    set_composition_report,
)
from cairn.core.schema.yaml_loader import (
    SchemaYamlError,
    load_pack_schemas,
)

_log = logging.getLogger("cairn.core.pack.loader")

ENTRY_POINT_GROUP = "cairn.packs"

#: #1545 — dev-mode discovery. Colon-separated (``os.pathsep``) directories that
#: either ARE a pack dir or CONTAIN pack dirs. Read fresh on every load pass so a
#: test (or a shell that just exported it) takes effect on the next reload.
DEV_PACK_PATH_ENV = "CAIRN_PACK_PATH"

#: Dev packs are imported by file path, not by their own import name, so their
#: module name is prefixed: two scratch dirs may both hold a pack called
#: `demopack`, and neither may collide with an installed `demopack` in
#: ``sys.modules``.
DEV_MODULE_PREFIX = "cairn_devpack_"

#: The engine's own distribution name — what ``[compat] cairn_engine`` ranges in
#: a pack.toml are checked against (#1545 part 3). The root pyproject's ``[project]
#: name`` since #2499 (ADR-0121 D3); the import name stays ``cairn`` (#2512).
ENGINE_DISTRIBUTION = "cairndev"

#: #1538 — packs that FAILED to load this process, as ``{name, target, error}``.
#: Skip-on-error is right (a broken pack must not take down the engine), but a
#: log warning at boot is invisible by the time someone asks "why is my pack
#: missing?" — `cairn ls` and doctor render this list so the answer is one
#: command away instead of buried in startup logs.
_LOAD_FAILURES: list[dict[str, str]] = []


def pack_load_failures() -> list[dict[str, str]]:
    """Entry-points that did not produce a registered Pack this process."""
    return list(_LOAD_FAILURES)


#: packs already warned about, so the message is once per pack per process and
#: not once per flow — eight warnings for one pack trains people to ignore them.
_WARNED_LEGACY: set[str] = set()

#: same one-warning-per-pack discipline for the [compat] mismatch (#1545).
_WARNED_COMPAT: set[str] = set()

#: legacy asset dir → what supersedes it (ADR-0084 D2, #1163).
LEGACY_LAYOUT_DIRS = {"templates_dir": "flows/", "dataflows_dir": "flows/"}


def legacy_layout_dirs(pack: Any) -> list[str]:
    """Which superseded asset dirs this pack still declares (#1164).

    Returned rather than only logged so `cairn doctor` can list them without
    scraping logs — a deprecation nobody can see is not a deprecation.
    """
    return [a for a in LEGACY_LAYOUT_DIRS if getattr(pack, a, None)]


def _warn_legacy_layout(pack: Any) -> None:
    """One warning per pack when it ships flows under a superseded dir.

    #1133 said `templates/` stays "a working alias (warning, not error)" and no
    warning was ever emitted, so six of seven first-party packs sat on the old
    layout with nothing signalling it. The alias still WORKS — third-party packs
    depend on it — but it should no longer be silent.
    """
    if pack.name in _WARNED_LEGACY:
        return
    legacy = legacy_layout_dirs(pack)
    if not legacy:
        return
    _WARNED_LEGACY.add(pack.name)
    _log.warning(
        "pack %r keeps flows in %s — superseded by flows/ (ADR-0084 D2). The "
        "alias still loads; move the files to flows/ when convenient (#1163).",
        pack.name, ", ".join(a.replace("_dir", "/") for a in legacy),
    )


# ─── flow-asset lint: one ref one flow · no triggers on a flow ───────
#
# Two structural rules ADR-0106 puts on a pack's `flows/` dir, both refused at
# the install door and both reported (never raised) by a lenient boot:
#
#   D9  ONE REF, ONE FLOW  — `<pack>/<name>` addresses exactly one flow.
#   D3  A FLOW HAS NO TRIGGERS — `triggers:`/`on:` belong to a pipeline.
#
# They live together because they answer the same question — is this pack's
# flow directory legal — and an author who broke both should learn both in one
# round trip (#2128 item E, #2134 S2).

class PackAssetError(ValueError):
    """A pack's flow ASSETS break a structural rule (the manifest's
    :class:`~cairn.core.pack.manifest.ManifestError`, one directory out).

    Carries ``reasons`` so every violation in one pack is reported at once —
    an author fixing them one round trip at a time is the failure mode
    :class:`~cairn.core.pack.composition.CompositionConflict` already avoids.
    """

    def __init__(self, pack: str, reasons: list[str]) -> None:
        self.pack = pack
        self.reasons = list(reasons)
        joined = "\n  - ".join(self.reasons)
        super().__init__(f"pack {pack!r} ships invalid flow assets:\n  - {joined}")


#: packs already warned about, so the asset lint speaks once per pack per
#: process — the same discipline `_WARNED_LEGACY` keeps.
_LINTED_ASSETS: set[str] = set()

#: Packs that FAILED the asset lint, as ``{name, reason}``. The lenient load
#: keeps them, so this is how `cairn ls`, doctor and a reviewer see what the
#: strict door would refuse. Cleared per load pass, like `_LOAD_FAILURES`.
#: Empty on the shipped packs since #2134 S2 renamed the two colliding refs.
_ASSET_LINT_FAILURES: list[dict[str, str]] = []


def pack_asset_lint_failures() -> list[dict[str, str]]:
    """Packs that broke a flow-asset rule on the last load pass."""
    return list(_ASSET_LINT_FAILURES)


def flow_ref_collisions(pack: Any) -> list[str]:
    """ONE REF, ONE FLOW (#2128 item E): every ref this pack declares twice.

    ADR-0084 D2 says a flow comes in three kinds; ADR-0105 D1 says a flow
    started on its own IS its one-leg pipeline. Both assume ``<pack>/<name>``
    addresses exactly one flow. ``flows/clean.yaml`` (data) beside
    ``flows/clean.md`` (agentic) breaks that assumption everywhere at once: the
    start door can only REFUSE the ref
    (:class:`~cairn.core.dispatch.start.AmbiguousStartRefError`, and no ``kind``
    settles a flow-vs-flow tie), ``GET /flows`` lists two rows at one address,
    and the console's flow page has to pick one — which is how #2130's
    ``training/finetune`` page lost its Recipe, its Configuration and its Run.

    Returns one reason per colliding ref, NAMING BOTH FILES: "the ref is
    ambiguous" without the paths sends the author hunting through a tree.
    """
    from cairn.core.dataflow.defs import FLOW_KIND_DATA, FLOW_KIND_ML  # noqa: PLC0415 - cycle
    from cairn.core.pack.registry import flow_dirs_of  # noqa: PLC0415 - cycle

    # `flows/` + the `templates/` alias hold agentic flows; `flows/` + the
    # legacy `dataflows/` alias hold data/ml ones. ONE address space.
    agentic: dict[str, Path] = {}
    for d in flow_dirs_of(pack):
        for f in sorted(Path(d).glob("*.md")):
            agentic.setdefault(f.stem, f)
    stage: dict[str, Path] = {}
    for attr in ("flows_dir", "dataflows_dir"):
        d = getattr(pack, attr, None)
        if d and Path(d).is_dir():
            for f in sorted(Path(d).glob("*.yaml")):
                stage.setdefault(f.stem, f)
    kinds = f"{FLOW_KIND_DATA}/{FLOW_KIND_ML}"
    return [
        f"{pack.name}/{name}: two flows at one ref — {stage[name]} (a {kinds} "
        f"flow) and {agentic[name]} (an agentic flow). A ref addresses ONE "
        f"flow (ADR-0106 D9 / ADR-0084 D2): rename one of them and fix its "
        f"readers."
        for name in sorted(set(agentic) & set(stage))
    ]


def flow_front_matter(path: Path) -> dict[str, Any]:
    """The declaration block of a flow file, as a plain mapping.

    An agentic flow puts it in YAML front matter; a data/ml flow's whole file
    IS the mapping. ``{}`` when the file is missing, unparseable, or does not
    open with a front-matter fence — every caller here is a lint or a lookup
    whose right answer for a broken file is "nothing to see", because the def
    loaders already report the parse error with the line the author needs.

    Split by hand rather than through ``frontmatter.load``: YAML 1.1 reads an
    unquoted ``on:`` as the boolean ``True``, and that library passes the
    metadata as ``**kwargs``, so it raises ``TypeError`` on exactly the
    GitHub-Actions-shaped block :func:`flow_triggers_declared` must catch.
    """
    import yaml  # noqa: PLC0415 - keep the module's import path light

    try:
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".md":
            if not text.startswith("---"):
                return {}
            parts = text.split("\n---", 2)
            if len(parts) < 2:
                return {}
            text = parts[0][3:]
        meta = yaml.safe_load(text)
    except Exception:  # noqa: BLE001 — see the docstring
        return {}
    return meta if isinstance(meta, dict) else {}


def flow_files_of(pack: Any) -> list[Path]:
    """Every flow file this pack ships, agentic and data/ml, deduped + sorted.

    ``flows/`` plus the ``templates/`` alias hold agentic flows; ``flows/``
    plus the legacy ``dataflows/`` alias hold data/ml ones. ONE address space
    (ADR-0084 D2), which is what makes D9 checkable at all.
    """
    from cairn.core.pack.registry import flow_dirs_of  # noqa: PLC0415 - cycle

    files: list[Path] = []
    for d in flow_dirs_of(pack):
        files.extend(Path(d).glob("*.md"))
    for attr in ("flows_dir", "dataflows_dir"):
        d = getattr(pack, attr, None)
        if d and Path(d).is_dir():
            files.extend(Path(d).glob("*.yaml"))
    return sorted(set(files))


def _declares_triggers(path: Path) -> str:
    """Which trigger key ``path`` declares at the top level — ``""`` for none.

    Reads the two places a flow could put one: an agentic flow's YAML front
    matter, and a data/ml flow's own mapping. Both spellings are checked,
    because ``on:`` is the legacy spelling of ``triggers:`` (#1159, #1987) and
    a rule that only refused the new one would send authors to the old one.

    A file this cannot parse yields ``""``: the def loaders report a broken
    flow with the error the author needs, and a lint that turned a YAML typo
    into "you declared a trigger" would be worse than silent.
    """
    meta = flow_front_matter(path)
    # `on:` unquoted IS `True` after a YAML 1.1 load, which is exactly how a
    # GitHub-Actions-shaped `on:` block arrives — check every spelling.
    for key in ("triggers", "on", True):
        if meta.get(key) not in (None, [], {}):
            return "triggers" if key == "triggers" else "on"
    return ""


def flow_triggers_declared(pack: Any) -> list[str]:
    """A FLOW MAY NOT DECLARE TRIGGERS (ADR-0106 D3): every flow that does.

    ADR-0101 D1 let a flow carry its own ``triggers:`` block. ADR-0106
    supersedes that: a trigger is *intent* — when should this run, on whose
    budget, behind which gate — and intent is the pipeline's to own (D4). A
    flow states needs. Leaving the block on the flow means a start can be
    scheduled for something that, under D1, cannot be started at all: the flow
    is a component until a pipeline wraps it, so an armed flow trigger is a
    schedule pointing at a door that refuses.

    Returns one reason per offending file, naming the file and the pipeline
    that should carry the block instead. No shipped flow declares one, so this
    rule costs the first-party packs nothing — it stops the next one.
    """
    out: list[str] = []
    for f in flow_files_of(pack):
        if key := _declares_triggers(f):
            out.append(
                f"{pack.name}/{f.stem}: flow {f} declares `{key}:` — a flow "
                f"cannot be triggered (ADR-0106 D3). Triggers belong to a "
                f"pipeline: move the block to a pipeline that runs this flow "
                f"(pipelines/{f.stem}.yaml, `inputs: {{$inherit: {f.stem}}}`, "
                f"one leg) and arm `pipeline:{pack.name}/{f.stem}`."
            )
    return out


def flow_asset_violations(pack: Any) -> list[str]:
    """Every flow-asset rule this pack breaks, in one list (D9 then D3)."""
    return flow_ref_collisions(pack) + flow_triggers_declared(pack)


def assert_flow_assets(pack: Any) -> None:
    """Raise :class:`PackAssetError` when a pack's flows break D9 or D3."""
    if reasons := flow_asset_violations(pack):
        raise PackAssetError(pack.name, reasons)


def _lint_pack_assets(pack: Any, *, strict: bool) -> None:
    """The asset lint, at registration.

    ``strict`` is the install / ``cairn validate`` door and RAISES. The lenient
    boot warns and records instead: a naming collision or a stray ``triggers:``
    block in ONE pack must not brick a running engine that has nine others, and
    the recorded row is what tells an operator (`cairn ls`, doctor) exactly what
    the strict door would refuse. Since #2134 S2 no shipped pack breaks either
    rule, so the lenient path exists for dev packs and third-party ones.
    """
    reasons = flow_asset_violations(pack)
    if not reasons:
        return
    _ASSET_LINT_FAILURES.append({"name": pack.name, "reason": "; ".join(reasons)})
    if strict:
        raise PackAssetError(pack.name, reasons)
    if pack.name in _LINTED_ASSETS:
        return
    _LINTED_ASSETS.add(pack.name)
    for r in reasons:
        _log.warning("pack asset lint (ADR-0106 D3/D9): %s", r)


# ─── [compat] cairn_engine enforcement (#1545 part 3) ────────────────


def installed_engine_version() -> str | None:
    """The running engine's version, or ``None`` when cairn isn't installed as a
    distribution (a source checkout on ``PYTHONPATH``, which is a real dev setup).
    """
    try:
        return _dist_version(ENGINE_DISTRIBUTION)
    except PackageNotFoundError:
        return None


def engine_compat_status(pack: Any) -> tuple[str, str] | None:
    """Check one pack's ``[compat] cairn_engine`` range against this engine.

    Returns ``(status, detail)`` where status is ``"ok"``/``"warn"``, or ``None``
    when the pack declares no range (the overwhelmingly common case — nothing to
    say, no doctor row).

    ONE function, two callers (the loader's warning + doctor's row), so the two
    can never disagree about what "compatible" means.

    A mismatch is a **warning, never a hard reject**: the manifest key has been
    parsed-but-unenforced since it was introduced, so packs in the wild carry
    ranges nobody has ever validated, and every engine version bump would brick
    those dev setups on the spot. The warning names the pack, the declared range
    and the actual version, which is what an author needs to fix it.
    """
    m = getattr(pack, "manifest", None)
    declared = getattr(m, "cairn_engine", None) if m is not None else None
    if not declared:
        return None

    name = getattr(pack, "name", "?")
    actual = installed_engine_version()
    if actual is None:
        return ("warn", (
            f"pack {name!r} declares [compat] cairn_engine = {declared!r} but the "
            f"{ENGINE_DISTRIBUTION!r} distribution is not installed (source checkout?) "
            "— cannot verify"))
    try:
        spec = SpecifierSet(declared)
    except InvalidSpecifier:
        return ("warn", (
            f"pack {name!r} declares [compat] cairn_engine = {declared!r}, which is not a "
            f"valid PEP 440 specifier (e.g. \">=0.1.0,<0.2\") — cannot verify against "
            f"engine {actual}"))
    try:
        parsed = Version(actual)
    except InvalidVersion:  # pragma: no cover — a non-PEP440 engine version
        return ("warn", (
            f"pack {name!r} declares [compat] cairn_engine = {declared!r} but the installed "
            f"engine version {actual!r} is not PEP 440 — cannot verify"))

    # prereleases=True: a dev/rc engine build must not be silently excluded from
    # a plain `>=x` range — that would warn on every pre-release install.
    if spec.contains(parsed, prereleases=True):
        return ("ok", f"pack {name!r} requires engine {declared} — installed {actual}")
    return ("warn", (
        f"pack {name!r} declares [compat] cairn_engine = {declared!r} but the installed "
        f"engine is {actual} — loading anyway; the pack may not behave as its author "
        "expects (#1545)"))


def _warn_engine_incompat(pack: Any) -> None:
    """Log the compat mismatch once per pack per process (same discipline as the
    legacy-layout warning: eight copies of one warning trains people to ignore it)."""
    status = engine_compat_status(pack)
    if status is None or status[0] == "ok":
        return
    key = str(getattr(pack, "name", "?"))
    if key in _WARNED_COMPAT:
        return
    _WARNED_COMPAT.add(key)
    _log.warning("%s", status[1])


# ─── dev-mode directory discovery (#1545 part 1) ─────────────────────


def dev_pack_dirs() -> list[Path]:
    """Directories listed in ``CAIRN_PACK_PATH``, in order, de-duplicated."""
    raw = os.environ.get(DEV_PACK_PATH_ENV, "")
    out: list[Path] = []
    for chunk in raw.split(os.pathsep):
        chunk = chunk.strip()
        if not chunk:
            continue
        p = Path(chunk).expanduser()
        if p not in out:
            out.append(p)
    return out


def is_pack_dir(path: Path) -> bool:
    """A directory is a pack when it holds both halves of the contract: the
    declarative ``pack.toml`` and the ``__init__.py`` shim the entry-point would
    have targeted."""
    return (path / "pack.toml").is_file() and (path / "__init__.py").is_file()


def _import_dev_pack(pack_dir: Path) -> Any:
    """Import a pack directory by path under a collision-proof module name."""
    mod_name = f"{DEV_MODULE_PREFIX}{pack_dir.name}"
    init = pack_dir / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        mod_name, init, submodule_search_locations=[str(pack_dir)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot build an import spec for {init}")
    module = importlib.util.module_from_spec(spec)
    # in sys.modules BEFORE exec: a shim that does `from .x import y` (or any
    # dataclass/pickle round-trip) needs its own module findable while executing.
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(mod_name, None)
        raise
    return module


def _candidate_dev_pack_dirs(root: Path) -> list[Path]:
    """Pack dirs under one ``CAIRN_PACK_PATH`` entry.

    The documented shape is a dir CONTAINING packs (``CAIRN_PACK_PATH=~/packs``),
    but pointing straight at one pack (``CAIRN_PACK_PATH=~/packs/demopack``) is
    the obvious thing to try, so both work rather than one silently finding
    nothing.
    """
    if is_pack_dir(root):
        return [root]
    return [c for c in sorted(root.iterdir()) if c.is_dir() and is_pack_dir(c)]


def _load_dev_packs(installed: set[str], *, strict: bool = False) -> None:
    """Scan ``CAIRN_PACK_PATH`` and register every pack dir found there.

    ``installed`` is the set of names the entry-point pass just registered; those
    win, and a dev copy of the same name is skipped with a warning naming both
    locations. Every failure is per-directory and non-fatal.
    """
    seen_dev: dict[str, Path] = {}
    for root in dev_pack_dirs():
        if not root.is_dir():
            _log.warning(
                "%s entry %s is not a directory — skipping", DEV_PACK_PATH_ENV, root)
            _LOAD_FAILURES.append({
                "name": root.name, "target": str(root),
                "error": f"{DEV_PACK_PATH_ENV} entry is not a directory"})
            continue
        try:
            candidates = _candidate_dev_pack_dirs(root)
        except OSError as e:
            _log.warning("cannot scan %s entry %s: %s", DEV_PACK_PATH_ENV, root, e)
            _LOAD_FAILURES.append({
                "name": root.name, "target": str(root),
                "error": f"{type(e).__name__}: {e}"})
            continue
        if not candidates:
            _log.info(
                "%s entry %s holds no pack directories (a pack dir has pack.toml + "
                "__init__.py)", DEV_PACK_PATH_ENV, root)
        for pack_dir in candidates:
            try:
                module = _import_dev_pack(pack_dir)
            except Exception as e:  # a broken dev pack must not brick the engine
                _log.warning("failed to load dev pack at %s: %s: %s",
                             pack_dir, type(e).__name__, e)
                _LOAD_FAILURES.append({
                    "name": pack_dir.name, "target": str(pack_dir),
                    "error": f"{type(e).__name__}: {e}"})
                continue
            obj = getattr(module, "pack", None)
            if not isinstance(obj, Pack):
                what = "no `pack` attribute" if obj is None else \
                    f"`pack` is {type(obj).__name__}, not a Pack"
                _log.warning("dev pack at %s has %s — skipping", pack_dir, what)
                _LOAD_FAILURES.append({
                    "name": pack_dir.name, "target": str(pack_dir), "error": what})
                continue
            if obj.name in installed:
                _log.warning(
                    "dev pack %r at %s is SHADOWED by the installed pack of the same "
                    "name (entry-point %r) — the installed one wins. Uninstall it, or "
                    "rename the dev pack, to work on this copy.",
                    obj.name, pack_dir, obj.name)
                continue
            if obj.name in seen_dev:
                _log.warning(
                    "dev pack %r at %s duplicates the earlier one at %s — first entry "
                    "in %s wins (PATH semantics).",
                    obj.name, pack_dir, seen_dev[obj.name], DEV_PACK_PATH_ENV)
                continue
            seen_dev[obj.name] = pack_dir
            _register_pack(obj, strict=strict)
            _log.info("registered dev pack %r from %s (%s)",
                      obj.name, pack_dir, DEV_PACK_PATH_ENV)


def _register_pack(obj: Pack, *, strict: bool = False) -> None:
    """The one registration path — entry-point packs and dev packs both take it.

    Registration is not just ``register_pack``: it is also the legacy-layout
    deprecation, the ``[compat]`` engine check, the ADR-0106 D3/D9 flow-asset
    lint, and the pack's JSON Schema URIs. A second copy of this for dev packs would
    drift (the #1402 lesson: a chokepoint must be a FUNCTION others call, not a
    block others copy).
    """
    register_pack(obj)
    _warn_legacy_layout(obj)
    _warn_engine_incompat(obj)
    _lint_pack_assets(obj, strict=strict)

    # §24.20.10 — every JSON Schema YAML the pack ships registers under
    # its `schema://<pack>/<Name>` URI so templates can reference them
    # from frontmatter. Failures are logged but non-fatal so a broken
    # schema doesn't take down the whole pack load.
    if obj.schemas_dir:
        schemas_path = Path(obj.schemas_dir)
        pack_root = schemas_path.parent
        try:
            uris = load_pack_schemas(pack_root, pack_name=obj.name)
            if uris:
                _log.debug(
                    "pack %r registered %d schema(s): %s",
                    obj.name, len(uris), [u.uri for u in uris],
                )
        except SchemaYamlError as e:
            _log.warning(
                "pack %r schema load failed: %s", obj.name, e,
            )


def load_all_packs(*, strict: bool = False) -> list[Pack]:
    """Discover, load, and register every installed pack. Idempotent.

    Two passes: the ``cairn.packs`` entry-point group (installed packs), then
    ``CAIRN_PACK_PATH`` (dev packs, #1545 — see the module docstring). Installed
    packs win any name collision.

    Composition (§24.3 / P2.b) runs after registration. ``strict=True``
    raises :class:`CompositionConflict` on any hard violation (duplicate
    providers, unresolved required capabilities, profile-pick mismatches);
    ``strict=False`` (default) only logs the conflict so dev workflows
    and doctor can still inspect a partially-broken state.

    Optional capability gaps are *always* warnings — they never fail the
    load regardless of ``strict``.
    """
    _LOAD_FAILURES.clear()  # a fresh pass recomputes; no duplicate rows on reload
    _ASSET_LINT_FAILURES.clear()  # as above: THIS pass's verdict, not a running tally
    installed: set[str] = set()
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        try:
            obj = ep.load()
        except Exception as e:  # a broken pack shouldn't take down the engine
            _log.warning("failed to load pack %r (%s): %s", ep.name, ep.value, e)
            _LOAD_FAILURES.append(
                {"name": ep.name, "target": ep.value, "error": f"{type(e).__name__}: {e}"})
            continue
        if not isinstance(obj, Pack):
            _log.warning(
                "entry-point %r resolved to %r, not a Pack — skipping",
                ep.name, type(obj).__name__,
            )
            _LOAD_FAILURES.append(
                {"name": ep.name, "target": ep.value,
                 "error": f"entry-point resolved to {type(obj).__name__}, not a Pack"})
            continue
        installed.add(obj.name)
        _register_pack(obj, strict=strict)

    # ── Dev-mode pass (#1545) — CAIRN_PACK_PATH, after the installed packs so
    # they win every name collision. ──
    _load_dev_packs(installed, strict=strict)

    # ── Composition pass ──
    profile = load_profile_from_env()
    report = validate_composition(get_packs(), profile)
    set_composition_report(report)

    for w in report.warnings:
        _log.info("composition warning: %s", w)
    if report.conflicts:
        for c in report.conflicts:
            _log.warning("composition conflict: %s", c)
        if strict:
            raise CompositionConflict(report.conflicts)

    return get_packs()


def ensure_loaded(*, strict: bool = False) -> list[Pack]:
    """Load packs if none are registered yet, then serve from the registry.

    Keys off registry emptiness rather than a one-shot flag so that a test which
    calls ``registry.clear()`` self-heals on the next consumption call.

    Re-runs composition validation when a report hasn't been cached (e.g.
    the caller cleared the registry, or the engine boots fresh). Doesn't
    re-validate on warm calls — composition is a property of *what's
    installed*, which doesn't change once entry-points have been walked.
    """
    if not get_packs():
        return load_all_packs(strict=strict)
    if get_composition_report() is None:
        # Registry was preloaded by tests without running the loader.
        # Run the composition pass anyway so callers like doctor have
        # something to render.
        profile = load_profile_from_env()
        report = validate_composition(get_packs(), profile)
        set_composition_report(report)
        if strict and report.conflicts:
            raise CompositionConflict(report.conflicts)
    return get_packs()
