"""Command line interface.

  python -m ai_process_audit validate eval/intakes/corner-pharmacy.json
  python -m ai_process_audit map      eval/intakes/corner-pharmacy.json
  python -m ai_process_audit score    eval/intakes/corner-pharmacy.json
  python -m ai_process_audit report   eval/intakes/corner-pharmacy.json --out out/pharmacy
  python -m ai_process_audit rubric

Exit codes: 0 success, 1 a problem with the input, 2 a problem with the rubric.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from .intake.validator import IntakeValidationError, load_intake
from .model.normalize import normalize_intake
from .pipeline import audit_and_report, audit_file
from .processmap.mermaid import render_mermaid
from .processmap.steps import build_process_map
from .scoring.rubric import RubricError, load_rubric
from .scoring.score import AuditResult


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("intake", type=Path, help="path to an intake JSON file")
    parser.add_argument(
        "--rubric",
        type=Path,
        default=None,
        help="path to rubric.md, defaults to the one in the project root",
    )
    parser.add_argument(
        "--judge",
        choices=["stub", "live"],
        default="stub",
        help="which judge to use, defaults to stub, live is not implemented",
    )
    parser.add_argument(
        "--stub-behaviour",
        choices=["heuristic", "fixed"],
        default="heuristic",
        help="how the stub judge behaves",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai_process_audit",
        description="Score business processes for automation potential and write a report.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="check an intake file against the schema")
    validate.add_argument("intake", type=Path, help="path to an intake JSON file")

    mapper = subparsers.add_parser("map", help="print the inferred process maps as Mermaid")
    mapper.add_argument("intake", type=Path, help="path to an intake JSON file")

    score = subparsers.add_parser("score", help="score an intake and print the ranking")
    _add_common(score)
    score.add_argument("--json", action="store_true", help="print full results as JSON")

    report = subparsers.add_parser("report", help="score an intake and write the reports")
    _add_common(report)
    report.add_argument("--out", type=Path, required=True, help="output directory")
    report.add_argument(
        "--date",
        dest="generated_on",
        default=None,
        help="date to stamp on the report as YYYY-MM-DD, defaults to today",
    )

    rubric = subparsers.add_parser("rubric", help="show the rubric the engine would use")
    rubric.add_argument("--rubric", dest="rubric_path", type=Path, default=None)

    return parser


def _result_to_dict(result: AuditResult) -> dict:
    return {
        "intake_id": result.intake.intake_id,
        "business": result.intake.business.display_name,
        "rubric_version": result.rubric.version,
        "rubric_approved": result.rubric.approved,
        "judge_id": result.judge_id,
        "judge_mode": result.judge_mode,
        "opportunities": [
            {
                "rank": item.rank,
                "process_id": item.process.id,
                "process_name": item.process.name,
                "weighted_score": item.weighted_score,
                "band": item.band.id,
                "band_label": item.band.label,
                "band_before_caps": (
                    item.band_before_caps.id if item.band_before_caps else item.band.id
                ),
                "applied_caps": [
                    {
                        "criterion": cap.cap.criterion,
                        "raw_score": cap.raw_score,
                        "band_before": cap.band_before.id,
                        "band_after": cap.band_after.id,
                        "reason": cap.cap.reason,
                    }
                    for cap in item.applied_caps
                ],
                "step_count": item.process_map.step_count,
                "criteria": {
                    criterion.id: {
                        "raw_score": criterion.raw_score,
                        "judge_score": criterion.judge_score,
                        "capped": criterion.was_capped,
                        "effective_score": criterion.effective_score,
                        "weight": criterion.weight,
                        "rationale": criterion.rationale,
                    }
                    for criterion in item.criteria
                },
            }
            for item in result.opportunities
        ],
    }


def _print_ranking(result: AuditResult) -> None:
    print(f"{result.intake.business.display_name} ({result.intake.business.industry})")
    print(
        f"Rubric {result.rubric.version}"
        f"{' [DRAFT, not approved]' if not result.rubric.approved else ''}"
        f" | judge {result.judge_id} in {result.judge_mode} mode"
    )
    print()
    for item in result.opportunities:
        print(f"{item.rank}. {item.process.name}")
        print(f"   {item.band.label} at {item.weighted_score:.2f}")
        print(f"   held back by {item.weakest.label.lower()} at {item.weakest.raw_score}")
        for cap in item.applied_caps:
            print(
                f"   capped down from {cap.band_before.label} because "
                f"{cap.criterion_label.lower()} is {cap.raw_score}"
            )
    if result.judge_mode == "stub":
        print()
        print(
            "Stub judge. These numbers show the pipeline runs. They are not an assessment."
        )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        if args.command == "validate":
            document = load_intake(args.intake)
            intake = normalize_intake(document)
            print(f"{args.intake} is valid.")
            print(
                f"{intake.business.display_name}: {len(intake.processes)} process(es), "
                f"schema {intake.schema_version}."
            )
            for process in intake.processes:
                print(f"  {process.id}: {process.name}")
            return 0

        if args.command == "map":
            intake = normalize_intake(load_intake(args.intake))
            for process in intake.processes:
                process_map = build_process_map(process, intake.business)
                print(f"# {process.name} ({process.id}), {process_map.step_count} steps")
                print(render_mermaid(process_map))
                print()
            return 0

        if args.command == "rubric":
            rubric = load_rubric(args.rubric_path)
            status = "approved" if rubric.approved else "DRAFT, not approved"
            print(f"Rubric {rubric.version} ({status}) from {rubric.source_path}")
            print(f"Scale {rubric.scale_min} to {rubric.scale_max}")
            print()
            for criterion in rubric.criteria:
                direction = "inverted" if criterion.is_inverted else "higher is better"
                print(f"  {criterion.label:<22} weight {criterion.weight:.2f}  ({direction})")
            print()
            for band in rubric.bands:
                print(f"  {band.label:<22} from {band.min_score:.2f}")
            return 0

        if args.command == "score":
            result = audit_file(
                args.intake,
                judge_mode=args.judge,
                stub_behaviour=args.stub_behaviour,
                rubric_path=args.rubric,
            )
            if args.json:
                print(json.dumps(_result_to_dict(result), indent=2))
            else:
                _print_ranking(result)
            return 0

        if args.command == "report":
            generated_on = (
                date.fromisoformat(args.generated_on) if args.generated_on else None
            )
            result, paths = audit_and_report(
                args.intake,
                output_dir=args.out,
                judge_mode=args.judge,
                stub_behaviour=args.stub_behaviour,
                rubric_path=args.rubric,
                generated_on=generated_on,
            )
            _print_ranking(result)
            print()
            print(f"Markdown: {paths.markdown}")
            print(f"HTML:     {paths.html}")
            if paths.pdf is not None:
                note = (
                    " (rendered by headless Edge, so it has no page footer)"
                    if paths.pdf_renderer == "edge"
                    else ""
                )
                print(f"PDF:      {paths.pdf}{note}")
            else:
                print("PDF:      not written.")
                print(f"          {paths.pdf_error}")
            if paths.mermaid:
                print(f"Maps:     {len(paths.mermaid)} file(s) in {paths.mermaid[0].parent}")
            return 0

    except IntakeValidationError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except RubricError as exc:
        print(f"Rubric problem: {exc}", file=sys.stderr)
        return 2
    except (NotImplementedError, RuntimeError) as exc:
        print(f"{exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
