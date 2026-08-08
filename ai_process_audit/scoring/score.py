"""Combine judge scores into a ranked list of opportunities.

Deterministic. No model is consulted here.

Everything in this module is arithmetic over the numbers the judge returned. The
separation matters: if the ranking is wrong, it is wrong here and can be fixed by
reading the code, and if a score is wrong, it is wrong in the judge and shows up as
a rationale a reader can disagree with.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..model.models import Intake, Process
from ..processmap.steps import ProcessMap, build_process_map
from .judge import CriterionScore, Judge, JudgeVerdict, get_judge
from .rubric import (
    AppliedCap,
    AppliedCriterionCap,
    Band,
    CriterionCap,
    Disqualification,
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
class Evaluation:
    """The result of applying the rubric to one set of per criterion scores.

    This is the whole of the rubric arithmetic: criterion caps, direction, weights,
    banding, band caps. It is deliberately independent of where the scores came from,
    so the engine and the evaluation harness can put the same numbers through the same
    code. When the harness worked out a gold band with its own arithmetic it silently
    skipped both caps, and the resulting band agreement compared two numbers computed
    under different rules.
    """

    judge_scores: dict[str, int]
    scores: dict[str, int]
    criterion_caps: dict[str, AppliedCriterionCap]
    # None when the process is disqualified. There is deliberately no score and no
    # band in that case, rather than a low one, because a low score still invites the
    # comparison that disqualification exists to prevent.
    weighted_score: float | None
    band_before_caps: Band | None
    band: Band | None
    band_caps: tuple[AppliedCap, ...]
    disqualification: Disqualification | None = None

    @property
    def is_disqualified(self) -> bool:
        return self.disqualification is not None


def evaluate(judge_scores: dict[str, int], process: Process, rubric: Rubric) -> Evaluation:
    """Apply the rubric to a set of scores, whoever produced them.

    judge_scores are the scores as given, before any cap. Every criterion in the
    rubric must be present.
    """
    capped: dict[str, int] = {}
    caps: dict[str, AppliedCriterionCap] = {}
    for criterion in rubric.criteria:
        if criterion.id not in judge_scores:
            raise KeyError(f"No score given for criterion {criterion.id!r}")
        final_score, applied = _apply_criterion_caps(
            criterion.id, criterion.label, int(judge_scores[criterion.id]), process, rubric
        )
        capped[criterion.id] = final_score
        if applied is not None:
            caps[criterion.id] = applied

    # Checked before any arithmetic. A disqualified process gets no weighted score at
    # all, so there is nothing to compute and nothing that could later be mistaken for
    # a ranking position.
    disqualification = rubric.disqualification_for(capped)
    if disqualification is not None:
        return Evaluation(
            judge_scores={key: int(value) for key, value in judge_scores.items()},
            scores=capped,
            criterion_caps=caps,
            weighted_score=None,
            band_before_caps=None,
            band=None,
            band_caps=(),
            disqualification=disqualification,
        )

    weighted = round(
        sum(
            rubric.effective_score(criterion.id, capped[criterion.id]) * criterion.weight
            for criterion in rubric.criteria
        ),
        4,
    )
    earned_band = rubric.band_for(weighted)
    final_band, band_caps = rubric.apply_caps(earned_band, capped)

    return Evaluation(
        judge_scores={key: int(value) for key, value in judge_scores.items()},
        scores=capped,
        criterion_caps=caps,
        weighted_score=weighted,
        band_before_caps=earned_band,
        band=final_band,
        band_caps=band_caps,
    )


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
class Disqualified:
    """A process removed from the ranking rather than scored.

    It carries the same per criterion scores as an Opportunity, so a labeller and the
    failure analysis can still compare every criterion. What it does not have is a
    weighted score, a band, or a rank, because it is not in the running.
    """

    process: Process
    process_map: ProcessMap
    verdict: JudgeVerdict
    criteria: tuple[ScoredCriterion, ...]
    disqualification: Disqualification
    evidence: str

    # Kept so anything iterating over both kinds can ask without a type check.
    weighted_score: None = None
    band: None = None
    rank: int = 0

    def criterion(self, criterion_id: str) -> ScoredCriterion:
        for item in self.criteria:
            if item.id == criterion_id:
                return item
        raise KeyError(criterion_id)

    @property
    def trigger(self) -> ScoredCriterion:
        return self.criterion(self.disqualification.criterion)


@dataclass(frozen=True)
class AuditResult:
    """The full output of one run of the engine."""

    intake: Intake
    rubric: Rubric
    opportunities: tuple[Opportunity, ...]
    judge_id: str
    judge_mode: str
    disqualified: tuple[Disqualified, ...] = ()

    @property
    def rubric_is_approved(self) -> bool:
        return self.rubric.approved

    @property
    def all_processes(self) -> tuple[Opportunity | Disqualified, ...]:
        """Everything scored, ranked first then disqualified."""
        return self.opportunities + self.disqualified


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


def _first_sentence(text: str) -> str:
    """The opening sentence of a description, for quoting back as evidence.

    Used only when a process is disqualified, so the report can say why in the
    business's own words rather than in the engine's.
    """
    cleaned = " ".join(text.split())
    match = re.search(r"^(.+?[.!?])(\s|$)", cleaned)
    sentence = match.group(1) if match else cleaned
    if len(sentence) > 240:
        sentence = sentence[:237].rsplit(" ", 1)[0] + "..."
    return sentence


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

    results: dict[str, CriterionScore] = {
        criterion.id: verdict.score_for(criterion.id) for criterion in rubric.criteria
    }
    evaluation = evaluate(
        {criterion_id: result.score for criterion_id, result in results.items()},
        process,
        rubric,
    )

    scored = tuple(
        ScoredCriterion(
            id=criterion.id,
            label=criterion.label,
            raw_score=evaluation.scores[criterion.id],
            effective_score=rubric.effective_score(
                criterion.id, evaluation.scores[criterion.id]
            ),
            weight=criterion.weight,
            inverted=criterion.is_inverted,
            rationale=results[criterion.id].rationale,
            judge_score=evaluation.judge_scores[criterion.id],
            cap=evaluation.criterion_caps.get(criterion.id),
        )
        for criterion in rubric.criteria
    )

    if evaluation.is_disqualified:
        return Disqualified(
            process=process,
            process_map=process_map,
            verdict=verdict,
            criteria=scored,
            disqualification=evaluation.disqualification,
            evidence=_first_sentence(process.description),
        )

    return Opportunity(
        process=process,
        process_map=process_map,
        verdict=verdict,
        criteria=scored,
        weighted_score=evaluation.weighted_score,
        band=evaluation.band,
        band_before_caps=evaluation.band_before_caps,
        applied_caps=evaluation.band_caps,
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
    disqualified: list[Disqualified] = []
    for process in intake.processes:
        process_map = build_process_map(process, intake.business)
        verdict = judge.judge(process, process_map, rubric)
        outcome = score_process(process, process_map, verdict, rubric)
        if isinstance(outcome, Disqualified):
            disqualified.append(outcome)
        else:
            opportunities.append(outcome)

    return AuditResult(
        intake=intake,
        rubric=rubric,
        opportunities=rank(opportunities),
        judge_id=judge.judge_id,
        judge_mode=judge.mode,
        # Ordered by process id so two runs over the same intake match.
        disqualified=tuple(sorted(disqualified, key=lambda item: item.process.id)),
    )
