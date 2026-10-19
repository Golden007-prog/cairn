# cairn-operator-dataflow — see pack.toml + ADR-0081.

The engines, their factories, the bridges and the `cairn-dataflow-run` entrypoint.
The kernel runner, stage loader, result readers and fetch contract are core since
#2583 (`cairn.core.dataflow.{runner,stage_loader,results,fetch}`, ADR-0129); this
pack re-exports them for one release.
