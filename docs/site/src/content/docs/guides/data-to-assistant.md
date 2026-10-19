---
title: From checked data to an assistant
description: Check a dataset, publish it behind gates, and let the published event start an assistant that answers from docs — profile-and-publish, dataset.ready, ask-the-docs.
---

This guide chains two `hello-cairn` pipelines. `profile-and-publish` checks a
dataset and publishes it only after a person approves; its last write emits
`dataset.ready`; that event starts `ask-the-docs`, an agentic pipeline that
answers a question from the pack's docs.

Features used: [pipelines](/features/pipelines/), [gates](/features/gates/),
[dataflow and artifacts](/features/dataflow-and-artifacts/),
[governance](/features/governance/),
[triggers and events](/features/triggers-and-events/),
[models and agents](/features/models-and-agents/).

## 1. Install and start an engine

Run everything from the repository root: the pipelines' default paths resolve
against the engine's working directory.

```bash
uv sync --extra dev --extra hello --extra rag
export CAIRN_PROFILE=examples/hello-cairn/profiles/hello.toml

export OPENAI_API_KEY=sk-...                 # or OPENAI_BASE_URL=<OpenAI-compatible endpoint>
export RAG_EMBED_PROVIDER=openai
export CAIRN_RAG_AUTOINDEX=1                 # index rag/docs at boot
export CAIRN_MCP_SERVERS='{"hello-calc": {"transport": "stdio", "command": "python",
  "args": ["examples/hello-cairn/tools/calc_server.py"]}}'
cairn dev --engine-only --port 8471 &
cairn remote login -u dev -p dev -e http://localhost:8471
```

`cairn dev` pins a SQLite store (`~/.cairn/dev.db`), the in-process runtime and
the in-process bus, and signs you in as `dev`/`dev`. To run the same chain on
Postgres or DBOS, start `cairn-server` with those variables set instead (see
[Backends](/features/backends/)).

## 2. Turn on the event trigger

`ask-the-docs` ships its `kind: event` trigger off. Set `enabled: true` in
`examples/hello-cairn/pipelines/ask-the-docs.yaml` and restart the engine:

```yaml
triggers:
  - kind: event
    topic: dataset.ready
    enabled: true
    description: Ask the docs again whenever the sample dataset is published.
```

The engine arms declared triggers at boot, in the `default` workspace.

## 3. Check and publish the data

```bash
cairn pipeline explain hello-cairn/profile-and-publish -e sandbox
cairn remote launch hello-cairn/profile-and-publish --environment sandbox --watch
```

What happens, leg by leg:

1. `profile` (`data/profile`) reads `examples/hello-cairn/data/sample.jsonl`
   and resolves the `text` and `label` roles.
2. `data-ok`, a **data gate**, passes only if the verdict is `pass` and there
   are at least `min_rows` rows. With `-i min_rows=1000` it stops the pipeline
   (`gate_blocked`) before anything is written.
3. `redact` (`redact/text`) replaces emails, phone numbers and keys in the
   profile's summary.
4. `engine-check`, a flow leg on the builtin engine, registers the file's
   content as a version of the dataset `hello-sample`.
5. `stage` writes the redacted summary under `CAIRN_ARTIFACTS_DIR`.
6. `review`, a **person gate**, pauses with the profile and the redacted text
   on the card.

```bash
cairn remote advance <pipeline_id> review        # the person gate
cairn remote advance <pipeline_id> promote       # artifact/promote's own gate
```

7. `promote` (`artifact/promote`) waits for its own approval, then points
   `hello-sample@published` at the flow leg's artifact.
8. `publish` writes the summary to `hello-published` and **emits
   `dataset.ready`**.
9. `audit` appends a hash-chained row to `CAIRN_AUDIT_LOG`.

## 4. The event starts the assistant

The `dataset.ready` message starts one `ask-the-docs` run. Its `assistant`
leg searches the `docs` corpus, re-ranks, asks the model, measures the answer
with the `word_count` tool on the example's MCP server, and remembers the
exchange; `length` flags an answer longer than `max_words`; `review` waits for
a person.

```bash
cairn remote pipelines                           # the event-started run appears
cairn remote advance <ask_pipeline_id> review
cairn remote findings <ask_pipeline_id>          # hits, answer, answer_stats, remembered
```

The listener runs on the engine's leader: a message published while no leader
is subscribed starts nothing, on either bus. The Postgres bus additionally
replays an `emits` event to a pipeline **step** that starts waiting on it
later (`trigger: on:dataset.ready`).

## Variations

- **Run it in `hello-prod`.** `--environment hello-prod` applies the pack's
  policy: `stage` (and `publish`) wait for an approval before they write.
- **Start the check from a watch instead.** Turn on `profile-and-publish`'s
  `dataset` watch: each tick starts the pipeline only when `hello-sample`
  gained a version.
- **Start it from another system.** Turn on its webhook and
  `POST /triggers/hello-sample`.

## What to check

- `cairn remote findings <pipeline_id>` on the first pipeline shows the
  verdict, the staged and published URIs, the promoted artifact and the audit
  hash.
- The audit log has one new row per `audit` leg.
- A pipeline may not start on a topic its own legs emit, so this chain cannot
  loop.
