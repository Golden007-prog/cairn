"""SkillScheduler.list_jobs surfaces the cron schedule (#179 console Scheduled view).

The console "Scheduled" section needs the cron string, not just the next-run
time. list_jobs() reads it from the job's registration args (position 2) — this
guards that indexing against an arg-order change.
"""
from __future__ import annotations

import types

from cairn.core.dispatch.scheduler import SkillScheduler


def test_list_jobs_surfaces_schedule_and_next_run():
    sched = SkillScheduler(runner=types.SimpleNamespace())
    # stand in for apscheduler: a job carrying the registration args
    # (args=[skill_name, spec_name, cron]) + a next_run_time.
    fake_job = types.SimpleNamespace(
        args=["nightly-report", "nightly-report", "0 9 * * *"],
        next_run_time="2026-06-29 09:00:00+00:00",
    )
    sched._scheduler = types.SimpleNamespace(get_job=lambda _id: fake_job)
    sched._jobs = {"nightly-report": "skill:nightly-report"}

    jobs = sched.list_jobs()
    assert len(jobs) == 1
    j = jobs[0]
    assert j["skill_name"] == "nightly-report"
    assert j["schedule"] == "0 9 * * *"
    assert j["next_run_time"] == "2026-06-29 09:00:00+00:00"


def test_list_jobs_tolerates_missing_job():
    sched = SkillScheduler(runner=types.SimpleNamespace())
    sched._scheduler = types.SimpleNamespace(get_job=lambda _id: None)  # job evicted
    sched._jobs = {"gone": "skill:gone"}
    j = sched.list_jobs()[0]
    assert j["skill_name"] == "gone" and j["schedule"] is None and j["next_run_time"] is None
