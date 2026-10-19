# Runbooks — real failure modes only

Each runbook documents a failure mode that has actually occurred or is a known, current
shape, with the diagnosis and the fix as the code supports them today. Speculative
runbooks for unbuilt systems are not written here; `/incident` proposes a runbook only
when the failure is real and recurring.

| Runbook | Failure mode | Since |
|---|---|---|
| [pipeline-stuck.md](pipeline-stuck.md) | a pipeline or leg shows `running`/`paused` and does not advance | #958/#976 |
| [deployment-profile.md](deployment-profile.md) | `cairn-server`/`cairn-worker` refuse to start: `CAIRN_PROFILE` unset or unloadable | #2296 |
| [stale-dataset-reference.md](stale-dataset-reference.md) | a leg fails at launch or eight seconds in on a `dataset:` ref | #2171 |
