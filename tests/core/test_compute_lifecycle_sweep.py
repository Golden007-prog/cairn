"""`cairn compute sweep` — the max_hours watchdog (#1787 criterion 5, ADR-0097).

``max_hours`` on ``compute/acquire`` stamps a ``cairn:release-after`` deadline on
the INSTANCE; this is the half that acts on it. The point of putting the deadline
on the instance is that the guard no longer depends on the pipeline's control
plane surviving — any host with credentials can enforce every lease, which is
what a cron does with ``--apply``.

It is a WATCHDOG, not instance-side enforcement, and these tests pin the two
properties that keep it honest rather than dangerous:

* it never stops a box cairn did not turn on (the `cairn:managed` filter);
* it never acts on a tag it could not read (an unparseable deadline is left
  alone, not treated as expired).

No AWS: the fake stands in at the ONE seam (`ec2_client`), which is the same seam
the operators use.
"""
from __future__ import annotations

import asyncio
import datetime as dt

from cairn.core import compute_lifecycle as cl


def _inst(iid: str, tags: dict, state: str = "running") -> dict:
    return {"InstanceId": iid, "State": {"Name": state},
            "Tags": [{"Key": k, "Value": v} for k, v in tags.items()]}


class FakeEC2:
    def __init__(self, instances: list[dict]) -> None:
        self.instances = instances
        self.filters: list = []
        self.stopped: list[str] = []

    def describe_instances(self, **kw):
        self.filters = kw.get("Filters") or []
        return {"Reservations": [{"Instances": self.instances}]}

    def stop_instances(self, **kw):
        self.stopped.extend(kw["InstanceIds"])
        return {"StoppingInstances": [{"CurrentState": {"Name": "stopping"}}]}


NOW = dt.datetime(2026, 8, 30, 12, 0, tzinfo=dt.timezone.utc)
PAST = cl.iso(NOW - dt.timedelta(hours=1))
FUTURE = cl.iso(NOW + dt.timedelta(hours=3))


def _sweep(instances: list[dict], *, apply: bool = False):
    fake = FakeEC2(instances)
    rows = asyncio.run(cl.sweep_expired(client=fake, apply=apply, now=NOW))
    return fake, rows


def test_an_expired_lease_is_reported_and_not_stopped_without_apply():
    """Report-only is the default: the shape a human runs first."""
    fake, rows = _sweep([_inst("i-1", {cl.TAG_MANAGED: "cairn",
                                       cl.TAG_RELEASE_AFTER: PAST})])
    assert rows[0]["expired"] is True
    assert rows[0]["stopped"] is False
    assert fake.stopped == []


def test_apply_stops_the_expired_box():
    fake, rows = _sweep([_inst("i-1", {cl.TAG_MANAGED: "cairn",
                                       cl.TAG_RELEASE_AFTER: PAST})], apply=True)
    assert fake.stopped == ["i-1"]
    assert rows[0]["stopped"] is True
    assert rows[0]["state"] == "stopping"


def test_a_live_lease_is_left_alone():
    fake, rows = _sweep([_inst("i-1", {cl.TAG_MANAGED: "cairn",
                                       cl.TAG_RELEASE_AFTER: FUTURE})], apply=True)
    assert fake.stopped == []
    assert rows[0]["expired"] is False
    assert "valid for another" in rows[0]["note"]


def test_an_unreadable_deadline_is_never_treated_as_expired():
    """A tag we cannot parse may be somebody else's. Stopping on a guess is the
    one way a watchdog becomes an outage."""
    fake, rows = _sweep([_inst("i-1", {cl.TAG_MANAGED: "cairn",
                                       cl.TAG_RELEASE_AFTER: "whenever"})], apply=True)
    assert fake.stopped == []
    assert rows[0]["expired"] is False
    assert "no readable" in rows[0]["note"]


def test_the_sweep_is_scoped_to_cairn_managed_running_instances():
    """Scoping happens in the DESCRIBE filter, so a neighbour's box is never
    even considered — not filtered out afterwards by a predicate that could
    drift."""
    fake, _rows = _sweep([])
    names = {f["Name"]: f["Values"] for f in fake.filters}
    assert names[f"tag:{cl.TAG_MANAGED}"] == ["cairn"]
    assert set(names["instance-state-name"]) == set(cl.BILLING_STATES)


def test_one_failing_stop_does_not_end_the_sweep():
    class Flaky(FakeEC2):
        def stop_instances(self, **kw):
            if kw["InstanceIds"] == ["i-1"]:
                raise RuntimeError("throttled")
            return super().stop_instances(**kw)

    fake = Flaky([_inst("i-1", {cl.TAG_MANAGED: "cairn", cl.TAG_RELEASE_AFTER: PAST}),
                  _inst("i-2", {cl.TAG_MANAGED: "cairn", cl.TAG_RELEASE_AFTER: PAST})])
    rows = asyncio.run(cl.sweep_expired(client=fake, apply=True, now=NOW))

    assert fake.stopped == ["i-2"], "the second lease is still enforced"
    assert "stop FAILED" in rows[0]["note"]


def test_parse_iso_accepts_the_z_suffix_and_rejects_junk():
    assert cl.parse_iso("2026-08-30T12:00:00Z") == NOW
    assert cl.parse_iso("2026-08-30T12:00:00+00:00") == NOW
    assert cl.parse_iso("") is None
    assert cl.parse_iso("soon") is None


def test_the_client_factory_seam_is_env_pointed(monkeypatch):
    """The dev/test seam the #1787 live drive used to prove the governance
    wiring with no AWS call. Unset in production."""
    monkeypatch.setenv("CAIRN_EC2_CLIENT_FACTORY",
                       "tests.core.test_compute_lifecycle_sweep:_factory")
    assert cl.ec2_client("eu-central-1") == ("stub", "eu-central-1")


def _factory(region: str):
    return ("stub", region)
