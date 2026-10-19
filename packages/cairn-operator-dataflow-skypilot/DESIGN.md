# cairn-operator-dataflow-skypilot — design

- **Distribution:** `cairn-operator-dataflow-skypilot` (`cairn/operators/optional/dataflow_skypilot/`),
  offered by `cairndev[skypilot]`
- **Capabilities provided:** none as operators — it registers the `skypilot` dataflow engine
  (`cairn.dataflow_engines`), which `dataflow.run` and `ml.run` submit to when selected
- **Status:** experimental — unit-tested against a fake SkyPilot SDK; not in the hello-cairn
  feature proof
- **Decisions:** ADR-0104 D5 (SkyPilot as the provisioning engine), ADR-0099 §1 (derive, refuse,
  never provision), ADR-0134 D3 (an optional wheel; its launch check is the engine-neutral
  flow-engine door asking this engine's `check_ready()`), #2073, #2102

## 1. Purpose

Run a cairn flow on a machine this engine ACQUIRES — an SSH node pool built from a
`compute.ssh` box, or a Vast / RunPod / AWS instance — and give it back afterwards. It is the
engine for an environment that has no standing compute. It does NOT add operators, govern, or
register artifacts (the `dataflow/run` / `ml/run` operators of cairn-operator-dataflow do), and
it never provisions to make a readiness check pass. Until ADR-0134 it lived inside
cairn-operator-dataflow; it moved out so a default `cairndev` install carries no cloud-SDK
adapter.

## 2. Architecture

```
dataflow/run | ml/run  (cairn-operator-dataflow; governance, budget, artifacts)
        │ resolve_engine(kind) — core: cairn.core.dataflow.engine_registry
        ▼
skypilot_engine(kind, target)     factory.py — reads the skypilot_* backend keys
        ▼
SkyPilotEngine.submit/poll/result/cancel   engines_skypilot.py — sky.launch / sky.status / sky.down
        │ the task YAML runs cairn-dataflow-run (cairn-operator-dataflow) on the machine
        ▼
SSH node pool | Vast | RunPod | AWS
```

Before any of that, the launch door (`cairn.core.dataflow.engine_door`, ADR-0134 D3c) builds
this engine and awaits `SkyPilotEngine.check_ready()`: a target is set, the extra is
importable, `ssh` and `rsync` are on PATH, the target resolves to an infra, that infra is
enabled (`sky check`). Imports: `cairn.core.*` only (the operator contract,
`cairn.core.dataflow` incl. the shared `resources` helpers); never another operator pack; `sky`
lazily.

The one engine that *acquires* the machine. Three protocol calls map onto three
SkyPilot calls — `submit`→`sky.launch`, `poll`→`sky.status`, `cancel`→`sky.down`
— and nothing above the engine changes: the spec still travels as
`CAIRN_DATAFLOW_SPEC`, the machine still runs `cairn-dataflow-run`, the curve
still streams over the job channel. Five properties are this module's alone,
because nothing above it can enforce them:

1. **The cost estimate runs BEFORE `sky.launch`** and refuses; a refused submit
   has provisioned nothing.
2. **Every terminal state tears the cluster down, exactly once** — success,
   failure and cancellation alike.
3. **The cluster carries its own deadline.** Autostop (as autoDOWN, so the disk
   goes too) is set from the run's `budget.max_seconds` plus a grace window, or
   a fixed 120-minute ceiling when the run declared no ceiling. It is the last
   line of defence, not the first — it covers only the case cancel and teardown
   cannot: a control plane that dies mid-run.
4. **Every teardown writes a receipt** (cluster, infra, resources, duration,
   estimate, and how many outputs came home) as a `skypilot.receipt` run event,
   so what a run bought is on the run's own timeline instead of in a cloud
   console.
5. **The result comes home before the machine stops existing** (#2102). The
   flow runs in `$HOME/.cairn-jobs/<cluster>` — named explicitly, because
   `cairn-dataflow-run` writes `result.json`/`metrics.json` into `os.getcwd()`
   and SkyPilot's run script only does `cd ~/sky_workdir`, a directory that
   need not exist. On every terminal state *and on cancel*, `_teardown` rsyncs
   the declared outputs off the cluster **before** `sky down`; fetching after
   would lose the product with the disk.

The SDK sits behind one seam (`sky_sdk()`, overridable with
`CAIRN_SKY_SDK_FACTORY`, mirroring `compute_lifecycle.ec2_client`) because a
live `sky launch` provisions a paid machine: the adapter has to be provable
without one. The fetch has its own seam (`fetch=`) for the same reason.

`SkyPilotEngine.result()` reads the fetched `result.json` and is byte-for-byte
the SSH engine's contract, through the SSH engine's own functions —
`fetch_list()` (which files), `fetch_dir()` (where they land:
`$TMPDIR/cairn-remote-<cluster>`, deterministic so a restarted control plane
finds them) and `rewrite_fetched()` (remote workdir paths in `stats` → the
local copies). One result contract, two transports: a pipeline hoist and the
next leg cannot tell which engine ran this one. A missing `result.json` is a
loud refusal naming the receipt to check, never an empty result.

**Why rsync and not the SDK.** Checked against skypilot 0.13.0: `sky.client.sdk`
has no generic file-download call — `download_logs` returns the job's *logs*,
not the workdir. What SkyPilot does write, for every cluster it manages, is a
per-cluster SSH stanza at `~/.sky/generated/ssh/<cluster>`
(`SSHConfigHelper.add_cluster`; `SKY_USER_FILE_PATH = '~/.sky/generated'`)
whose `Host` is the cluster name and which already carries the address, user,
key, port and any proxy command. `rsync -e "ssh -F <that file>"` is therefore
the transport SkyPilot itself uses, with nothing re-derived here. It is also
why the door refuses when `ssh`/`rsync` are missing (below).

Target resolution turns a cairn connection into a SkyPilot infra. The door asks
for the infra *name* only (`materialize=False`) — a dry run must not put a
private key or an API token on disk; only a real submit writes these:

| Connection kind | Becomes | What lands on disk |
|---|---|---|
| `compute.ssh` | `infra: ssh/<pool>` | one entry in `~/.sky/ssh_node_pools.yaml` (merged, never clobbered) + the key at `~/.sky/cairn-keys/<pool>.pem`, mode 0600 — SkyPilot reads the key as a PATH |
| `compute.vast` | `infra: vast` | `~/.config/vastai/vast_api_key`, mode 0600 — the only place SkyPilot's Vast provider reads it |
| `compute.runpod` | `infra: runpod` | `~/.runpod/config.toml`, mode 0600 — what `runpod config` writes |
| any connection holding `AWS_ACCESS_KEY_ID` | `infra: aws` | nothing: the keys are passed as process env for the SDK call only |

## 3. Operators

None. The pack registers one `cairn.dataflow_engines` entry, `skypilot` →
`operator_dataflow_skypilot.factory:skypilot_engine`. The operators that reach it are
`dataflow/run` and `ml/run` in cairn-operator-dataflow.

## 4. Configuration

| Key | Kind | Scope | Default | Meaning |
|---|---|---|---|---|
| `ml_engine` / `dataflow_engine` = `skypilot` | backend field | deployment | `builtin` | select this engine for a flow kind |
| `ml_target` / `dataflow_target` | backend field | deployment | `` | the compute connection to provision on — required; empty is a refusal at the door |
| `skypilot_image` / `CAIRN_SKYPILOT_IMAGE` | backend field | deployment | `` | image the provisioned machine runs; empty = a `pip install` setup step (slower, driftable) |
| `skypilot_accelerator` / `CAIRN_SKYPILOT_ACCELERATOR` | backend field | deployment | `` | the accelerator TYPE the target offers (`A100`, `T4`); the COUNT is the stage's `gpu` hint, sent as `<type>:<count>`. A flow asking for GPUs with this empty is refused |
| `skypilot_use_spot` / `CAIRN_SKYPILOT_USE_SPOT` | backend field | deployment | `false` | ask for pre-emptible capacity; a reclaimed machine surfaces as a RETRIABLE failure |
| `CAIRN_SKYPILOT_SETUP` | env | deployment | `pip install 'cairndev[dataflow-engine]'` | override the setup line for a bare VM |
| `CAIRN_SKYPILOT_HOURLY_USD` | env | deployment | — | price used for the pre-launch estimate when the connection declares none |
| `CAIRN_SKY_SDK_FACTORY` | env | dev/test | — | point the SkyPilot seam at a fake; unset in production |

## 5. Governance

Reached only through `dataflow/run` / `ml/run`, so every flow leg passes `enforce_flow_gate` and
the flow-engine door before any record exists. The pre-launch cost estimate is checked against
the leg's budget before `sky launch`; the cluster is torn down on cancel and after the result is
fetched; a receipt (`skypilot.receipt`) is recorded per run. The environment allowlist is core's
engine-neutral set only — no Metaflow configuration and no ambient credential reaches a
provisioned machine (ADR-0134 D3b).

## 6. Failure modes

| Failure | Cause | Effect | Recovered? |
|---|---|---|---|
| Engine not installed | `skypilot` selected without `cairndev[skypilot]` | refused at the launch door, naming the extra | n/a |
| No target | `ml_target`/`dataflow_target` empty | `check_ready()` refusal at the door | n/a |
| `ssh`/`rsync` missing (#2102) | the image carries the extra but not the binaries | `check_ready()` refusal | n/a — install them in the image |
| Infra not enabled | `sky check` does not list the target's infra | `check_ready()` refusal | n/a |
| Pre-emption | a spot machine reclaimed | the leg fails; no preemption recovery yet | GAP |
| Cluster left running after a cancelled or failed run (#2073) | a provisioner with no teardown on the terminal path | every terminal state and `cancel` call `sky down` once and write a receipt | yes |
| Cluster outlives the control plane (#2073) | the process that would tear it down died | autostop/autodown, derived from `budget.max_seconds` + grace (default 120 min) | yes — but it is a backstop; no receipt is written for a cluster the control plane never saw end |
| A run launches over budget (#2073) | no pre-launch cost check existed for a flow spec | `EngineError` naming the estimate, the ceiling and the infra; nothing provisioned | yes |
| A run's cost cannot be priced (#2073) | neither the connection's `hourly_usd` nor `CAIRN_SKYPILOT_HOURLY_USD` is set | launches, and the receipt records `priced: false` | no — deliberate: refusing every unpriced run would make the engine unusable, and the receipt makes the gap visible |
| SkyPilot forgets a cluster it launched (#2073) | its state is a SQLite db under `~/.sky`, which was not on a volume — a control-plane restart lost the cluster records, the SSH node pool and the materialised key | the Helm chart persists `/root/.sky` on the same claim, and REFUSES to render `CAIRN_ML_ENGINE=skypilot` with persistence disabled | yes — but only for the chart; any other deployment shape must mount that path itself, or `sky down` cannot reach a machine that is still billing |
| The door refused every launch on a good install (#2073) | `sky.enabled_clouds` does not exist at the top level — it is `sky.client.sdk.enabled_clouds`, and the door turns any exception into a refusal | the adapter calls the path that exists; verified against skypilot 0.13 in a throwaway venv | yes |
| A GPU request asked for an accelerator named "1" (#2073) | `accelerators` was the COUNT alone; SkyPilot wants `<type>:<count>` and parses a bare count as a device NAME (`{'1': 1}`) | the count comes from the stage hint, the TYPE from `skypilot_accelerator`; a GPU request without a type is refused before launch | yes — and the pack's own test had asserted the broken value |
| The door passed with NOTHING enabled (#2102) | `check_ready` guarded the enabled-infra check with `if enabled and …`, so an empty `sky check` skipped it; seen live in the deployed control plane (`enabled_infra() == []`, verdict ok) | an empty `sky check` is a refusal naming `sky check` and, for an SSH pool, `sky ssh up <pool>`; the membership check always runs | yes |
| A control plane with the extra installed could launch nothing (#2102) | `Dockerfile.engine` is `python:3.13-slim-trixie`, which ships neither `ssh` nor `rsync`; SkyPilot shells out to both for every node it provisions or adopts and `sky ssh up` needs them (asyncssh is pure Python and does not help). Seen live in the chart-installed pod: `command -v ssh` → nothing | the image installs `openssh-client` + `rsync` (with a build-time `command -v` gate), and the door refuses NAMING the missing binary | yes |
| A flow leg on SkyPilot could never hand its product to the next leg (#2102) | `result()` raised: the lifecycle shipped without the artifact fetch-back, so only the live metric stream came home | `_teardown` rsyncs the declared outputs off the cluster BEFORE `sky down` (on cancel too), over SkyPilot's own generated per-cluster ssh config; `result()` reads them through the SSH engine's `fetch_list`/`fetch_dir`/`rewrite_fetched` | yes — but only against fakes; the rsync itself has never met a real cluster |

## 7. Testing

`tests/test_skypilot_engine.py` drives a fake SkyPilot SDK (`CAIRN_SKY_SDK_FACTORY` seam): task
YAML, target resolution, the receipt, teardown, the fetch, and `check_ready()` including the
empty-target refusal. `tests/test_skypilot_env_allowlist.py` pins the allowlist. No test talks
to a real cloud — a live `sky launch` provisions a paid machine.

## 8. History

| Date | Change | Issue |
|---|---|---|
| 2026-09 | SkyPilot as the provisioning engine (ADR-0104 D5), in cairn-operator-dataflow | #2073, #2102 |
| 2026-10-16 | The launch check moves from a core SkyPilot door to `check_ready()` behind the engine-neutral flow-engine door (ADR-0134 D3c) | #2683 |
| 2026-10-16 | Its own optional wheel behind `cairndev[skypilot]` (ADR-0134 D3a) | #2678 |
