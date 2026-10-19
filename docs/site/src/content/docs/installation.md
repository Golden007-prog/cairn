---
title: Installation
description: Install cairn with one line, or set it up from source with uv; the optional extras and the engine container.
---

cairn is a Python project managed with [`uv`](https://docs.astral.sh/uv/). The
engine is the distribution `cairndev` (import name `cairn`); each builtin
operator pack, each contract and the example pack is its own package in the
same uv workspace.

## Install

```bash
curl -LsSf https://docs.cairndev.sh/install.sh | sh
```

The installer puts `cairn` on your PATH as a [uv](https://docs.astral.sh/uv/) tool, in an
environment of its own; re-run it to upgrade. If uv is missing, it installs it first with
astral.sh's installer, into `~/.local/bin` and without sudo (like any uv install, that adds the
directory to your shell's PATH). Pick extras or pin a version with environment variables:

```bash
curl -LsSf https://docs.cairndev.sh/install.sh | CAIRN_EXTRAS=hello,rag sh
curl -LsSf https://docs.cairndev.sh/install.sh | CAIRN_VERSION=0.1.0 sh
```

The same without the script: `uv tool install cairndev`, `pipx install cairndev`, or
`pip install cairndev` inside a virtual environment. To remove it: `uv tool uninstall cairndev`.

## Requirements (from source)

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/getting-started/installation/)

## Set up from source

```bash
git clone https://github.com/cairndev-sh/cairn.git
cd cairn
uv sync --extra dev
```

`uv sync` builds the environment from the lockfile. The `dev` extra brings the
test and lint tooling.

:::caution[`uv sync` keeps only the extras you name]
A later `uv sync` (or `uv run`) without your extras removes them. Name every
extra you need on each sync, or call the tools as `.venv/bin/<tool>`.
:::

## Verify

```bash
uv run cairn doctor          # packs, profile bindings, settings, service reachability
uv run cairn ls              # what is installed
```

`doctor` checks that packs load, that profile bindings resolve to registered
operators, and which settings are in effect. Then follow the
[Quickstart](/quickstart/).

## Optional extras

| Extra | Adds |
|---|---|
| `hello` | the example pack (`cairn-pack-hello`) with the sqlite-vec store, asyncssh and DBOS |
| `rag` | the retrieval ingest stack (parse, embed, index) |
| `rag-sqlite-vec` · `rag-pgvector` · `rag-qdrant` | a vector store (`rag-pinecone` is a stub) |
| `ssh` | `remote/exec` over SSH (asyncssh) |
| `train` | the CPU training stack for `train/lora` (torch, transformers, peft, datasets) |
| `infisical` | the Infisical SDK for `secrets/infisical` |
| `storage-postgres` | the Postgres store and bus driver |
| `runtime-dbos` | the DBOS durable runtime |
| `runtime-temporal` | the Temporal runtime (flow legs only) |
| `runtime-sqlite` · `runtime-postgres` | a durable LangGraph checkpointer |
| `pii` | Presidio-backed PII detection |
| `telemetry` | OpenTelemetry export |
| `auth` | argon2 password hashing for server auth |
| `oauth` · `nats` | the OAuth connection broker; the NATS remote-job channel |
| `metaflow` | the Metaflow dataflow engine (`cairn-operator-dataflow-metaflow`) and the Metaflow library |
| `skypilot` | the SkyPilot provisioning engine (`cairn-operator-dataflow-skypilot`) and SkyPilot with its cloud providers |

```bash
uv sync --extra dev --extra hello --extra rag --extra storage-postgres
```

## Container image

The engine ships as a container built from `Dockerfile.engine`. The light
compose stack (`deploy/light`) builds and runs it behind Caddy with Postgres,
and a Helm chart lives in `deploy/helm/cairn`; see [Deployment](/deployment/).

## Run the test suite

```bash
uv run pytest -q -m "not dbos"      # dbos-marked tests need: uv sync --extra runtime-dbos
```
