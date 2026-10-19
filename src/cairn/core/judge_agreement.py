"""Measured judge trust (ADR-0102 D3, #2024) — how far a judge is from a human
on a **golden** set, and what that entitles its scores to.

ADR-0095 §2.6 made every judge-scored gain `untrusted_metric` as a *blanket*
rule: nothing measured a judge, so nothing could distinguish one that tracks a
human from one that does not. This module makes the rule **measured** without
making it weaker:

``golden`` versions are human-rated by construction (#2020 refuses to register
one whose rows carry no human rating), so they are the only honest benchmark a
judge can be scored against. Over the rows of one golden version we pair the
judge's verdict with the human's, map both onto an ordinal scale, and report:

``tau``   Kendall's τ-b — does the judge ORDER rows the way a human does. Kiln's
          recommendation, and the right statistic here because a judge's labels
          are ordinal, not interval: "correct > partial > hallucinated" is all
          the scale claims, and τ-b handles the ties that a three-label contract
          produces on nearly every row.
``mae``   mean absolute error on the same ordinal scale — how far off it is when
          it is off. τ says "in the right order", MAE says "by how much"; a
          judge can score well on one and badly on the other, so both are shown.
``n``     the number of rows both sides ruled on. Reported beside every figure
          because τ = 0.9 on ten rows is not evidence of anything.

The trust rule (D3), in three states, fail-closed at every edge:

* **no golden evaluation at all → untrusted.** The default, and the state every
  judge on main is in. Not "unknown, so allow" — unmeasured is untrusted.
* **below the workspace threshold (default τ ≥ 0.5, n ≥ 30) → untrusted.** Its
  scores are `untrusted_metric` and cannot even contribute to a gate.
* **above it → trusted.** Its scores may CONTRIBUTE to a promotion gate and
  **never satisfy one alone** — :func:`assess_promotion_evidence` is the
  function that says so, and it says so for a trusted judge too. The human at
  `promote-adapter` is unchanged; measuring a judge buys it a seat at the table,
  never the decision.

Statistics are implemented here rather than wired: scipy is not a dependency of
this project (`import scipy.stats` fails in the venv), τ-b is thirty lines, and
adding a numerical stack to the control plane to avoid writing them is the wrong
trade. :func:`kendall_tau_b` is tested against hand-computed values including
ties in ``tests/core/test_judge_agreement.py``.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Optional, Sequence

from pydantic import BaseModel, Field

from cairn.core._time import utc_now_iso

logger = logging.getLogger("cairn.judge_agreement")

#: Workspace-variable key holding the trust threshold (ADR-0101 D2).
TRUST_VARIABLE = "judge_trust"

#: The defaults ADR-0102 D3 names. A workspace that sets nothing gets these.
DEFAULT_TAU_MIN = 0.5
DEFAULT_N_MIN = 30

#: Trust states. ``UNMEASURED`` is a *reason*, not a third permission level: it
#: is untrusted, and is named separately only so the console can say why.
TRUST_TRUSTED = "trusted"
TRUST_UNTRUSTED = "untrusted"

#: Ordinal scale for the verdict labels cairn's judges actually emit. The
#: numbers are positions on a quality axis, not probabilities — only their ORDER
#: matters to τ, and their SPACING to MAE, which is why they are stated once
#: here rather than invented per call site.
LABEL_SCALE: dict[str, float] = {
    "correct": 1.0,
    "pass": 1.0,
    "yes": 1.0,
    "true": 1.0,
    "good": 1.0,
    "partial": 0.5,
    "partially_correct": 0.5,
    "abstain": 0.5,
    "unsure": 0.5,
    "incorrect": 0.0,
    "hallucinated": 0.0,
    "fail": 0.0,
    "no": 0.0,
    "false": 0.0,
    "bad": 0.0,
}

#: Never a verdict — the grading stage emits it when the judge call itself
#: failed. Excluded from the pairing rather than scored as a disagreement.
ERROR_LABEL = "error"


# ─── the statistics ──────────────────────────────────────────────────────────


def to_ordinal(value: Any) -> Optional[float]:
    """Map one verdict onto the ordinal scale, or ``None`` when it is not a
    verdict at all.

    Numbers pass through (a 1–5 rating or a 0–1 score is already ordinal); label
    strings go through :data:`LABEL_SCALE`; anything else — an empty cell, an
    ``error``, a label nobody declared — returns ``None`` and is dropped from
    the pairing. Dropping is deliberate: inventing a position on the scale for a
    label we do not understand would put a number on the judge's page that no
    one could defend.
    """
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip().lower()
    if not text or text == ERROR_LABEL:
        return None
    if text in LABEL_SCALE:
        return LABEL_SCALE[text]
    try:
        return float(text)
    except ValueError:
        return None


def kendall_tau_b(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    """Kendall's τ-b over paired ordinal observations, or ``None`` when it is
    undefined.

    τ-b rather than τ-a because a three-label judge ties on most pairs, and τ-a
    charges those ties against the judge as if they were disagreements::

        τ_b = (C − D) / sqrt((n0 − n1)(n0 − n2))

    with ``n0 = n(n−1)/2``, ``C``/``D`` the concordant/discordant pairs, and
    ``n1``/``n2`` the tied pairs within ``xs`` and within ``ys``. ``None`` when
    fewer than two pairs exist or when either side is entirely constant — the
    denominator is zero there, and a judge that gave every row the same label
    has not ordered anything, which is different from ordering it wrongly.

    O(n²), which is the right complexity for a golden set: these are hundreds of
    rows, not millions, and the pair loop is exact where a rank-merge is fiddly.
    """
    n = min(len(xs), len(ys))
    if n < 2:
        return None
    concordant = discordant = tied_x = tied_y = 0
    for i in range(n):
        for j in range(i + 1, n):
            dx = xs[i] - xs[j]
            dy = ys[i] - ys[j]
            if dx == 0 and dy == 0:
                tied_x += 1
                tied_y += 1
            elif dx == 0:
                tied_x += 1
            elif dy == 0:
                tied_y += 1
            elif (dx > 0) == (dy > 0):
                concordant += 1
            else:
                discordant += 1
    n0 = n * (n - 1) / 2
    denom = ((n0 - tied_x) * (n0 - tied_y)) ** 0.5
    if denom == 0:
        return None
    return round((concordant - discordant) / denom, 4)


def mean_absolute_error(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    """Mean |judge − human| on the ordinal scale, or ``None`` with no pairs."""
    n = min(len(xs), len(ys))
    if n == 0:
        return None
    return round(sum(abs(xs[i] - ys[i]) for i in range(n)) / n, 4)


# ─── the record ──────────────────────────────────────────────────────────────


class JudgeAgreement(BaseModel):
    """One judge version measured against one golden version.

    Keyed by ``(workspace, judge_name, judge_version, golden_ref)``: judge
    versions are immutable, so a stored agreement is a permanent fact about a
    pair and recomputing it can only confirm it. What goes stale is which pair
    is the CURRENT one — see :meth:`is_stale`.
    """

    workspace_id: str = "default"
    judge_name: str
    judge_version: int = 0
    #: The rubric that was measured. A prompt edit mints a new version with a
    #: new hash, so an old agreement can never be read as evidence about a new
    #: rubric — the same guard `JudgeScorecard.prompt_hash` gives (#1656).
    prompt_hash: str = ""
    #: ``"<dataset name>@<version>"`` — the golden version this measured.
    golden_ref: str = ""
    #: Rows both sides ruled on. The denominator behind τ and MAE.
    n: int = 0
    #: Rows in the golden set that could not be paired (unrated, judge errored,
    #: an undeclared label). Shown so a small ``n`` is explainable.
    rows_skipped: int = 0
    tau: Optional[float] = None
    mae: Optional[float] = None
    computed_at: str = Field(default_factory=utc_now_iso)
    computed_by: str = ""

    def key(self) -> str:
        return (f"{self.workspace_id or 'default'}:{self.judge_name}:"
                f"{self.judge_version}:{self.golden_ref}")

    def is_stale(self, *, head_prompt_hash: str = "", latest_golden_ref: str = "") -> bool:
        """True when this record no longer describes the CURRENT pair.

        Two ways that happens, both named by D3 ("re-evaluate on judge version
        change or golden version change"): the judge's head has moved past the
        version measured here, or a newer version of this golden set exists.
        A stale flag is enough — recomputation is on demand, because it costs
        judge calls and nobody should pay for them on a page load.
        """
        if head_prompt_hash and self.prompt_hash and head_prompt_hash != self.prompt_hash:
            return True
        return bool(latest_golden_ref and self.golden_ref and latest_golden_ref != self.golden_ref)


def compute_agreement(
    pairs: Iterable[tuple[Any, Any]],
    *,
    judge_name: str = "",
    judge_version: int = 0,
    prompt_hash: str = "",
    golden_ref: str = "",
    workspace_id: str = "default",
    computed_by: str = "",
) -> JudgeAgreement:
    """Score one judge against one golden set from ``(judge, human)`` verdicts.

    ``pairs`` are already matched BY ROW ID by the caller — the pairing is the
    caller's business because the row identity differs by source (a registry
    version's row ids, a measure run's answers file), while the statistic does
    not. A pair either side of which is not a verdict is skipped and counted.
    """
    js: list[float] = []
    hs: list[float] = []
    skipped = 0
    for judge_value, human_value in pairs:
        j = to_ordinal(judge_value)
        h = to_ordinal(human_value)
        if j is None or h is None:
            skipped += 1
            continue
        js.append(j)
        hs.append(h)
    return JudgeAgreement(
        workspace_id=workspace_id, judge_name=judge_name, judge_version=judge_version,
        prompt_hash=prompt_hash, golden_ref=golden_ref,
        n=len(js), rows_skipped=skipped,
        tau=kendall_tau_b(js, hs), mae=mean_absolute_error(js, hs),
        computed_by=computed_by,
    )


# ─── the threshold ───────────────────────────────────────────────────────────


class TrustThreshold(BaseModel):
    """``judge_trust`` from workspace variables, with D3's defaults."""

    tau_min: float = DEFAULT_TAU_MIN
    n_min: int = DEFAULT_N_MIN


def parse_threshold(value: Any) -> TrustThreshold:
    """Read a ``judge_trust`` variable defensively.

    Workspace variables are free-form JSON a person edits in Settings, so a
    malformed value must fall back to the defaults rather than 500 a page — but
    it must not silently WEAKEN the rule either, which is why the fallback is
    D3's default and never "no threshold".
    """
    if not isinstance(value, dict):
        return TrustThreshold()
    out = TrustThreshold()
    try:
        if value.get("tau_min") is not None:
            out.tau_min = float(value["tau_min"])
        if value.get("n_min") is not None:
            out.n_min = int(value["n_min"])
    except (TypeError, ValueError):
        logger.warning("malformed %s workspace variable %r — using D3 defaults",
                       TRUST_VARIABLE, value)
        return TrustThreshold()
    return out


async def read_threshold(workspace_id: str, *, store: Any = None) -> TrustThreshold:
    """The workspace's threshold, or D3's defaults when none is set."""
    from cairn.core.workspace_variables import variables_map  # noqa: PLC0415 — cycle: workspace_variables imports settings/storage

    try:
        variables = await variables_map(workspace_id, store=store)
    except Exception:  # noqa: BLE001 — an unreachable variable store must not weaken the rule
        logger.debug("workspace variables unavailable — using D3 defaults", exc_info=True)
        return TrustThreshold()
    return parse_threshold(variables.get(TRUST_VARIABLE))


# ─── the trust verdict ───────────────────────────────────────────────────────


class JudgeTrust(BaseModel):
    """What a judge's scores are entitled to, and why — the badge's payload."""

    judge_name: str = ""
    judge_version: int = 0
    status: str = TRUST_UNTRUSTED
    #: The best agreement backing this verdict (largest ``n`` at or above the
    #: τ bar; the largest ``n`` overall when nothing clears it).
    tau: Optional[float] = None
    mae: Optional[float] = None
    n: int = 0
    golden_ref: str = ""
    measured: bool = False
    stale: bool = False
    tau_min: float = DEFAULT_TAU_MIN
    n_min: int = DEFAULT_N_MIN
    reason: str = ""

    @property
    def trusted(self) -> bool:
        return self.status == TRUST_TRUSTED

    @property
    def untrusted_metric(self) -> bool:
        """True for EVERY judge-derived score — trusted or not.

        ADR-0095 §2.6 is not repealed by measuring a judge: a trusted judge's
        score is still a judge's opinion, still stamped `untrusted_metric`, and
        still cannot satisfy a promotion gate on its own. Trust changes whether
        it may CONTRIBUTE, not whether it is trusted evidence.
        """
        return True


def assess_trust(
    agreements: Iterable[JudgeAgreement],
    *,
    threshold: Optional[TrustThreshold] = None,
    judge_name: str = "",
    judge_version: int = 0,
    head_prompt_hash: str = "",
    latest_golden_refs: Optional[dict[str, str]] = None,
) -> JudgeTrust:
    """The trust verdict for one judge version from its agreement records.

    A judge is trusted when **some** golden set clears both bars — one golden
    set the judge tracks humans on is evidence it can judge that kind of row,
    and requiring every set to clear would let adding a new, harder golden set
    silently demote a judge that nothing about had changed. The set that carries
    the verdict is named in ``golden_ref`` so the claim is checkable.

    Stale records still count toward the numbers but set ``stale``: refusing to
    read them would demote a judge the moment its golden set gained a version,
    before anyone had a chance to recompute — a fail-closed that punishes the
    person doing the right thing. The flag is the honest report.
    """
    th = threshold or TrustThreshold()
    latest = latest_golden_refs or {}
    rows = [a for a in agreements
            if not judge_name or (a.judge_name == judge_name
                                  and (not judge_version or a.judge_version == judge_version))]
    base = JudgeTrust(judge_name=judge_name, judge_version=judge_version,
                      tau_min=th.tau_min, n_min=th.n_min)
    if not rows:
        base.reason = ("no golden evaluation — a judge is untrusted until it has been "
                       "measured against a human-rated golden set (ADR-0102 D3)")
        return base

    def _stale(a: JudgeAgreement) -> bool:
        name = a.golden_ref.split("@")[0]
        return a.is_stale(head_prompt_hash=head_prompt_hash,
                          latest_golden_ref=latest.get(name, ""))

    qualifying = [a for a in rows
                  if a.tau is not None and a.tau >= th.tau_min and a.n >= th.n_min]
    best = max(qualifying or rows, key=lambda a: a.n)
    base.measured = True
    base.tau, base.mae, base.n = best.tau, best.mae, best.n
    base.golden_ref = best.golden_ref
    base.stale = _stale(best)
    if qualifying:
        base.status = TRUST_TRUSTED
        base.reason = (f"τ={best.tau} with n={best.n} on {best.golden_ref} clears "
                       f"τ ≥ {th.tau_min}, n ≥ {th.n_min} — this judge's score may "
                       "contribute to a gate, never satisfy one alone")
        return base
    tau_text = "undefined" if best.tau is None else str(best.tau)
    misses = []
    if best.tau is None or best.tau < th.tau_min:
        misses.append(f"τ={tau_text} < {th.tau_min}")
    if best.n < th.n_min:
        misses.append(f"n={best.n} < {th.n_min}")
    base.reason = (f"best golden evaluation ({best.golden_ref}) misses the bar: "
                   f"{'; '.join(misses)} — this judge's scores are untrusted_metric")
    return base


# ─── the promotion chokepoint ────────────────────────────────────────────────


class PromotionVerdict(BaseModel):
    """Whether the cited evidence may promote, and what it is missing."""

    allowed: bool = False
    #: True when a judge score is among the evidence — the ADR-0095 §2.6 stamp,
    #: which rides regardless of how well the judge scored.
    untrusted_metric: bool = False
    #: Evidence that counts toward the promote.
    contributing: list[str] = Field(default_factory=list)
    #: Refusals — each one a reason the promote may not proceed.
    blocking: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def assess_promotion_evidence(
    *,
    judge_score_present: bool,
    trust: Optional[JudgeTrust] = None,
    deterministic_evidence: Sequence[str] = (),
    human_review: bool = False,
) -> PromotionVerdict:
    """**The trust rule.** One function, called wherever a promote is decided.

    The three states of D3, and the one invariant that survives all of them:

    * a judge with no golden evaluation, or one below the threshold, is
      untrusted — its score is `untrusted_metric` and does not count as
      evidence at all;
    * a judge above the threshold contributes its score as evidence;
    * **judge-derived evidence never satisfies a promotion gate alone.** Not
      the untrusted one, and not the trusted one either. Without a
      deterministic metric or a human review beside it, the promote is refused.

    ``trust=None`` with a judge score present is the fail-closed default and the
    state every judge on main is in: unmeasured is untrusted, never "unknown, so
    allow". The human at `promote-adapter` is unchanged by any of this; this
    function can only ever refuse a promote, never release one.
    """
    verdict = PromotionVerdict()
    deterministic = [e for e in deterministic_evidence if e]
    verdict.contributing.extend(deterministic)
    if human_review:
        verdict.contributing.append("human review")

    if judge_score_present:
        verdict.untrusted_metric = True
        t = trust or JudgeTrust()
        who = f"judge {t.judge_name}@{t.judge_version}" if t.judge_name else "the judge"
        if t.trusted:
            verdict.contributing.append(
                f"{who} score (τ={t.tau}, n={t.n} on {t.golden_ref}) — contributes, "
                "never satisfies alone")
        else:
            verdict.warnings.append(
                f"{who} is untrusted ({t.reason or 'no golden evaluation'}) — its score is "
                "untrusted_metric and does not count as promotion evidence")
        if not deterministic and not human_review:
            verdict.blocking.append(
                "promotion evidence is judge-derived only — a judge score never satisfies a "
                "promotion gate on its own (ADR-0102 D3, ADR-0095 §2.6). Cite a deterministic "
                "metric (held-out loss, a benchmark bar, a baseline delta) or attach a human "
                "review at the promote gate.")

    verdict.allowed = not verdict.blocking
    return verdict


# ─── running the judge over a golden set ─────────────────────────────────────


async def golden_versions(workspace_id: str, *, artifact_store: Any = None) -> list[dict]:
    """Every ``golden`` registry version in the workspace, newest last.

    Reads the registry's own role index (:meth:`list_dataset_versions_by_role`,
    #2020) rather than a second list of "eval sets" — one place decides what is
    golden, and it is the place that refuses to register an unrated one.
    """
    store = artifact_store
    if store is None:
        from cairn.core.storage.artifacts import build_artifact_store  # noqa: PLC0415 — cycle: artifacts imports dataset_roles
        store = build_artifact_store()
    lister = getattr(store, "list_dataset_versions_by_role", None)
    if lister is None:
        return []
    rows = await lister(["golden"], workspace_id=workspace_id)
    return sorted(rows, key=lambda r: (str(r.get("name") or ""), int(r.get("version") or 0)))


def golden_ref(row: dict) -> str:
    return f"{row.get('name') or ''}@{int(row.get('version') or 0)}"


async def evaluate_judge_against_golden(
    judge_name: str,
    *,
    workspace_id: str,
    judge_version: int = 0,
    prompt_hash: str = "",
    golden_refs: Optional[Sequence[str]] = None,
    scores: Optional[dict[str, Any]] = None,
    label_store: Any = None,
    artifact_store: Any = None,
    computed_by: str = "",
) -> list[JudgeAgreement]:
    """Score a judge version against the workspace's golden sets.

    Two sources of the JUDGE's side of each pair, in order:

    ``scores``
        ``{row_id: verdict}`` posted by the caller — a measure run that has just
        finished grading the golden rows sends its verdicts here. This is the
        "run the judge over the golden rows" path: the judge executes where
        judges execute (in an operator, through the governed gateway — DB =
        config store, operator = execution body), and the control plane records
        what it said. Running an LLM inside an API request would put a paid,
        minutes-long, ungoverned call behind a button, which is the one thing
        #1467's split exists to prevent.

    the judge-label ledger
        with no ``scores``, the judge's verdicts come from
        :mod:`cairn.core.storage.judge_labels`, whose rows already carry BOTH
        sides for the same row id — ``judge_label`` is what the judge said when
        the human ruled. Nothing new has to be run to measure a judge over rows
        a human has already been through.

    The HUMAN's side is always the ledger's ``human_label``: a golden set's
    authority is that a person ruled on every row, so it has exactly one source.
    """
    from cairn.core.storage.judge_labels import build_judge_label_store  # noqa: PLC0415 — cycle: judge_labels imports storage helpers

    store = label_store
    owns_store = store is None
    if owns_store:
        store = build_judge_label_store()
    try:
        labels = await store.list_labels(workspace_id=workspace_id, limit=100_000)
    finally:
        if owns_store:
            closer = getattr(store, "close", None)
            if callable(closer):
                try:
                    await closer()
                except Exception:  # noqa: BLE001 — best-effort cleanup, never fatal
                    logger.debug("judge-label store close failed", exc_info=True)

    wanted = set(golden_refs or [])
    if not wanted:
        wanted = {golden_ref(r) for r in
                  await golden_versions(workspace_id, artifact_store=artifact_store)}

    out: list[JudgeAgreement] = []
    for ref in sorted(wanted):
        # Labels are keyed by the DATASET row (JudgeLabel.key), so a human
        # ruling made against version 2 of a golden set is still a ruling on
        # that row — matched on the dataset NAME, exactly as the golden-set
        # registration guard matches it (`assert_golden_rows_rated`).
        prefix = f"{ref.split('@')[0]}@"
        rows = [lb for lb in labels if str(lb.dataset_ref or "").startswith(prefix)]
        pairs: list[tuple[Any, Any]] = []
        for lb in rows:
            judged = scores.get(lb.row_id) if scores is not None else lb.judge_label
            pairs.append((judged, lb.human_label))
        out.append(compute_agreement(
            pairs, judge_name=judge_name, judge_version=judge_version,
            prompt_hash=prompt_hash, golden_ref=ref, workspace_id=workspace_id,
            computed_by=computed_by))
    return out


__all__ = [
    "DEFAULT_N_MIN",
    "DEFAULT_TAU_MIN",
    "LABEL_SCALE",
    "TRUST_TRUSTED",
    "TRUST_UNTRUSTED",
    "TRUST_VARIABLE",
    "JudgeAgreement",
    "JudgeTrust",
    "PromotionVerdict",
    "TrustThreshold",
    "assess_promotion_evidence",
    "assess_trust",
    "compute_agreement",
    "evaluate_judge_against_golden",
    "golden_ref",
    "golden_versions",
    "kendall_tau_b",
    "mean_absolute_error",
    "parse_threshold",
    "read_threshold",
    "to_ordinal",
]
