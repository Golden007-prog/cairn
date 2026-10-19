# ADR-0104 — Remote deployment is part of cairn: images, a pull worker, a chart, a provisioning engine

- **Status:** ACCEPTED (owner, 2026-09-07)
- **Epic:** #2070 — slices D1 #2071 · D2 #565 · D3 #2072 · D4 #26 · D5 #2073 · D6 #2074
- **Supersedes / amends:** #565 (pull-worker design, now the D2 slice), #26 (Helm chart, un-deferred as D4), the runbook `deploy/prod/RUNBOOK-gpu-finetune-ssh.md` §2 (hand-installed box → image), `deploy/prod/TRAINING-ACCEPTANCE.md` (the job channel is wired since #2025)
- **Relates to:** ADR-0043 (job channel), ADR-0081 (dataflow engines), ADR-0089 (Metaflow substrate on Kubernetes), ADR-0097 (`compute/acquire` behind an authorization gate), ADR-0099 (services per pipeline, the door), ADR-0102 D4 (live metrics over the job channel), #1793 (the no-bolt-on rule)

## Context

On 2026-09-07 a GPU training run could not start, and every reason was a deployment reason, not a training one: the GPU box's hand-installed virtualenv had drifted since August and no longer carried the flow kernel the fine-tune flow now requires; the control plane's `gpu` connection had no host; and the environment's ML target was empty, so ML work ran in-process on the control-plane host. The proven GPU path (`training/finetune-remote`, SSH, `cairn-train-local`) works, but it is a **custom script on a box the control plane pushes into**, and the owner's ruling is that a governed run needs zero manual scripts (#1793) and that deployment must be part of cairn.

A survey of how comparable platforms do it (Prefect, Dagster+, Weights & Biases Launch, Flyte, Airflow, SkyPilot, dstack — sources in the epic) converges on two properties that cairn lacks today:

1. **Nobody hand-installs the framework on the execution machine.** Code reaches the remote as a container image or a declarative pull step; the machine carries no install that can drift.
2. **The execution machine dials out.** Workers and agents poll the control plane; no ingress and no central SSH key. Dagster states it plainly: no ingress is required from the control plane into the customer's environment.

Cairn already owns most of the machinery these platforms are built from, and the upstream check (rule 1) says so: Temporal task queues with `cairn worker --remote` polling a dedicated GPU queue and remote nodes routed to it (#565); the durable remote-job store with a claim-and-reconcile loop; the job channel with idempotent ingest, spool and replay, and control down (#2025); `compute/acquire` / `release` with a lease sweep (ADR-0097); a Metaflow engine that already places per-stage GPU requests on Kubernetes or Batch (ADR-0089); the GPU trainer image built and published by CI; `deploy doctor`; a compose stack with profiles. What is missing is the **pull direction under the runtime prod actually runs**, the **image as the unit of remote work**, a **chart**, and a **provisioning engine**.

## Decision

Deployment is a first-class cairn capability with six rungs. Each rung is an adapter over what exists, never a second orchestrator, queue or agent protocol.

### D1 — The image is the unit of remote work (option 1)

The SSH path runs the CI-built `cairn-train-gpu` image on the box instead of a virtualenv. A box carries drivers and a container runtime and nothing of cairn's that can drift. The `gpu` connection carries the host; the environment's `ml_target` names it. This is the smallest rung and the one that removes the failure class that blocked today.

### D2 — A machine joins by dialling out (option 2)

A worker anywhere runs one command with a control-plane URL and a per-run token (`live_channel_env_for` already mints it): it **claims** work, runs the image, and reports over the job channel. No inbound access, no key held centrally. Under the Temporal runtime this is the existing remote task queue and `cairn worker --remote`. Under the DBOS runtime prod runs, the job store gains one HTTP **claim** endpoint with a lease and heartbeat, and `cairn worker --pull` claims from it; the reporting half is the job channel from #2025. The pull worker is declared as a service the launch door can refuse on (ADR-0099 §1: derived from the environment's target, never a manifest field).

### D3 — Kubernetes for the workloads through the Metaflow engine (option 3)

When a cluster with GPU nodes exists, the environment sets the ML engine to Metaflow with Kubernetes compute, an S3 datastore and the published image; the fine-tune flow's per-stage GPU request becomes the pod's. Nothing new is built; the GPU leg gets its first live proof. Cluster provisioning is infrastructure, not cairn code.

### D4 — Cairn ships as a chart with a public image (option 4)

A Helm chart for the control plane (server, worker, Postgres or an external DSN, ingress in place of Caddy, secrets, the runtime and observability profiles as values), published by CI beside a **public** engine image. Compose stays the single-host path; `deploy doctor` learns values files. This un-defers #26.

### D5 — SkyPilot is the provisioning engine (option 5)

A `skypilot` entry in the dataflow-engine registry: the engine protocol's three calls map onto `sky launch`, `sky status` and `sky down`; the flow spec and its resources become the task YAML; the box registers once as an SSH node pool from its `compute.ssh` connection; the Vast and RunPod connections become SkyPilot credentials. It is an optional extra of the dataflow pack, a declared service at the launch door, and cairn's cost estimate and gate run **before** `sky launch`; a cancelled run tears the cluster down and writes the receipt. Once proven it retires the per-provider operators and the EC2 lifecycle it subsumes (#592, #594, `compute/acquire`) — a later cleanup, on record here so nobody rebuilds them.

### D6 — Managed providers stay a fallback (option 6)

`train/runpod`, `train/vast`, `train/modal` remain, image-based, until D5 proves itself; a task-leg pipeline variant reaches them, and one paid run proves a path the runbook still calls unproven.

### Explicitly not chosen

- **CI as the training substrate** (a self-hosted runner on the GPU box). It is a pull agent for free, but it has no human gates, no cost governance and a six-hour ceiling on hosted runners; it would duplicate the governance that is cairn's whole point. The deploy-on-merge action for cairn itself stays.
- **A new orchestrator, queue or agent protocol.** Temporal task queues, the job store and the job channel are those things.
- **KubeRay / Ray Train** before D3 or D4 exists (ADR-0081's later rung).

## Consequences

- The owner's "no custom script" rule becomes enforceable: a run's compute is an engine or a worker the door can name and refuse on, never a box someone prepared by hand.
- Two deployment shapes are supported and only two: a **single host** (compose) and a **cluster** (chart). Both run the same image.
- The runbook and the acceptance checklist are corrected: the box section becomes "run the image", and the checklist's claim that the job channel is unwired is removed (#2025 wired it; prod runs it over HTTPS).
- D2 under DBOS adds one endpoint and one command to core; everything else is adapters in the dataflow pack and deploy files.
- Order: D1 → D2 → D4 (with D3 when a cluster is justified) → D5 → retire what D5 subsumes; D6 in parallel as the fallback. D1 and D6 are the shortest routes to a real GPU run.

## Verification (rule 1)

- LangGraph: not applicable.
- Workflow engine: Temporal task queues + `cairn worker --remote` = the pull-agent pattern, unused on prod (runtime = DBOS, no remote queue configured).
- Ecosystem: `kubernetes`, `skypilot`, `ray`, `paramiko` absent; `metaflow`, `prefect`, `boto3`, `docker` present. SkyPilot needs Python 3.7–3.13, one configured cloud, no controller; existing machines via `sky ssh up` (Debian-based, drivers present).
- This codebase: job channel, job store + reconciler, `compute/acquire`/`release`/sweep, Metaflow k8s/batch engine, `deploy_check`, ship script, four GitHub Actions incl. the GPU image build. Missing: an HTTP claim endpoint, any chart or manifest, a public image.
