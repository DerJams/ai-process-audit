"""The pipeline, start to finish.

Deterministic except for the judge, which is stubbed. This module exists so that the
order of stages is written down in one place and can be read in twenty lines.

  validate -> normalise -> map -> score -> rank -> report

There is no loop, no retry, and no stage that decides what to do next. That is the
point of the design, and it is argued for in the README.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from .intake.validator import load_intake, validate_intake
from .model.normalize import normalize_intake
from .report.render import ReportPaths, write_reports
from .scoring.judge import Judge, get_judge
from .scoring.rubric import Rubric, load_rubric
from .scoring.score import AuditResult, score_intake


def audit_document(
    document: dict,
    judge: Judge | None = None,
    rubric: Rubric | None = None,
) -> AuditResult:
    """Run validation, normalisation, mapping, and scoring over a parsed intake."""
    validate_intake(document)
    intake = normalize_intake(document)
    return score_intake(intake, judge=judge, rubric=rubric)


def audit_file(
    intake_path: str | Path,
    judge_mode: str = "stub",
    stub_behaviour: str = "heuristic",
    rubric_path: str | Path | None = None,
) -> AuditResult:
    """Run the engine over an intake file on disk."""
    document = load_intake(intake_path)
    rubric = load_rubric(rubric_path)
    judge = get_judge(judge_mode, behaviour=stub_behaviour)
    intake = normalize_intake(document)
    return score_intake(intake, judge=judge, rubric=rubric)


def audit_and_report(
    intake_path: str | Path,
    output_dir: str | Path,
    judge_mode: str = "stub",
    stub_behaviour: str = "heuristic",
    rubric_path: str | Path | None = None,
    generated_on: date | None = None,
) -> tuple[AuditResult, ReportPaths]:
    """Run the engine and write every report format."""
    result = audit_file(
        intake_path,
        judge_mode=judge_mode,
        stub_behaviour=stub_behaviour,
        rubric_path=rubric_path,
    )
    paths = write_reports(result, output_dir, generated_on=generated_on)
    return result, paths
