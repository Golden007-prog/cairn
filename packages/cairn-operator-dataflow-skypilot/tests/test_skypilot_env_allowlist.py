"""Each remote engine's environment allowlist (ADR-0134 D3b, #2678): core's engine-neutral set
plus only that engine's own prefixes. Before #2678 SkyPilot borrowed the Metaflow engine's set
wholesale, so Metaflow's configuration rode to every provisioned machine."""
from __future__ import annotations

from operator_dataflow_skypilot.engines_skypilot import _ESSENTIAL_ENV as SKYPILOT_ENV


def test_a_provisioned_machine_gets_no_metaflow_configuration():
    for k in ("CAIRN_CONN_DB", "CAIRN_DATAFLOW_SPEC", "CAIRN_JOB_CHANNEL", "PATH"):
        assert k in SKYPILOT_ENV, k
    for k in ("METAFLOW_DATASTORE_SYSROOT_S3", "CAIRN_METAFLOW_PROFILE", "AWS_SECRET_ACCESS_KEY"):
        assert k not in SKYPILOT_ENV, k
