# cairn-operator-remote

`remote/exec` — run a command (and ship files in/out) on **any SSH box you own**:
raw CPU/GPU compute (#70 Axis-2). Submit launches the command detached, poll reads
the captured exit code (+ pulls outputs and any `metrics.json`), cancel kills it —
on the existing `remote_job` lifecycle. Run a workflow locally, dispatch the heavy
step to a box you control.

- **Connection:** `cairn connect ssh` → a `compute.ssh` connection (host/user/port +
  `known_hosts`); the private key is held in the SecretStore. Key-based auth,
  host-key verified (never disabled).
- **Install:** `pip install 'cairndev[ssh]'` (asyncssh).
- **Foundation for** the NATS remote-worker channel (events-up/control-down), which
  reuses the JetStream substrate.
