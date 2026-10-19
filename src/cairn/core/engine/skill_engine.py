"""
Skill engine: loads skills, matches triggers, resolves templates.

A skill is a markdown file with YAML frontmatter declaring:
  - When it activates (triggers: patterns, alerts, metrics, schedule, manual)
  - Which template to use for investigation
  - Additional capabilities to add to the template's tool pool
  - Action candidates it may suggest

When a TriggerEvent fires, the engine finds matching skills and produces
LoadedSkill objects that bundle the skill spec, the (skill-augmented) compiled
template, and the action candidates ready for evaluation.
"""

from __future__ import annotations

import importlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import frontmatter

from cairn.core.engine.compiled import CompiledTemplate
from cairn.core.engine.compiler import compile_template
from cairn.core.registry import CapabilityRegistry, default_registry
from cairn.core.engine.schema import (
    ActionCandidate,
    SkillCustomLogic,
    SkillSpec,
    TriggerEvent,
)

logger = logging.getLogger("cairn.skill_engine")


# ─── Custom logic loader ─────────────────────────────────────────────


@dataclass
class LoadedCustomLogic:
    """Resolved callables from a skill's `custom_logic` spec. Missing
    entries are None; callers must check before invoking.
    """
    evaluate_trigger: Callable[[TriggerEvent], bool] | None = None
    validate_action: Callable[[Any, dict], bool] | None = None
    on_resolve: Callable[[dict], None] | None = None
    on_failure: Callable[[dict], None] | None = None


def load_custom_logic(spec: SkillCustomLogic | None) -> LoadedCustomLogic | None:
    """Import the configured Python module and resolve named functions.

    Returns None if no custom_logic is configured. Logs a warning (not an
    error) if the module import fails — the skill can still run declaratively.
    """
    if spec is None:
        return None
    try:
        module = importlib.import_module(spec.module)
    except ImportError as e:
        logger.warning(
            "Failed to import custom_logic module '%s': %s — skill will run declaratively",
            spec.module, e,
        )
        return None

    def _resolve(attr: str | None):
        if attr is None:
            return None
        fn = getattr(module, attr, None)
        if fn is None:
            logger.warning(
                "custom_logic.%s='%s' is not defined in module '%s'",
                attr, attr, spec.module,
            )
        return fn

    return LoadedCustomLogic(
        evaluate_trigger=_resolve(spec.evaluate_trigger),
        validate_action=_resolve(spec.validate_action),
        on_resolve=_resolve(spec.on_resolve),
        on_failure=_resolve(spec.on_failure),
    )


# ─── Loaded skill (compiled, ready to run) ───────────────────────────


@dataclass
class LoadedSkill:
    """A skill matched against a trigger, with its template compiled and ready."""
    spec: SkillSpec
    compiled_template: CompiledTemplate
    trigger: TriggerEvent
    action_candidates: list[ActionCandidate]
    custom_logic: LoadedCustomLogic | None = None


# ─── Skill loading from disk ─────────────────────────────────────────


def load_skill(path: Path | str) -> SkillSpec:
    """Parse a skill .md file and return its validated SkillSpec.

    The markdown body becomes domain context — appended to the referenced
    template's prose when the skill activates.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Skill file not found: {path}")
    post = frontmatter.load(path)
    spec = SkillSpec.model_validate(post.metadata)
    # Stash the prose as a private attribute so the engine can pick it up later
    spec.__dict__["_prose"] = post.content
    spec.__dict__["_source_path"] = str(path)
    return spec


def load_skills_directory(directory: Path | str) -> list[SkillSpec]:
    """Load all .md skill files in a directory recursively."""
    directory = Path(directory)
    if not directory.exists():
        return []
    skills: list[SkillSpec] = []
    for path in sorted(directory.rglob("*.md")):
        try:
            skills.append(load_skill(path))
        except Exception as e:
            # Don't crash the whole registry for one bad file
            print(f"[skill_engine] Failed to load {path}: {e}")
    return skills


# ─── Template resolution ─────────────────────────────────────────────


# Templates ship inside their pack and are discovered via `_pack_template_dirs()`
# (appended in `resolve_template_path`). These defaults are only dev-convenience
# override locations searched FIRST — e.g. a local `templates/` dir for ad-hoc
# experiments. The incident pack's templates are NOT duplicated here; they live
# in `cairn/packs/incident/flows/` and resolve through pack discovery.
# When the registry lands, this becomes hash/tag resolution.
DEFAULT_TEMPLATE_SEARCH_PATHS = [
    Path("templates"),
]


def _pack_template_dirs() -> list[Path]:
    """Directories holding agentic flows, contributed by installed packs.

    #1133 — ONE concept: FLOW. `flows/` is searched FIRST (the target home for
    all three kinds), `templates/` second as a working alias, so a pack can
    migrate one file at a time without a flag day.
    """
    try:
        from cairn.core.pack import all_flow_dirs, all_template_dirs, ensure_loaded

        ensure_loaded()
        return [Path(d) for d in (*all_flow_dirs(), *all_template_dirs())]
    except Exception:
        return []


def _pack_skill_dirs() -> list[Path]:
    """Skill directories contributed by installed packs (best-effort)."""
    try:
        from cairn.core.pack import all_skill_dirs, ensure_loaded

        ensure_loaded()
        return [Path(d) for d in all_skill_dirs()]
    except Exception:
        return []


def resolve_template_path(
    name: str,
    search_paths: list[Path] | None = None,
    *,
    from_pack: str | None = None,
) -> Path:
    """Find a template file by name. Returns first match.

    Resolution order (§24.3.9 — P2.e):

      1. **Namespaced ref** ``<pack>/<localname>`` — resolves against the
         named pack's ``templates_dir``. The lookup respects encapsulation:
         if the owning pack declared ``[provides]`` and ``localname`` is
         not in ``provides_templates``, the lookup fails (private template
         invisible across pack boundaries). Permissive packs (no
         ``[provides]``) export everything — today's behavior.

      2. **Caller-supplied search paths** — direct dev-convenience override.

      3. **Calling pack's own dir** — when ``from_pack`` is given, that
         pack's templates resolve first (private templates stay internal).

      4. **All pack template dirs** — flat fallback that preserves
         today's behavior for permissive callers.

    ``from_pack=None`` keeps today's everything-visible default for
    callers that don't carry a calling-pack identity (engine, CLI,
    compiler before it adopts P2.d threading).
    """
    # ── (1) Namespaced cross-pack ref ────────────────────────────
    if "/" in name:
        pack_name, _, local_name = name.partition("/")
        return _resolve_namespaced_template(pack_name, local_name, from_pack)

    paths: list[Path] = list(search_paths or DEFAULT_TEMPLATE_SEARCH_PATHS)
    # ── (3) Calling-pack-first ─────────────────────────────────
    if from_pack is not None:
        caller_dir = _template_dir_for_pack(from_pack)
        if caller_dir is not None:
            paths.append(caller_dir)
    # ── (4) Flat fallback ──────────────────────────────────────
    paths.extend(_pack_template_dirs())

    for base in paths:
        candidate = Path(base) / f"{name}.md"
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        f"Template '{name}' not found in any search path: {paths}"
    )


def _template_dir_for_pack(pack_name: str) -> Path | None:
    """The ``templates_dir`` of a registered pack, or ``None`` if the
    pack isn't installed or doesn't ship templates."""
    try:
        from cairn.core.pack import ensure_loaded, get_pack
        ensure_loaded()
        pack = get_pack(pack_name)
    except Exception:
        return None
    # #1133 — `flows/` first, `templates/` as the transition alias.
    for attr in ("flows_dir", "templates_dir"):
        if (d := getattr(pack, attr, None)):
            return Path(d)
    return None


def _resolve_namespaced_template(
    pack_name: str, local_name: str, from_pack: str | None,
) -> Path:
    """Resolve ``<pack>/<localname>`` per §24.3.9.

    Encapsulation enforcement: an encapsulated pack's private template
    is invisible to outside callers. The same lookup made by the owning
    pack itself succeeds (within-pack rule), so a pack can use the
    namespaced form on its own templates without losing access.
    """
    from cairn.core.pack import (
        ensure_loaded,
        get_pack,
        is_template_visible,
    )
    ensure_loaded()
    pack = get_pack(pack_name)
    if pack is None:
        raise FileNotFoundError(
            f"Template '{pack_name}/{local_name}' references unknown "
            f"pack '{pack_name}' — install it or fix the namespace prefix"
        )
    if not is_template_visible(local_name, owning_pack=pack,
                               from_pack=from_pack):
        raise FileNotFoundError(
            f"Template '{local_name}' is private to pack '{pack_name}' — "
            f"not in its [provides.templates] export list"
        )
    # #1163 — a pack keeps agentic flows in `flows/` (or the `templates/` alias
    # during the transition). Checking only `templates_dir` made every migrated
    # pack unresolvable through the pack-scoped path.
    from cairn.core.pack.registry import flow_dirs_of  # noqa: PLC0415 - cycle

    dirs = flow_dirs_of(pack)
    if not dirs:
        raise FileNotFoundError(
            f"Pack '{pack_name}' has no flows directory"
        )
    for d in dirs:
        candidate = Path(d) / f"{local_name}.md"
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        f"Flow file not found: {local_name}.md in {dirs} "
        f"(pack '{pack_name}' declares '{local_name}' but the .md is missing)"
    )


# ─── Trigger matching ────────────────────────────────────────────────


def match_skill_to_event(spec: SkillSpec, event: TriggerEvent) -> bool:
    """Return True if the given trigger event matches one of the skill's triggers."""
    triggers = spec.triggers

    if event.source == "manual" and triggers.manual:
        # A *named* manual fire (`cairn remote submit <skill>` / a manual
        # trigger carrying the target name in matched_pattern) must scope to
        # that one skill — otherwise a single submit fans out to EVERY skill
        # with `manual: true` (e.g. submitting incident-orchestrated also fired
        # dataset-readiness's `check`). A bare manual fire (no name) keeps the
        # original fan-out to all manual skills.
        target = (event.matched_pattern or "").strip()
        if not target:
            return True
        return target == spec.name or target in (triggers.patterns or [])

    # Cascade events (source=cascade) carry the target skill name in
    # ``matched_pattern``; treat as a pattern match against that exact name.
    if event.source == "cascade" and triggers.patterns:
        return (event.matched_pattern or "") in triggers.patterns

    if event.source == "pattern" and triggers.patterns:
        if event.matched_pattern:
            return event.matched_pattern in triggers.patterns
        msg = str(event.payload.get("message", ""))
        for pattern in triggers.patterns:
            if pattern in msg or re.search(re.escape(pattern), msg, re.IGNORECASE):
                return True
        return False

    if event.source == "alert" and triggers.alerts:
        evt_source = event.payload.get("source", "")
        evt_service = event.service or event.payload.get("service", "")
        for alert in triggers.alerts:
            if alert.source == evt_source:
                if not alert.service or alert.service == evt_service:
                    return True
        return False

    if event.source == "metric" and triggers.metrics:
        # MVP: just check that there's any metric trigger; full expr eval is later
        return len(triggers.metrics) > 0

    if event.source == "schedule" and triggers.schedule:
        return True

    # Cross-pack event-bus activation (ADR-0010 P3.c): the bus topic is carried
    # in matched_pattern; match it against the skill's subscribes_to patterns
    # using the same wildcard semantics as the bus.
    if event.source == "event" and triggers.subscribes_to:
        from cairn.core.bus import _topic_matches
        topic = event.matched_pattern or ""
        return any(_topic_matches(p, topic) for p in triggers.subscribes_to)

    return False


# ─── Engine ──────────────────────────────────────────────────────────


class SkillEngine:
    """Loads skills, matches triggers, prepares LoadedSkill for the runner."""

    def __init__(
        self,
        skills_directory: Path | str | None = None,
        template_search_paths: list[Path] | None = None,
        registry: CapabilityRegistry | None = None,
    ):
        # An explicit directory overrides pack discovery (dev/test); otherwise
        # skills come from every installed pack's contributed skills dir.
        if skills_directory is not None:
            self.skills: list[SkillSpec] = load_skills_directory(skills_directory)
        else:
            self.skills = []
            for d in _pack_skill_dirs():
                self.skills.extend(load_skills_directory(d))
        self.template_search_paths = template_search_paths or DEFAULT_TEMPLATE_SEARCH_PATHS
        self.registry = registry or default_registry

    def list_skills(self) -> list[str]:
        return [s.name for s in self.skills]

    def list_skills_detail(self) -> list[dict[str, Any]]:
        """Skills with their resolved Pack→Module→Template grouping + tags.

        ``module`` is the skill's own label when set, else inherited from the
        referenced template's frontmatter (read cheaply, no compile). ``tags``
        come from the template's frontmatter. Pure discovery metadata — failure
        to resolve a template is non-fatal and yields ``None`` / ``[]``.
        """
        out: list[dict[str, Any]] = []
        for s in self.skills:
            meta = self._template_meta(s.template.name)
            out.append(
                {
                    "name": s.name,
                    "module": s.module or meta.get("module"),
                    "template": s.template.name,
                    "tags": meta.get("tags", []),
                }
            )
        return out

    def _template_meta(self, template_name: str) -> dict[str, Any]:
        """Read discovery metadata (``module``, ``tags``) from a template's
        frontmatter without compiling it. Non-fatal on any resolution error."""
        try:
            path = resolve_template_path(template_name, self.template_search_paths)
            md = frontmatter.load(path).metadata
            return {"module": md.get("module"), "tags": md.get("tags", []) or []}
        except Exception:
            return {"module": None, "tags": []}

    def find_matching_skills(self, event: TriggerEvent) -> list[SkillSpec]:
        """Return all skills whose triggers match the event."""
        return [s for s in self.skills if match_skill_to_event(s, event)]

    def activate(self, event: TriggerEvent) -> list[LoadedSkill]:
        """Find matching skills, compile their templates with skill context, return ready-to-run bundles.

        If a skill declares `custom_logic.evaluate_trigger`, it's invoked AFTER the
        declarative match — returning False vetoes the activation.
        """
        matched = self.find_matching_skills(event)
        loaded: list[LoadedSkill] = []
        for spec in matched:
            custom = load_custom_logic(spec.custom_logic)

            # Run the custom trigger evaluator if present — may veto the skill
            if custom and custom.evaluate_trigger is not None:
                try:
                    if not custom.evaluate_trigger(event):
                        logger.info(
                            "Skill '%s' vetoed by custom_logic.evaluate_trigger",
                            spec.name,
                        )
                        continue
                except Exception as e:
                    logger.warning(
                        "Skill '%s' custom_logic.evaluate_trigger raised %s; "
                        "treating as veto", spec.name, e,
                    )
                    continue

            template_path = resolve_template_path(
                spec.template.name, self.template_search_paths
            )
            skill_prose = spec.__dict__.get("_prose", "")
            compiled = compile_template(
                template_path,
                registry=self.registry,
                extra_context=skill_prose if skill_prose else None,
            )

            # Filter action candidates through custom validator if present
            candidates = list(spec.action_candidates)
            if custom and custom.validate_action is not None:
                validated = []
                for c in candidates:
                    try:
                        if custom.validate_action(c, {"event": event, "skill": spec.name}):
                            validated.append(c)
                        else:
                            logger.info(
                                "Action candidate '%s' rejected by custom_logic.validate_action",
                                c.id,
                            )
                    except Exception as e:
                        logger.warning(
                            "validate_action raised %s for '%s'; keeping candidate", e, c.id,
                        )
                        validated.append(c)
                candidates = validated

            loaded.append(
                LoadedSkill(
                    spec=spec,
                    compiled_template=compiled,
                    trigger=event,
                    action_candidates=candidates,
                    custom_logic=custom,
                )
            )
        return loaded
