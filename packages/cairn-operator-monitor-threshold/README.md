# cairn-operator-monitor-threshold

Zero-dependency `monitor/threshold` detector — the generic **data-shaped trigger**
primitive (ADR-0076 D4). Reads a numeric metric from a pluggable source (inline
value, a JSON file + key, or an HTTP GET + key) and compares it to a threshold.

`read_only`: it observes a signal; it never mutates. The *watch* (fire a proposed
run past threshold) is human-gated at the template level — a scheduled skill runs
this detector and, when the threshold is crossed, pauses for approval.

Vendor drift detectors (Evidently, NannyML, whylogs) plug the same
`monitor.threshold` capability as official packs + extras; this is the builtin
default so the base install can watch with no vendor SDK.

## Shared contracts (#2242 V1-E)

The three operators implement `contract://monitor.threshold/1.0.0`,
`monitor.propose/1.0.0` and `monitor.dispatch/1.0.0` from `cairn-contracts-monitor`
(ADR-0111), on the class and in `pack.toml`. None has a `config_schema`, so each
configuration is an empty closed object, and all three are `ReportSpec.NONE`, so
no report definition. `monitor.threshold`'s output `source` is the closed set the
code emits (`inline | file | http`, and `mock` under mock mode); `comparator` stays
a string because mock mode echoes the input. The `watch` flow and the
`watch-source` pipeline keep naming the operators and run under these contracts:
`tests/core/test_pack_edges_operator_contracts.py`.
