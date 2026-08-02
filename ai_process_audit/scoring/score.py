"""Combine judge scores into a ranked list of opportunities.

Deterministic. No model is consulted here.

Everything in this module is arithmetic over the numbers the judge returned. The
separation matters: if the ranking is wrong, it is wrong here and can be fixed by
reading the code, and if a score is wrong, it is wrong in the judge and shows up as
a rationale a reader can disagree with.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..model.models import Intake, Process
from ..processmap.steps import ProcessMap, build_process_map
from .judge import CriterionScore, Judge, JudgeVerdict, get_judge
from .rubric import (
    AppliedCap,
    AppliedCriterionCap,
    Band,
    CriterionCap,
    Rubric,
    load_rubric,
)

# How each criterion cap condition is checked against a process. The rubric may only
# name a condition that appears here, which is enforced when the rubric loads.
CAP_CONDITION_CHECKS = {
    "no_baseline_metric": lambda process: not process.has_baseline,
}


@dataclass(frozen=True)
class ScoredCriterion:
    """One criterion after any cap, the rubric direction, and the weight."""

    id: str
    label: str
    raw_score: int
    effective_score: float
    weight: float
    inverted: bool
    rationale: str
    # What the judge said before a criterion cap lowered it. Equal to raw_score when
    # no cap applied. Kept so a report can show the score the cap overrode.
    judge_score: int | None = None
    cap: AppliedCriterionCap | None = None

    @property
    def contribution(self) -> float:
        return self.effective_score * self.weight

    @property
    def was_capped(self) -> bool:
        return self.cap is not None


@dataclass(frozen=True)
class Opportunity:
    """One process, scored and placed in a band."""

    process: Process
    process_map: ProcessMap
    verdict: JudgeVerdict
    criteria: tuple[ScoredCriterion, ...]
    weighted_score: float
    band: Band
    rank: int = 0
    # The band the weighted score earned before any cap. Equal to band when no cap
    # applied. Kept so the report can show the number a cap overrode rather than
    # quietly presenting the capped result as what the arithmetic said.
    band_before_caps: Band | None = None
    applied_caps: tuple[AppliedCap, ...] = ()

    @property
    def was_capped(self) -> bool:
        return bool(self.applied_caps)

    @property
    def capped_criteria(self) -> tuple[ScoredCriterion, ...]:
        """Criteria whose score was lowered by a criterion cap."""
        return tuple(item for item in self.criteria if item.was_capped)

    @property
    def strongest(self) -> ScoredCriterion:
        """The criterion adding most to the case for automating this."""
        return max(self.criteria, key=lambda item: (item.contribution, item.id))

    @property
    def weakest(self) -> ScoredCriterion:
        """The criterion holding this back the most, relative to a perfect score."""
        return min(self.criteria, key=lambda item: (item.effective_score, item.id))

    def criterion(self, criterion_id: str) -> ScoredCriterion:
        for item in self.criteria:
            if item.id == criterion_id:
                return item
        raise KeyError(criterion_id)


@dataclass(frozen=True)
class AuditResult:
    """The full output of one run of the engine."""

    intake: Intake
    rubric: Rubric
    opportunities: tuple[Opportunity, ...]
    judge_id: str
    judge_mode: str

    @property
    def rubric_is_approved(self) -> bool:
        return self.rubric.approved


def _applies_to(cap: CriterionCap, process: Process) -> bool:
    check = CAP_CONDITION_CHECKS.get(cap.condition)
    if check is None:
        # Unreachable through load_rubric, which rejects unknown conditions, but a
        # rubric built in code could still get here. Fail loudly rather than ignore.
        raise ValueError(f"No check defined for cap condition {cap.condition!r}")
    return check(process)


def _apply_criterion_caps(
    criterion_id: str,
    criterion_label: str,
    judge_score: int,
    process: Process,
    rubric: Rubric,
) -> tuple[int, AppliedCriterionCap | None]:
    """Lower one criterion score where a cap applies.

    A cap only ever lowers. Where several apply to the same criterion, the lowest
    ceiling wins.
    """
    final = judge_score
    applied: AppliedCriterionCap | None = None
    for cap in rubric.criterion_caps:
        if cap.criterion != criterion_id or not _applies_to(cap, process):
            continue
        if final <= cap.max_score:
            continue  # already at or below the ceiling, so the cap changes nothing
        applied = AppliedCriterionCap(
            cap=cap,
            criterion_label=criterion_label,
            score_before=final,
            score_after=cap.max_score,
        )
        final = cap.max_score
    return final, applied


def score_process(
    process: Process,
    process_map: ProcessMap,
    verdict: JudgeVerdict,
    rubric: Rubric,
) -> Opportunity:
    """Turn one verdict into a weighted score and a band."""
    if verdict.rubric_version != rubric.version:
        raise ValueError(
            f"Verdict for {process.id} was produced against rubric {verdict.rubric_version} "
            f"but is being scored against rubric {rubric.version}. Rerun the judge."
        )

    scored: list[ScoredCriterion] = []
    for criterion in rubric.criteria:
        result: CriterionScore = verdict.score_for(criterion.id)
        final_score, applied = _apply_criterion_caps(
            criterion.id, criterion.label, result.score, process, rubric
        )
        scored.append(
            ScoredCriterion(
                id=criterion.id,
                label=criterion.label,
                raw_score=final_score,
                effective_score=rubric.effective_score(criterion.id, final_score),
                weight=criterion.weight,
                inverted=criterion.is_inverted,
                rationale=result.rationale,
                judge_score=result.score,
                cap=applied,
            )
        )

    weighted = round(sum(item.contribution for item in scored), 4)
    earned_band = rubric.band_for(weighted)
    final_band, applied_caps = rubric.apply_caps(
        earned_band, {item.id: item.raw_score for item in scored}
    )
    return Opportunity(
        process=process,
        process_map=process_map,
        verdict=verdict,
        criteria=tuple(scored),
        weighted_score=weighted,
        band=final_band,
        band_before_caps=earned_band,
        applied_caps=applied_caps,
    )


def rank(opportunities: list[Opportunity]) -> tuple[Opportunity, ...]:
    """Order opportunities best first.

    Ordering is by weighted score, not by capped band. A cap limits what may be
    recommended, and the rubric is explicit that it does not change the score, so a
    capped process keeps its place in the list and carries its cap notice with it.

    Ties break on process id rather than on input order, so that two runs over the
    same intake always produce the same report.
    """
    ordered = sorted(
        opportunities,
        key=lambda item: (-item.weighted_score, item.process.id),
    )
    return tuple(
        Opportunity(
            process=item.process,
            process_map=item.process_map,
            verdict=item.verdict,
            criteria=item.criteria,
            weighted_score=item.weighted_score,
            band=item.band,
            rank=position,
            band_before_caps=item.band_before_caps,
            applied_caps=item.applied_caps,
        )
        for position, item in enumerate(ordered, start=1)
    )


def score_intake(
    intake: Intake,
    judge: Judge | None = None,
    rubric: Rubric | None = None,
) -> AuditResult:
    """Run the scoring stage over a normalised intake."""
    rubric = rubric if rubric is not None else load_rubric()
    judge = judge if judge is not None else get_judge("stub")

    opportunities: list[Opportunity] = []
    for process in intake.processes:
        process_map = build_process_map(process, intake.business)
        verdict = judge.judge(process, process_map, rubric)
        opportunities.append(score_process(process, process_map, verdict, rubric))

    return AuditResult(
        intake=intake,
        rubric=rubric,
        opportunities=rank(opportunities),
        judge_id=judge.judge_id,
        judge_mode=judge.mode,
    )
