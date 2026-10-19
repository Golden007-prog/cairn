# cairn/core/approval — the HITL gate (decision + enforcement)

**Purpose.** Compute the effective approval gate for an operation or flow leg and refuse to
dispatch a gated one without an `ApprovalRecord` (ADR-0034: non-bypassable).
**Owns.** `resolver.py:resolve_gate` (PURE decision: operator `risk`/`requires_approval`
from the installed manifest + `[profile.approval]` + binding strictness-only + the
PolicyEngine); `enforce.py` (`resolve_runtime_gate`, `enforce_gate`, `enforce_flow_gate`,
`approved_state`, `gate_to_request`); `policy_source.py` — the ONE cached runtime
`PolicyEngine`; `surface.py` (request shaping); `reaper.py` (stale approvals).
**Does not own.** Graph pausing (`compiler.py` calls `interrupt()`), the review store
(`storage/reviews.py`), policy rule loading (`policy/`), UI.

**Contracts.** Callers: `operator/runtime.py:950` (every operator), `pipeline_bridge.py:1332`
(task legs), `pipeline_bridge.py:1121` (flow legs, aggregate risk), `compiler.py:1591`
(node pre-check). Four bypass vectors are closed in the resolver (omission, `required:
false`, route-around, different op) — keep it that way: authority comes from the deployment,
a pack may only tighten.

**Allowed deps.** `operator` (types), `policy`, `pack` (profile), `storage` (reviews),
`engine` (`ApprovalRecord` type), `runtime` (lazy). **Forbidden.** operator packs,
`cairn.packs.*`, `triggers`, `cli`.

**Testing.** `tests/core/test_approval_chokepoint.py`, `test_approval_resolver.py`,
`test_approval_gate_e2e.py`, `test_approval_policy_wiring.py`, `test_approval_surface.py`,
`test_approval_reaper.py`, `test_leg_approval_gates.py`, `test_flow_leg_gate_guard.py`. A
change here must show a gated op being REFUSED, not only allowed.

**Common mistakes.** Constructing a second `PolicyEngine()` instead of
`runtime_policy_engine()`; adding a call site that checks `requires_approval` itself;
letting mock mode skip gates in production paths (it is intentional in tests only — see
`test_mock_mode_skips_the_gate`).

**Read.** `docs/architecture/08-governance.md`, ADR-0034, ADR-0028, ADR-0083 R1.
