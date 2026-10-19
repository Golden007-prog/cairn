# cairn-operator-dataflow-skypilot — see DESIGN.md + ADR-0104 D5, ADR-0134 D3.

The SkyPilot provisioning engine for cairn: `pip install 'cairndev[skypilot]'`, set
`[profile.backends] ml_engine = "skypilot"` (or `dataflow_engine`) and point `ml_target` /
`dataflow_target` at a compute connection. The launch door asks this engine's `check_ready()`
before any record exists — nothing is provisioned to make the check pass.
