---
title: Monitor and run hooks
description: Threshold checks, proposals a person approves, and run hooks — code a package installs that runs when a run or leg settles.
---

## Monitor

The builtin monitor pack (`cairn-operator-monitor-threshold`) has three
operators:

| Operator | Does | Governance |
|---|---|---|
| `monitor/threshold` | compares a value with a threshold and reports `crossed` | read-only |
| `monitor/propose` | files a proposal (a metric, its value, the workflow to run) for a person | `requires_approval`: waits for a person before it runs |
| `monitor/dispatch` | launches the approved workflow | risk `paid_compute`; does not ask a second time |

### `monitor/threshold`

`comparator` is `gt`, `gte`, `lt` or `lte`; `crossed` is true when the
comparison holds. The value comes inline (`value`), from a JSON file
(`source_path` + `source_key`) or from a JSON URL (`source_url` + `source_key`).

```yaml
# examples/hello-cairn/pipelines/ask-the-docs.yaml
- id: length
  label: Check the answer's length
  task: monitor/threshold
  depends_on: [assistant]
  with:
    metric: answer words
    value: $steps.assistant.answer_stats.words
    threshold: $input.max_words
    comparator: gt
```

Pair it with a data gate on `crossed` to stop a pipeline
(`tiny-finetune`'s loss gate does this; see [Gates](/features/gates/)).

### `monitor/propose`

```yaml
# examples/hello-cairn/pipelines/nightly-ops.yaml
- id: propose
  label: Propose the follow-up
  task: monitor/propose
  depends_on: [summarise]
  with:
    metric: report lines
    value: $default($steps.job.report.lines, 0)
    threshold: $input.max_lines
    proposed_workflow: "pipeline:hello-cairn/profile-and-publish"
```

The leg waits on the approval queue with the proposal on the card; approve it
with `cairn remote advance <pipeline_id> propose`. Its output carries
`approved`. `monitor/dispatch` takes that verdict and launches
`proposed_workflow` as its own governed run; a proposal that was not approved
launches nothing.

## Run hooks

A **run hook** is an async callable `(event, payload) -> None` that a
package registers under the `cairn.run_hooks` entry-point group. The engine
fires events as runs move; whoever is installed listens. Absent means off.

| Event | Fired when | Payload (besides `event`) |
|---|---|---|
| `run.admitting` | before a run's record exists | `run_id`, `template_ref`, `workspace_id`, `series_id`, `estimated_usd`, `inputs` — a provider may refuse the run by raising `AdmissionRefused` |
| `run.launched` | a run is admitted and queued | `run_id`, `template_ref`, `workspace_id`, `series_id`, `lane` |
| `run.finished` / `run.failed` | a run reached a terminal state | `run_id`, `status`, `store` |
| `leg.finished` | one leg of a pipeline settled | `pipeline_id`, `step_id`, `run_id`, `status` |
| `run.events_ingested` | a worker's report batch was appended | `run_id`, `events`, `store`, `pipeline_store` |
| `workspace.created` | a workspace was created | `workspace_id`, `actor`, `source` |

Providers run one at a time in entry-point name order. A failing provider is
logged and the others still run; only `run.admitting` can refuse.
`cairn doctor` lists a provider that failed to load.

### The example's listener

```toml
# examples/hello-cairn/pyproject.toml
[project.entry-points."cairn.run_hooks"]
hello-receipts = "cairn_pack_hello.hooks:receipts"
```

```python
# examples/hello-cairn/hooks.py (abridged)
EVENTS = ("leg.finished", "run.finished", "run.failed")

async def receipts(event: str, payload: dict[str, Any]) -> None:
    """``(event, payload) -> None``, the run-hook signature."""
    if event not in EVENTS:
        return
    line = {k: v for k, v in payload.items() if isinstance(v, (str, int, float, bool)) or v is None}
    line["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # Hooks run on the engine's event loop; the file write goes to a worker thread so a
    # slow disk never stalls the dispatcher.
    await asyncio.to_thread(_append, receipts_path(), json.dumps(line, sort_keys=True))
```

Every settled leg and finished run appends a line to
`$CAIRN_HOME/hello-cairn/receipts.jsonl` (`CAIRN_HOME` defaults to `~/.cairn`).

## Setup

The monitor operators ship with the engine. A run hook fires only from an
**installed** distribution: `uv sync --extra hello` installs the example wheel
and its listener. A dev pack loaded through `CAIRN_PACK_PATH` declares no entry
points, so its hooks do not fire.

## Limits

- No example pipeline calls `monitor/dispatch`.
- A hook sees every run on the engine, not only its own package's.
- Hooks run on the engine's event loop: do slow work off the loop, as the
  example does with `asyncio.to_thread`.
