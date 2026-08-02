"""Rubric loading, the judge interface, and deterministic score aggregation."""

from .judge import CriterionScore, Judge, JudgeVerdict, StubJudge, get_judge
from .rubric import Criterion, CriterionCap, Rubric, RubricError, load_rubric
from .score import Opportunity, score_intake

__all__ = [
    "Criterion",
    "CriterionCap",
    "CriterionScore",
    "Judge",
    "JudgeVerdict",
    "Opportunity",
    "Rubric",
    "RubricError",
    "StubJudge",
    "get_judge",
    "load_rubric",
    "score_intake",
]
