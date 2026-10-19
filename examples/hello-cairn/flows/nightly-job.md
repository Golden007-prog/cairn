---
name: nightly-job
version: 0.1.0
module: ops
tags:
  - flow:state_machine
  - flow:operation-driven
  - domain:example
description: |
  The nightly job: check that the job's secret is present, then run a report
  command on an SSH box and fetch what it wrote. The first leg of `nightly-ops`.

flow:
  type: state_machine
  source: behavior_tree
  root:
    type: sequence
    children:
      # A capability node, not an operator: the active profile's
      # [profile.bindings] "secrets.store" picks the implementation
      # (secrets/env in profiles/hello.toml, secrets/infisical in
      # profiles/hello-infisical.toml). A presence check: the value never
      # enters the run's state.
      - type: operation
        capability: contract://secrets.store/1.0.0
        id: secret
        input:
          name: $input.secret_name
        output:
          present: $state.findings.secret_present
      # The SSH job (builtin remote/exec). The box comes from a `compute.ssh`
      # connection named by the run input, never from the template.
      - type: operation
        operator: remote/exec
        id: report
        execution:
          compute_target: $input.compute_target
        input:
          command: $input.command
          env:
            HELLO_SECRET_PRESENT: $state.findings.secret_present
        output:
          status: $state.findings.job_status
          exit_code: $state.findings.job_exit_code
          host: $state.findings.job_host
          metrics: $state.findings.report
          error: $state.findings.job_error
  nodes: {}

tools:
  required: []
---

# Nightly job

Two operation nodes and no model: the secret check runs through whichever
`secrets.store` implementation the profile binds, and the report runs over SSH on
the box the `compute_target` input names. The command writes `metrics.json` in its
working directory; `remote/exec` reads it back as `report`.
