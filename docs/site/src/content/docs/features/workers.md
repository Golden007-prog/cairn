---
title: Workers
description: cairn worker --pull — a machine that claims jobs from the control plane over HTTP, with no inbound port. Not proved by the example.
---

:::caution[Not proved]
No `hello-cairn` leg targets a pull worker, so the feature proof does not
exercise this path. What follows is what the command and the API provide.
:::

A **pull worker** is a machine that runs work for the control plane by
dialing out to it. It claims a job from the control plane's job store over
HTTP (`POST /jobs/claim`), runs it, heartbeats to hold its lease, and reports
back. There is no inbound port and no central SSH key, so the machine can sit
behind NAT. It needs no Temporal.

## Setup

The worker is the `cairn` CLI on the machine that does the work:

```bash
export CAIRN_ENGINE_URL=https://cairn.example.com    # the control plane's base URL
export CAIRN_ENGINE_TOKEN=...                         # a bearer token
cairn worker --pull --capabilities gpu=T4
```

By default the worker runs the job's container image (`docker run`, with
`--gpus all`); `--exec-local` runs the operator in the worker's own process
instead.

## Configuration

| Flag | Effect |
|---|---|
| `--engine-url` (`CAIRN_ENGINE_URL`) | the control plane's base URL |
| `--token` (`CAIRN_ENGINE_TOKEN`) | bearer token; a per-run worker token works |
| `--capabilities`, `-c` | what this machine offers, e.g. `gpu=T4` (repeatable) |
| `--image` | pin the container image to run, overriding the job's |
| `--exec-local` | run the operator in this process instead of a container |
| `--no-gpus` | drop `--gpus all` from the `docker run` (a CPU worker) |
| `--once` | one claim pass: run a job if there is one, exit 0 if not, non-zero if the control plane refuses or cannot be reached |
| `--lease-seconds` | how long a claim is held before the job is re-queued (default 300) |
| `--poll-seconds` | wait on an empty queue before asking again (default 5) |
| `--worker-id` | this worker's identity (default: hostname plus a random suffix) |

`cairn worker` without `--pull` runs a Temporal worker instead (`--remote`,
`--dataset` or `--task-queue` pick its queue); see
[Backends](/features/backends/#runtime).

## Example

There is no example leg for a pull worker. `--once` is the way to check that a
machine can reach the control plane and claim:

```bash
cairn worker --pull --once --no-gpus
```

## Limits

- **NOT PROVED end to end**: nothing in `hello-cairn` routes a leg to the pull
  lane.
- The worker runs container images by default; the machine needs Docker (and
  the NVIDIA runtime for `--gpus all`) unless it uses `--exec-local`.
