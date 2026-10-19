# obs-operator-mcp

Universal MCP dispatcher operator pack. Ships one operator (`mcp/call`)
that routes any tool invocation on any configured MCP server through the
substrate's `cairn.services.mcp` client.

Reference operator pack #1 of the Phase-2 set — wraps the core service
layer rather than re-implementing transport / connection / auth code.
Trust tier: `builtin`.

## Operator

| Name | Capability | Risk | Executors |
|---|---|---|---|
| `mcp/call` | `mcp.call` | `external_write` (default; per-tool refinement is a Phase 3+ policy concern) | `in_process` |

## Inputs / outputs

```python
CallInput:
    server : str        # logical name resolved by profile.mcp_servers
    tool   : str        # tool name as published by that server
    args   : dict       # opaque payload; conforms to the tool's schema

CallOutput:
    ok     : bool
    result : Any         # parsed result (dict / list / scalar / None)
    error  : str         # populated when ok == False
```

## Usage

A template `operation` node uses the capability ref:

```yaml
- type: operation
  ref:  send-incident-update
```

And `operations/send-incident-update.yaml` binds the capability:

```yaml
capability: mcp.call
input:
  server: comms                     # logical name; profile maps to slack/teams/...
  tool:   chat_postMessage
  args:
    channel: "#incidents"
    text:    $state.summary
```

## Mock mode

`supports_mock = True`. In mock mode the operator echoes the request as
`result = { mocked: true, server, tool, args }` — useful for offline
template development. Substrate-level replay-from-fixture lands in
Phase 3+.

## Discovery

Entry-point group: `cairn.operators`. Declared in this pack's
`pyproject.toml`:

```toml
[project.entry-points."cairn.operators"]
"mcp/call" = "operator_mcp.operators.call:CallOp"
```

The engine walks the group at boot via
`cairn.core.operator.loader.load_all_operators`.
