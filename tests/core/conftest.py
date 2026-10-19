"""Fixtures shared by the trigger-spec and trigger-router tests (#1543).

These two YAMLs used to live in the tree, as
``cairn/packs/incident/triggers/{pagerduty,prometheus-alert}.yaml``, and both
test modules read them from that path. #1543 deleted them: they were not inert
samples — ``TriggerRouter.from_installed()`` mounted them as live HTTP ingress
and a matched alert launched ``incident/incident-rca`` DIRECTLY, around the
``accept-root-cause`` gate that ``pipelines/rca.yaml`` puts in front of the
postmortem.

The router and loader contracts they exercised are still worth pinning, so the
fixtures moved here. They are now what they always claimed to be — canonical
*example* vendor adapters — and no first-party pack ships an ingress that
bypasses its own pipeline.
"""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

PAGERDUTY_YAML = dedent("""
    source: pagerduty
    description: PagerDuty incident webhook (v3 events API)

    ingress:
      transport: http
      path: /triggers/pagerduty
      method: POST
      signature:
        header: X-PagerDuty-Signature
        secret: ${env:PAGERDUTY_WEBHOOK_SECRET}

    normalize:
      event_type: event.event_type
      severity:   event.data.severity
      service:    event.data.service.summary
      alert_id:   event.id
      occurred_at: event.occurred_at
      summary:    event.data.title
      raw:        event.data

    match:
      event_type:
        - incident.triggered
      severity:
        - critical
        - high

    run:
      template: incident/incident-rca
      input:
        symptom:    $event.summary
        service:    $event.service
        started_at: $event.occurred_at
        source:     pagerduty

    example: examples/pagerduty-incident.json
""")

#: Alertmanager fires GROUPS, so this one carries the `fan_out:` block — the
#: only fixture in the suite that exercises one-payload-to-N-events.
PROMETHEUS_YAML = dedent("""
    source: prometheus
    description: Prometheus Alertmanager webhook (v4 schema)

    ingress:
      transport: http
      path: /triggers/prometheus
      method: POST
      signature:
        header: X-Webhook-Signature
        secret: ${env:PROMETHEUS_WEBHOOK_SECRET}
        required: false

    fan_out:
      iterate: alerts

    normalize:
      event_type: status
      severity:   labels.severity
      service:    labels.service
      alert_id:   fingerprint
      occurred_at: startsAt
      summary:    annotations.summary
      raw:        .

    match:
      event_type:
        - firing
      severity:
        - critical
        - high

    run:
      template: incident/incident-rca
      input:
        symptom:    $event.summary
        service:    $event.service
        started_at: $event.occurred_at

    example: examples/kubernetes-latency-alert.json
""")


@pytest.fixture
def vendor_trigger_yamls() -> dict[str, str]:
    """``filename → YAML text`` for the two example vendor adapters."""
    return {
        "pagerduty.yaml": PAGERDUTY_YAML,
        "prometheus-alert.yaml": PROMETHEUS_YAML,
    }


@pytest.fixture
def vendor_triggers_dir(tmp_path: Path, vendor_trigger_yamls) -> Path:
    """A populated ``triggers/`` directory — what a pack shipping them looks like."""
    d = tmp_path / "triggers"
    d.mkdir()
    for name, body in vendor_trigger_yamls.items():
        (d / name).write_text(body, encoding="utf-8")
    return d


@pytest.fixture
def vendor_trigger_specs(vendor_triggers_dir: Path) -> dict:
    """``source → TriggerSpec`` parsed from :func:`vendor_triggers_dir`."""
    from cairn.core.engine.triggers_spec import load_trigger_specs

    return {s.source: s for s in load_trigger_specs(vendor_triggers_dir)}
