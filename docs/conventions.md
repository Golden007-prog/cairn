# Code conventions

Short, opinionated, enforced by review. Keeps the codebase consistent
without ceremony.

## Logging

**Principle: log at boundaries and decisions, not in pure logic.**
"Logs everywhere" produces noise and unsearchable grep soup. Logs at the
right places are the operator's only window into what the engine did
when a real incident happens.

### Where logging belongs

| Layer | Log? |
|---|---|
| CLI command orchestrators (`src/cairn/cli/commands/`) | ✓ entry + exit + decisions |
| Engine boundaries (Runner, Dispatcher, ActionHandler) | ✓ lifecycle + outcomes |
| Storage operations (CRUD, connection pool, schema setup) | ✓ counts + lifecycle |
| Tool/integration calls (HTTP, subprocess, MCP) | ✓ start + result/error |
| Backend probes (doctor, reachability) | ✓ what was checked + outcome |
| Pack loader + registry resolution | ✓ what loaded, what was skipped |
| Error paths caught-and-continued | ✓ always |

### Where logging does NOT belong

| Layer | Why not |
|---|---|
| Pure scorers / validators / pure functions | Outputs are asserted in tests; logs are noise |
| Pydantic models / dataclasses / type-only files | No behavior to log |
| Thin adapter wrappers (delegating call) | Log at the place the work happens |
| Internal data transforms inside a private helper | Pollutes operator view |
| Returning from a function | If it succeeded, the caller's log captures the outcome |

### Levels

| Level | When |
|---|---|
| `debug` | Decisions/branches inside private logic, fine-grained tracing. Operators turn this on when something is wrong. |
| `info` | Lifecycle events at boundaries — "loaded N packs", "schema initialized", "run X completed". One per significant operation, not per micro-step. |
| `warning` | Recoverable surprises — "unknown env scheme, falling back", "optional dep missing", "rate-limited, retrying". |
| `error` | Caught exceptions where we continue anyway. Pair with `exc_info=True` when the traceback would help. |
| `exception` | Always inside `except` when logging-and-swallowing. |

### Mechanics

- **Module-level logger**: `logger = logging.getLogger(__name__)`. Gives
  searchable names like `cairn.cli.commands.run`.
- **No string concatenation** in log calls. Use lazy `%`-formatting:
  `logger.info("loaded %d packs", n)`, not
  `logger.info(f"loaded {n} packs")`. Lazy form skips the format step
  when the level is disabled.
- **Structured fields where they help**:
  `logger.info("eval completed pack=%s total=%d passed=%d failed=%d errored=%d", pack, total, passed, failed, errored)`
  is far more useful than `logger.info("eval done")`. Operators can grep
  on it.
- **Legacy `"templates_v2.*"` logger names should migrate to `__name__`**
  opportunistically when files are touched. Don't sweep just for this.

### Anti-patterns

| Don't | Do |
|---|---|
| `logger.info("entering function")` at every entry | One info per *boundary* (CLI command, storage op, integration call) |
| `logger.debug(state_dict)` of large objects | Log the *fields* you need, not the whole blob |
| `print()` in library code | Use the logger; reserve `print` for CLI user-facing output |
| `logger.error("oh no")` without exc_info or context | `logger.error("operation X failed for %s", target, exc_info=True)` |
| Logger name strings like `"foo"` or `"templates_v2.bar"` | `__name__` |

## Type hints

Required on every public function and method. Internal helpers can skip
when types are obvious from the call site. We use `Optional` /
`list[T]` / `dict[K, V]` / Pydantic models / Protocols extensively.

Type hints are not optional polish — they are how interfaces are
expressed. PRs that drop them on public surfaces get bounced.

## Where this lives

This file is the authority. `decisions/` (the ADR log) + `architecture/`
are the architecture; this is the style. If a future PR introduces a logging
pattern that contradicts what is here, the PR loses.
