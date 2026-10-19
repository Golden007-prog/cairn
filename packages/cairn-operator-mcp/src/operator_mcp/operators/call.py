"""mcp/call — universal MCP dispatcher.

Single operator that wraps :class:`cairn.services.mcp.client.McpClient`
and provides the ``mcp.call`` capability. Templates dispatch every MCP
call through this operator regardless of which server or tool — the
substrate-owned client handles transport, connection pool, schema
normalization, and replay (per architecture_obs_agent_locked.md §2:
core services own cross-cutting concerns; providers wrap them).

Failures are bubbled via ``CallOutput.ok = False`` rather than raised so
state-graph callers can branch without exception plumbing. The three
expected failure modes are:

  - Unconfigured server  → ``McpNotConfigured`` from ``services/mcp``
  - Unknown tool on server → ``KeyError`` from ``McpClient.call_tool``
  - Underlying transport error → caught generically with ``type(e).__name__``
"""

from __future__ import annotations

from cairn.core.operator import audit_emit
from cairn.core.operator import (
    Operation,
    OperationContext,
    ReportSpec,
    register_operator,
)
from cairn.services.mcp.client import (
    McpNotConfigured,
    get_mcp_client,
)

from operator_mcp.schemas.call import CallInput, CallOutput


@register_operator(
    name="mcp/call",
    version="0.1.0",
    capabilities=["mcp.call"],
    implements=["contract://mcp.call/1.0.0"],  # #2242 V1-B — cairn-contracts-mcp
)
class CallOp(Operation[CallInput, CallOutput]):
    """Invoke ``tool`` on ``server`` with ``args`` via the substrate MCP client."""

    input_schema = CallInput
    output_schema = CallOutput
    # #1539 — governance declarations; the [operator.*] table is generated from these
    risk = 'external_write'
    supports_mock = True
    reports = ReportSpec.NONE  # ADR-0103: one tool call, nothing to report

    async def run(self, inp: CallInput, ctx: OperationContext) -> CallOutput:
        client = get_mcp_client()
        target = f"{inp.server}/{inp.tool}"
        run_id = getattr(ctx, "pipeline_id", None) if ctx is not None else None
        try:
            result = await client.call_tool(inp.server, inp.tool, inp.args)
        except McpNotConfigured as exc:
            await audit_emit("tool.call", outcome="error", target=target,
                                 run_id=run_id, error="not_configured")
            return CallOutput(ok=False, error=str(exc))
        except KeyError as exc:
            # KeyError stringifies with quoted args; .args[0] is the message.
            msg = exc.args[0] if exc.args else str(exc)
            await audit_emit("tool.call", outcome="error", target=target,
                                 run_id=run_id, error="unknown_tool")
            return CallOutput(ok=False, error=str(msg))
        except Exception as exc:  # noqa: BLE001 — bubble underlying transport errors
            await audit_emit("tool.call", outcome="error", target=target,
                                 run_id=run_id, error=type(exc).__name__)
            return CallOutput(
                ok=False, error=f"{type(exc).__name__}: {exc}"
            )
        # Audit seam (ADR-0028): record the tool invocation at the MCP boundary.
        await audit_emit("tool.call", outcome="ok", target=target, run_id=run_id)
        return CallOutput(ok=True, result=result)

    def mock(self, inp: CallInput, ctx: OperationContext) -> CallOutput:
        """Echo the request without making a real call.

        Useful for template authors developing against this operator
        offline. Phase 3+ ``tests/mcp_fixtures/<server>/<tool>.json``
        replay-from-fixture mode lands at the substrate level — keeps
        this operator's mock simple.
        """
        return CallOutput(
            ok=True,
            result={
                "mocked": True,
                "server": inp.server,
                "tool": inp.tool,
                "args": inp.args,
            },
        )
