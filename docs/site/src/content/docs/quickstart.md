---
title: Quickstart
description: Install cairn from source, start an engine, and run the example's data pipeline through its data gate, person gate and operator gate.
---

From a clean checkout to a governed pipeline that stops for you twice. Run
every command from the repository root: the example pipelines' default paths
resolve against the engine's working directory.

## 1. Install

Requires Python 3.12+ and [`uv`](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/cairndev-sh/cairn.git
cd cairn
uv sync --extra dev --extra hello --extra rag
export CAIRN_PROFILE=examples/hello-cairn/profiles/hello.toml
```

`hello` installs the example pack as the wheel `cairn-pack-hello` plus what its
pipelines reach beyond the base install (the sqlite-vec store, asyncssh,
DBOS). `rag` lets the engine index the example's docs corpus at boot.

`profiles/hello.toml` binds the capabilities the example needs to the builtin
operators: memory, storage, RAG, MCP and secrets.

## 2. See what will happen before anything runs

```bash
cairn pipeline explain hello-cairn/profile-and-publish -e sandbox
cairn validate examples/hello-cairn
```

`explain` lists every leg with the operator class or flow file it resolves to,
the gate that answers it, and the budget, series and approvers. `validate`
checks the pack offline; it needs the example's profile.

## 3. Start an engine

```bash
cairn dev --engine-only --port 8471 &        # SQLite, in-process runtime, login dev/dev
cairn remote login -u dev -p dev -e http://localhost:8471
```

`cairn dev` starts the engine on a pinned SQLite store (`~/.cairn/dev.db`)
with auth on and the user `dev`/`dev`. `profile-and-publish` calls no model,
so no key is needed for this pipeline.

## 4. Run the data pipeline

```bash
cairn remote launch hello-cairn/profile-and-publish --environment sandbox --watch
```

The pipeline profiles `examples/hello-cairn/data/sample.jsonl`, passes its
**data gate**, redacts the summary, runs a flow leg, stages the summary, and
stops at `review`, a **person gate**:

```bash
cairn remote advance <pipeline_id> review        # the person gate
cairn remote advance <pipeline_id> promote       # the operator's own gate
```

It stops a second time at `promote`: `artifact/promote` declares
`requires_approval`, and a pipeline can add a gate but never remove one an
operator declares. Then it publishes and writes an audit row.

```bash
cairn remote findings <pipeline_id>
```

The record ends `done`: `data-ok` has verdict `data`, `review` is approved,
`promote` moved the tag `hello-sample@published`, and `audit` wrote a
hash-chained row to `CAIRN_AUDIT_LOG` (default `./audit.jsonl`).

Try the other outcomes:

- `-i min_rows=1000` — the data gate stops the pipeline (`gate_blocked`)
  before anything is written.
- `--environment hello-prod` — the pack's policy makes each storage write wait
  for an approval.

## Next

- The other three pipelines, step by step: [an assistant over your
  docs](/guides/data-to-assistant/), [a nightly job](/guides/nightly-ops/),
  [a governed fine-tune](/guides/governed-finetune/).
- Every feature on its own: start at [Pipelines](/features/pipelines/).
- A server for a team: [Deployment](/deployment/).
