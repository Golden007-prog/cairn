# cairn-operator-dataflow-metaflow — see DESIGN.md + ADR-0089, ADR-0134 D2.

The Metaflow dataflow engine for cairn: `pip install 'cairndev[metaflow]'`, then set
`[profile.backends] ml_engine = "metaflow"` (or `dataflow_engine`). The `dataflow/run` and
`ml/run` operators of `cairn-operator-dataflow` submit to it; this wheel registers the engine
only.
