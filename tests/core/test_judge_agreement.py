"""Measured judge trust (#2024, ADR-0102 D3) — the statistics, the record, the
storage and the rule.

The τ-b and MAE cases below are hand-computed in the test body itself (the
concordant / discordant / tied pair counts are written out), because the whole
point of implementing τ-b rather than importing it is that the implementation is
checkable — ``scipy`` is not a dependency of this project and this ticket was
not going to add a numerical stack to the control plane.
"""
from __future__ import annotations

import pytest

from cairn.core.judge_agreement import (
    DEFAULT_N_MIN,
    DEFAULT_TAU_MIN,
    JudgeAgreement,
    JudgeTrust,
    TrustThreshold,
    assess_promotion_evidence,
    assess_trust,
    compute_agreement,
    evaluate_judge_against_golden,
    kendall_tau_b,
    mean_absolute_error,
    parse_threshold,
    to_ordinal,
)
from cairn.core.storage.judges import (
    InMemoryJudgeStore,
    JudgeDefinition,
    SqliteJudgeStore,
)

# ── Kendall's τ-b, by hand ───────────────────────────────────────────────────


def test_perfect_agreement_is_one_and_perfect_reversal_is_minus_one():
    assert kendall_tau_b([1, 2, 3, 4], [1, 2, 3, 4]) == 1.0
    assert kendall_tau_b([1, 2, 3, 4], [4, 3, 2, 1]) == -1.0


def test_tau_b_with_ties_hand_computed():
    """xs=[1,1,0,0], ys=[1,0,1,0] — the tie case τ-a gets wrong.

    Six pairs: (0,1) ties in x · (0,2) ties in y · (0,3) concordant ·
    (1,2) discordant · (1,3) ties in y · (2,3) ties in x.
    C=1, D=1, n1=2, n2=2, n0=6 ⇒ τ_b = (1−1)/sqrt(4·4) = 0.0.

    τ-a would divide by n0=6 and still give 0 here, but the denominators differ
    the moment C≠D — which is the next case.
    """
    assert kendall_tau_b([1, 1, 0, 0], [1, 0, 1, 0]) == 0.0


def test_tau_b_on_an_ordinal_three_label_sample_hand_computed():
    """judge=[1, 1, .5, 0] vs human=[1, .5, .5, 0] — one label apart on one row.

    Pairs: (0,1) ties in judge · (0,2) C · (0,3) C · (1,2) ties in human ·
    (1,3) C · (2,3) C ⇒ C=4, D=0, n1=1, n2=1, n0=6,
    τ_b = 4 / sqrt(5·5) = 0.8. MAE = (0+.5+0+0)/4 = 0.125.
    """
    judge = [1.0, 1.0, 0.5, 0.0]
    human = [1.0, 0.5, 0.5, 0.0]
    assert kendall_tau_b(judge, human) == 0.8
    assert mean_absolute_error(judge, human) == 0.125


def test_tau_is_undefined_rather_than_zero_when_a_side_never_varies():
    """A judge that said 'correct' to everything has not ordered anything.

    ``None`` and 0.0 are different claims: 0.0 says "measured, and it tracks
    nothing", ``None`` says "there is nothing here to measure". Reporting the
    first would let a constant judge look merely mediocre.
    """
    assert kendall_tau_b([1, 1, 1, 1], [1, 0, 1, 0]) is None
    assert kendall_tau_b([1], [1]) is None
    assert mean_absolute_error([], []) is None


# ── the ordinal mapping ──────────────────────────────────────────────────────


def test_labels_and_numbers_map_onto_one_scale():
    assert to_ordinal("correct") == 1.0
    assert to_ordinal("Hallucinated") == 0.0
    assert to_ordinal("abstain") == 0.5
    assert to_ordinal(4) == 4.0
    assert to_ordinal("0.75") == 0.75


def test_a_non_verdict_is_dropped_not_guessed():
    for value in ("", None, "error", "banana"):
        assert to_ordinal(value) is None


def test_unmappable_rows_are_counted_not_silently_lost():
    a = compute_agreement([("correct", "correct"), ("error", "correct"), ("x", "y")])
    assert a.n == 1 and a.rows_skipped == 2


# ── the record ───────────────────────────────────────────────────────────────


def test_an_agreement_goes_stale_on_a_judge_edit_or_a_new_golden_version():
    a = JudgeAgreement(judge_name="j", judge_version=2, prompt_hash="h1",
                       golden_ref="facts@3", n=40, tau=0.8)
    assert a.is_stale(head_prompt_hash="h1", latest_golden_ref="facts@3") is False
    assert a.is_stale(head_prompt_hash="h2", latest_golden_ref="facts@3") is True
    assert a.is_stale(head_prompt_hash="h1", latest_golden_ref="facts@4") is True


# ── the threshold ────────────────────────────────────────────────────────────


def test_the_threshold_defaults_to_the_adr_values_and_never_to_none():
    assert parse_threshold(None) == TrustThreshold(tau_min=DEFAULT_TAU_MIN, n_min=DEFAULT_N_MIN)
    assert parse_threshold({"tau_min": 0.7, "n_min": 50}) == TrustThreshold(tau_min=0.7, n_min=50)
    # A malformed variable falls back to the DEFAULT, never to "no bar" — a typo
    # in Settings must not silently trust every judge in the workspace.
    assert parse_threshold({"tau_min": "quite high"}) == TrustThreshold()
    assert parse_threshold("nonsense") == TrustThreshold()


# ── the trust verdict ────────────────────────────────────────────────────────


def _agreement(tau, n, *, ref="facts@1", version=1, prompt_hash="h1") -> JudgeAgreement:
    return JudgeAgreement(judge_name="j", judge_version=version, golden_ref=ref, n=n, tau=tau,
                          prompt_hash=prompt_hash)


def test_a_judge_with_no_golden_evaluation_is_untrusted():
    t = assess_trust([], judge_name="j", judge_version=1)
    assert t.trusted is False and t.measured is False
    assert "no golden evaluation" in t.reason


def test_tau_08_n_40_is_trusted():
    t = assess_trust([_agreement(0.8, 40)], judge_name="j", judge_version=1)
    assert t.status == "trusted"
    assert t.tau == 0.8 and t.n == 40
    assert "never satisfy one alone" in t.reason


def test_tau_09_n_10_is_untrusted_on_the_sample_size_alone():
    t = assess_trust([_agreement(0.9, 10)], judge_name="j", judge_version=1)
    assert t.status == "untrusted"
    assert "n=10 < 30" in t.reason


def test_tau_02_is_untrusted_however_many_rows():
    t = assess_trust([_agreement(0.2, 400)], judge_name="j", judge_version=1)
    assert t.status == "untrusted"
    assert "τ=0.2 < 0.5" in t.reason


def test_one_qualifying_golden_set_is_enough_and_names_itself():
    """Adding a new, harder golden set must not silently demote a judge that
    nothing about had changed — so the verdict is 'some set clears the bar',
    and the set that carries it is named."""
    t = assess_trust([_agreement(0.2, 90, ref="hard@1"), _agreement(0.7, 40, ref="facts@1")],
                     judge_name="j", judge_version=1)
    assert t.status == "trusted" and t.golden_ref == "facts@1"


def test_a_stale_record_is_flagged_not_discarded():
    t = assess_trust([_agreement(0.8, 40)], judge_name="j", judge_version=1,
                     head_prompt_hash="different")
    assert t.stale is True
    assert t.status == "trusted", "flagged for recomputation, not demoted before anyone can act"


# ── the promotion chokepoint ─────────────────────────────────────────────────


def test_an_untrusted_judge_alone_is_refused():
    v = assess_promotion_evidence(judge_score_present=True, trust=JudgeTrust())
    assert v.allowed is False
    assert v.untrusted_metric is True
    assert any("never satisfies a promotion gate on its own" in b for b in v.blocking)


def test_a_human_review_satisfies_what_the_judge_could_not():
    v = assess_promotion_evidence(judge_score_present=True, trust=JudgeTrust(),
                                  human_review=True)
    assert v.allowed is True and "human review" in v.contributing


def test_a_trusted_judge_contributes_but_still_never_alone():
    trust = assess_trust([_agreement(0.8, 40)], judge_name="j", judge_version=1)
    alone = assess_promotion_evidence(judge_score_present=True, trust=trust)
    assert alone.allowed is False, "measuring a judge buys it a seat, never the decision"
    with_metric = assess_promotion_evidence(
        judge_score_present=True, trust=trust, deterministic_evidence=["eval_loss"])
    assert with_metric.allowed is True
    assert any("contributes, never satisfies alone" in c for c in with_metric.contributing)


def test_an_untrusted_judge_does_not_count_as_evidence_even_beside_a_metric():
    v = assess_promotion_evidence(judge_score_present=True, trust=JudgeTrust(),
                                  deterministic_evidence=["eval_loss"])
    assert v.allowed is True
    assert v.contributing == ["eval_loss"], "an untrusted score is not evidence"
    assert any("untrusted_metric" in w for w in v.warnings)


def test_a_promotion_with_no_judge_at_all_is_untouched():
    v = assess_promotion_evidence(judge_score_present=False,
                                  deterministic_evidence=["eval_loss"])
    assert v.allowed is True and v.untrusted_metric is False and v.blocking == []


def test_trust_never_makes_a_judge_score_trusted_evidence():
    """ADR-0095 §2.6 is measured by #2024, not repealed by it."""
    trust = assess_trust([_agreement(0.95, 500)], judge_name="j", judge_version=1)
    assert trust.trusted is True
    assert trust.untrusted_metric is True


# ── storage: memory, sqlite, and the judges that predate this ────────────────


@pytest.mark.parametrize("factory", [InMemoryJudgeStore, SqliteJudgeStore])
async def test_agreements_round_trip_and_a_recompute_replaces_rather_than_doubles(factory):
    store = factory()
    definition = await store.put_judge(JudgeDefinition(name="j", model="m", prompt="p"))
    first = JudgeAgreement(judge_name="j", judge_version=definition.version,
                           golden_ref="facts@1", n=30, tau=0.6)
    await store.put_agreement(first)
    await store.put_agreement(first.model_copy(update={"n": 40, "tau": 0.8}))
    await store.put_agreement(first.model_copy(update={"golden_ref": "hard@1"}))

    rows = await store.list_agreements(judge_name="j")
    assert len(rows) == 2, "keyed by (judge version, golden set) — recompute replaces"
    facts = next(a for a in rows if a.golden_ref == "facts@1")
    assert (facts.n, facts.tau) == (40, 0.8)
    assert await store.list_agreements(judge_name="j", workspace_id="other") == []
    await store.close()


@pytest.mark.parametrize("factory", [InMemoryJudgeStore, SqliteJudgeStore])
async def test_a_judge_that_predates_this_still_loads_and_is_untrusted(factory):
    """The migration case: every judge on main has no agreement record. It must
    still resolve, still score, and be untrusted — not error, and not trusted."""
    store = factory()
    await store.put_judge(JudgeDefinition(name="legacy", model="m", prompt="p"))
    head = await store.get_judge("legacy")
    assert head is not None and head.version == 1
    assert await store.list_agreements(judge_name="legacy") == []
    trust = assess_trust([], judge_name="legacy", judge_version=1)
    assert trust.trusted is False
    await store.close()


# ── running a judge over the golden rows ─────────────────────────────────────


class _FakeLabelStore:
    """The judge-label ledger, which already carries both sides per row."""

    def __init__(self, rows):
        self._rows = rows

    async def list_labels(self, **_):
        return self._rows

    async def close(self):
        return None


def _label(row_id, judge, human, ref="facts@2"):
    from cairn.core.storage.judge_labels import JudgeLabel
    return JudgeLabel(judge_name="j", dataset_ref=ref, row_id=row_id,
                      judge_label=judge, human_label=human)


async def test_the_ledger_supplies_the_judges_side_when_no_scores_are_posted():
    rows = [_label(str(i), "correct" if i % 4 else "hallucinated",
                   "correct" if i % 4 else "hallucinated") for i in range(40)]
    out = await evaluate_judge_against_golden(
        "j", workspace_id="default", judge_version=1,
        golden_refs=["facts@2"], label_store=_FakeLabelStore(rows))
    assert len(out) == 1
    assert out[0].n == 40 and out[0].tau == 1.0 and out[0].mae == 0.0


async def test_posted_scores_override_the_ledgers_copy_of_the_judge():
    """The 'run the judge and post its verdicts' path — the judge executes in an
    operator through the governed gateway, the control plane records what it
    said."""
    rows = [_label(str(i), "correct", "correct" if i < 3 else "hallucinated")
            for i in range(4)]
    out = await evaluate_judge_against_golden(
        "j", workspace_id="default", judge_version=1, golden_refs=["facts@2"],
        scores={"0": "correct", "1": "correct", "2": "correct", "3": "hallucinated"},
        label_store=_FakeLabelStore(rows))
    assert out[0].n == 4 and out[0].tau == 1.0


async def test_labels_match_the_golden_set_by_NAME_across_versions():
    """A human ruling on row 7 of facts@2 is a ruling on that row, not on that
    version number — the same reasoning `assert_golden_rows_rated` uses."""
    rows = [_label(str(i), "correct", "correct", ref="facts@2") for i in range(5)]
    out = await evaluate_judge_against_golden(
        "j", workspace_id="default", judge_version=1, golden_refs=["facts@5"],
        label_store=_FakeLabelStore(rows))
    assert out[0].n == 5 and out[0].golden_ref == "facts@5"
