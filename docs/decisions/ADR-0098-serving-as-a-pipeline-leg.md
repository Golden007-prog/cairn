# ADR-0098 — Serving as a pipeline leg: endpoint as a leg output, stop as a finalizer

- **Status**: Accepted (code-complete; the live halves are DEFERRED — see §8)
- **Date**: 2026-08-30
- **Issue**: #1788 (part of epic #1793, "the no-bolt-on rule")
- **Builds on**: ADR-0097 (`finalizer:`, `compute/acquire` → `target`), ADR-0095
  Amendment 2 ruling 1 (measurement serves the training-time base), #1718
  (`serve/vllm` reachability), #1719 (base and adapter get distinct model ids),
  #1544 (`as_ssh_params` lifted into core)

## 1. Context

Every continual-learning measurement session in the 08-26→30 campaign did the
same three things by hand:

1. SSH to the GPU box and launch `hf_shim_flagship.py` — a ~90-line HTTP shim
   re-written from memory each session, with the base model id, the adapter
   routing and the `disable_adapter()` champion trick reconstructed each time;
2. run `ssh -f -N -L 172.18.0.1:8600:127.0.0.1:8600 …` in a spare terminal, so
   the eval workers on the control-plane box could reach an endpoint on the GPU
   box;
3. paste the resulting URL into the pipeline's `champion_endpoint` /
   `challenger_endpoint` inputs.

None of it was on the record. The flagship's `serve-challenger` gate did not mean
"a human approves the serving configuration"; it meant "a human goes and does
SSH", which #1459 recorded as temporary. This is the single largest bolt-on of
the saga, and #1788 is the slice that deletes it.

What already existed: `serve/vllm` (reachable since #1718; two model ids since
#1719, never fired on real hardware), `$steps.<id>.<field>` wiring, and — as of
ADR-0097 — `finalizer: true` plus a `compute/acquire` that outputs the connection
name downstream legs can bind.

## 2. Decision

Two operators in the existing `operator-serving` pack:

* **`serve/start`** (`risk = paid_compute`, long-running) — takes a `target`
  (a `compute.ssh` connection name, wired from `$steps.acquire.target`), a
  `backend`, a `base_model`, an adapter map, and a `port`. It stages adapters,
  launches the server on the box, waits for the socket, opens a tunnel, proves
  the endpoint answers a **real completion**, and outputs
  `{endpoint_url, model_ids, pid_or_unit, …}`.
* **`serve/stop`** (`risk = external_write`, never approval-gated) — takes
  `(target, port)` and kills the server's process group and the tunnel.
  Idempotent. Declared `finalizer: true` in the shipped pipeline — the **second
  user of ADR-0097's one engine feature**, which is what that feature was built
  for.

And a third piece of shared code: **`cairn.core.ssh_exec`**, the lifted SSH
primitive (§6).

## 3. Reachability: an SSH `-L` tunnel owned by the leg

The eval legs run where the bridge runs; the server runs on another box. Two
mechanisms were available.

**Chosen: the tunnel.** The serve leg opens `forward_local_port` on the same SSH
connection it used to launch, and publishes the **local end** as `endpoint_url`.

- It reuses the `compute.ssh` credential the pipeline already holds. No new auth
  surface, no security-group change, no new cloud-write permission behind a
  serving leg.
- It never exposes an unauthenticated OpenAI endpoint to a network. `api_key_env`
  is still unwired (#1721), so a server bound to `0.0.0.0` plus an SG rule would
  be open to whatever that rule allows.
- It is what the sessions did by hand. The leg is therefore the same procedure on
  the record, not a new one to debug.
- `bind_host` is an input because the sessions needed it to be: eval workers
  running in containers on the control-plane host cannot reach `127.0.0.1`, which
  is why every session bound the docker bridge gateway (`172.18.0.1`).

**Rejected: bind on the private VPC address.** It requires the boxes co-located
in one VPC, an SG ingress rule, and the private IP — none of which the
`compute.ssh` connection carries. Adding SG mutation to make it work would put
new cloud-write surface behind a serving leg, for a reachability problem the
existing credential already solves.

**The honest limit.** The tunnel belongs to the control-plane PROCESS. A restart
takes it with it, and the published URL stops answering; the eval leg then fails
loudly, which is the correct outcome — a stale URL that silently answered from
somewhere else would be worse. `serve/stop`'s remote half never depended on the
tunnel, so the teardown still works after a restart, reporting
`tunnel_closed: false` rather than inventing one.

## 4. The health check is a completion, not a liveness route

`serve/vllm` polls `GET /health` and reports `status: "failed"` as a *successful*
operator call. Both halves are wrong for a leg:

- A bound port proves a socket, not a loaded adapter. A server can answer
  `/health` and still fail its first forward pass (CUDA OOM, a wrong base id, an
  adapter that did not load). The flagship's "healthy in ~40s" signal was a bare
  `ok` string, after which every session hand-ran a `curl` completion to check it
  really worked. **That curl is now the health check.**
- A leg that reads `done` while publishing an empty endpoint hands its failure to
  the next leg, where it surfaces as an eval *error rate* rather than as a
  serving problem. So `serve/start` **raises**, with the server's log tail in the
  message (criterion 2).

The probe runs **through the published URL**, so passing it proves criterion 3
(reachable from where the probe runs) rather than merely that something is alive
on the box. One `health_timeout_s` budget covers launch, socket, and answer —
two independent waits would let a leg take twice its declared bound.

## 5. `hf-shim` as the fidelity backend

ADR-0095 Amendment 2 ruling 1: a continual-learning measurement must serve the
*training-time* base. For these adapters that is a **prequantized 4-bit repo**
whose name the adapter records in `adapter_config.json`; vLLM loads its own base
and applies the LoRA to weights training never saw, so its delta is partly
quantization. Hence two backends, and the pack says so:

- **`hf-shim`** — the reference for measurement. One process, one base load,
  `disable_adapter()` for the base id and `set_adapter()` per adapter id. Greedy
  decoding always (`do_sample=False`; a request's `temperature` is accepted and
  ignored) — a measurement whose numbers depend on a sampling seed is not a
  measurement. A global lock serializes generation, because `set_adapter` mutates
  state shared by every request.
- **`vllm`** — production throughput; its numbers may diverge, and the README and
  the pipeline input both say so.

Three things the shipped shim does that the hand-written one did not, each
because the hand-written one's absence of them cost a session:

1. **The base string is checked against every adapter's
   `base_model_name_or_path`** and a mismatch refuses to start. The flagship shim
   hard-coded that string and a human kept the two in sync by remembering.
2. **A generation failure is a 500 with a reason**, not an unhandled exception
   that resets the connection — a reset is indistinguishable from the server
   having crashed, which is what the probe exists to detect.
3. **A POST to an unknown route is a 404.** The flagship shim answered a POST to
   *any* path; a typo'd route that generates is a measurement nobody knows is
   mis-addressed. This is its own 400-on-unknown-model ruling, applied to paths.

The shim is pack code shipped as data: `serve/start` reads the module's own
source and SFTPs it, so the file that runs on the box and the file this pack
tests cannot be different files.

## 6. `cairn.core.ssh_exec` — the fifth caller, not the fifth copy

`asyncssh.connect(...)` with the compute.ssh key/known_hosts mapping was already
written out four times (`remote/exec`, `serve/vllm`, the dataflow SSH engine,
`connection_probe`) — the shape #1544 lifted `as_ssh_params` out of, where one
host-key fix meant four edits. `serve/start` needed a fifth *plus* two primitives
nobody had (ship a file, forward a port), so the primitive is lifted rather than
copied.

It is deliberately small: connect, run, write a file, forward a port, wait for a
remote port. It is **not** a job protocol — `remote/exec`'s submit/poll/adopt
semantics (#337 / #551 / #601 / #603 / #708) stay in that operator, which owns
them. What moved is only the part every caller had verbatim. Two of those
hard-won details ARE reproduced in `serve/start`, with their issue numbers,
because they are correctness-critical for any detached launch: the `cd && { setsid
… & }` form (#601 — the obvious `A && B &` holds the SSH channel open for the
server's whole life) and killing the process **group** (#603 — `kill <pid>` reaps
the wrapper and orphans the GPU process, a leak that reads as a clean stop).

## 7. Risk classes

**`serve/start` = `paid_compute`, unconditionally** — not "when the target is
metered". A risk class is read *statically*, by the bridge's restart policy and
the approval resolver, before any input is seen; a class that is right only
sometimes is a `read_only` default in disguise, which is exactly the defect
CLAUDE.md rule 7 records against `infer/batch`. This leg holds a GPU for the
length of a measurement — that is spending, even though it is not what started
the box. The consequence is intended: a `paid_compute` leg caught by a restart is
not auto-resubmitted, because relaunching over a server that may still be up
stacks two on one GPU and OOMs it (#708).

**`serve/stop` = `external_write`, `requires_approval = False`** — `compute/release`'s
reasoning, unchanged. The higher classes route to `high` in the approval
resolver, where a deployment's HITL policy bites; a teardown that can pause
behind an unanswered gate is a GPU held while it waits for a human. The class
records honestly that it mutates state off this box; it is never gated, because
the direction of the mutation is *stop spending*.

## 8. One server or two, for champion and challenger

**One server at a time, carrying every model id, restarted once per cycle.**

The restart is *forced, not chosen*: the challenger adapter does not exist until
`train` has run, so no single server lifetime can span both measurements of a
cycle. Both serve legs therefore use the same `port`, and the second **replaces**
the first — one GPU holds one base, and two ports would mean two servers stacked
on one card.

What the layout achieves is the one ADR-0095 §2.5 asks for: the post-train server
carries the base id, every standing champion adapter, **and** the challenger, so
both sides are addressable from one process and a re-measure of the champion
needs no further restart. Composing that adapter map needed no grammar change —
`adapters` (a dict `$input`) plus an `adapter_id`/`adapter_uri` pair (the scalar
`$steps.train.…_adapter_uri`), merged by the operator, because a pipeline wire
carries a single value.

**The limit, stated rather than implied**: within a cycle the champion sweep runs
against the *previous* server lifetime — same base string, same backend, same
operator, but not literally the same process. Amendment 2's corollary ("both
sides must always come from the same server in the same session") is satisfied to
the extent one cycle can satisfy it. A measurement that needs one process for
both moves the `champion` leg to depend on `serve-challenger` instead; that is
one `depends_on` and one wire, and the def is written so it stays that cheap.

## 9. Compatibility (criterion 6)

`champion_endpoint` / `challenger_endpoint` become **optional** with
`default: null` — not `""`, because `$default` falls back on missing-or-null only
and an empty string is a real value it would hand to the harness (#1728, which
broke a dress rehearsal). The eval legs wire
`$default($input.<x>_endpoint, $steps.serve-<x>.endpoint_url)`, so a hand-pasted
endpoint **wins** and an existing launch behaves exactly as before. They carry a
deprecation note and go away when #1776 closes and the flagship adopts this file.

## 10. What is NOT proven

No box was started, no GPU was held, no model was downloaded, and no tunnel
reached a real host. `cairn.core.ssh_exec.connect` is the single seam, stubbed in
every test and pointed at a fake by `CAIRN_SSH_CONNECT_FACTORY` in the local
drive — the mirror of ADR-0097's `CAIRN_EC2_CLIENT_FACTORY`. The live halves
belong to the owner's authorized GPU session:

- **criterion 3's live half** — the champion sweep completing with
  `error_rate 0` against a tunnel no human started;
- **criterion 5** — backend `hf-shim` reproducing cl-cycle2's recorded numbers on
  the w2 harness (paraphrase 49.14%, control 84.47%, to 4dp). The shim here is a
  port with three deliberate behavioural changes (§5); until it has re-run that
  harness, byte-faithfulness is a claim, not a measurement.
