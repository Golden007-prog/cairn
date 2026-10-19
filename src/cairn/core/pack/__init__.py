"""The pack contract: the domain-neutral seam between engine and packs.

Public surface:
  - :class:`Pack` and the descriptor dataclasses — what a pack contributes.
  - :func:`load_all_packs` / :func:`ensure_loaded` — entry-point discovery.
  - registry helpers — what the engine's consumption points read from.
"""

from __future__ import annotations

from cairn.core.pack.composition import (
    CompositionConflict,
    CompositionReport,
    build_contribution_registry,
    validate_composition,
)
from cairn.core.pack.profile import (
    McpServerSpec,
    Profile,
    ProfileError,
    load_profile,
    load_profile_from_env,
)
from cairn.core.pack.visibility import (
    is_capability_visible,
    is_template_visible,
    owning_pack_for_capability,
    visible_capability_bindings,
)
from cairn.core.pack.loader import (
    DEV_PACK_PATH_ENV,
    PackAssetError,
    assert_flow_assets,
    engine_compat_status,
    ensure_loaded,
    flow_asset_violations,
    flow_ref_collisions,
    flow_triggers_declared,
    installed_engine_version,
    load_all_packs,
    pack_asset_lint_failures,
)
from cairn.core.pack.manifest import (
    ArtifactRequirement,
    DemoSpec,
    ManifestError,
    McpRequirement,
    MemoryRequirement,
    PackManifest,
    RagRequirement,
    find_manifest,
    load_manifest,
)
from cairn.core.pack.protocol import (
    BackendDescriptor,
    CapabilityBinding,
    CheckResult,
    ExecutorDescriptor,
    Pack,
    Status,
    ToolDescriptor,
    TriggerAdapterDescriptor,
)
from cairn.core.pack.registry import (
    all_backends,
    all_capability_bindings,
    all_data_dirs,
    all_doctor_checks,
    all_evals_dirs,
    all_examples_dirs,
    all_policy_dirs,
    all_rag_dirs,
    all_schemas_dirs,
    all_settings_inventory,
    all_skill_dirs,
    all_flow_dirs,
    all_pipeline_dirs,
    all_template_dirs,
    all_tools,
    all_triggers_dirs,
    clear,
    get_composition_report,
    get_pack,
    get_packs,
    register_pack,
    set_composition_report,
)

__all__ = [
    "Pack",
    "PackManifest",
    "ManifestError",
    "McpRequirement",
    "MemoryRequirement",
    "ArtifactRequirement",
    "RagRequirement",
    "DemoSpec",
    "CheckResult",
    "Status",
    "ToolDescriptor",
    "ExecutorDescriptor",
    "TriggerAdapterDescriptor",
    "BackendDescriptor",
    "CapabilityBinding",
    "load_all_packs",
    "ensure_loaded",
    # #1545 — dev loop + [compat] cairn_engine
    "DEV_PACK_PATH_ENV",
    "engine_compat_status",
    "installed_engine_version",
    "load_manifest",
    "find_manifest",
    # §24.3 composition
    "CompositionConflict",
    "CompositionReport",
    "PackAssetError",
    "assert_flow_assets",
    "flow_asset_violations",
    "flow_ref_collisions",
    "flow_triggers_declared",
    "pack_asset_lint_failures",
    "build_contribution_registry",
    "validate_composition",
    # Phase-1 §2 profile layer
    "Profile",
    "ProfileError",
    "McpServerSpec",
    "load_profile",
    "load_profile_from_env",
    "get_composition_report",
    "set_composition_report",
    # §24.3 visibility (P2.d / P2.e)
    "is_capability_visible",
    "is_template_visible",
    "visible_capability_bindings",
    "owning_pack_for_capability",
    "register_pack",
    "get_packs",
    "get_pack",
    "clear",
    "all_tools",
    "all_capability_bindings",
    "all_backends",
    "all_settings_inventory",
    "all_doctor_checks",
    "all_flow_dirs",
    "all_pipeline_dirs",
    "all_template_dirs",
    "all_skill_dirs",
    "all_policy_dirs",
    # Phase 1 asset-dir aggregators
    "all_schemas_dirs",
    "all_triggers_dirs",
    "all_evals_dirs",
    "all_rag_dirs",
    "all_examples_dirs",
    "all_data_dirs",
]
