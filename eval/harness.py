"""Agreement harness: compare engine scores against hand written gold labels.

Run it:

    python -m eval.harness
    python -m eval.harness --gold eval/gold/corner-rx-003.gold.json

Two rules are enforced here rather than left to discipline:

1. This harness never writes to a gold file. It opens them read only and has no
   code path that could produce a label. Labels are written by a person, by hand.
2. A gold file records the rubric version it was written against. If the rubric has
   changed since, the comparison is refused rather than silently run, because scores
   from one rubric say nothing about scores from another.

What the numbers mean is described in eval/gold/README.md. The short version: exact
agreement is the honest headline, agreement within one point is the useful one, and
the signed error says which direction the engine leans when it is wrong.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import date
from itertools import combinations
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    # Allow "python eval/harness.py" as well as "python -m eval.harness".
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai_process_audit.intake.validator import load_intake
from ai_process_audit.model.normalize import normalize_intake
from ai_process_audit.scoring.judge import get_judge
from ai_process_audit.scoring.rubric import Rubric, load_rubric
from ai_process_audit.scoring.score import Opportunity, score_intake

EVAL_DIR = Path(__file__).resolve().parent
GOLD_DIR = EVAL_DIR / "gold"
OUTPUT_DIR = EVAL_DIR / "out"

# Agreement within this many points is reported alongside exact agreement. One point
# on a five point scale is the width of an anchor, so it is the difference between
# two people reading the same anchor table slightly differently.
NEAR_MISS_TOLERANCE = 1


class GoldError(Exception):
    """Raised when a gold file cannot be used as it stands."""


@dataclass
class CriterionAgreement:
    """Agreement figures for one criterion across every labelled process."""

    criterion_id: str
    label: str
    labelled: int = 0
    exact: int = 0
    within_tolerance: int = 0
    signed_errors: list[int] = field(default_factory=list)

    @property
    def exact_rate(self) -> float | None:
        return self.exact / self.labelled if self.labelled else None

    @property
    def near_rate(self) -> float | None:
        return self.within_tolerance / self.labelled if self.labelled else None

    @property
    def mean_signed_error(self) -> float | None:
        if not self.signed_errors:
            return None
        return sum(self.signed_errors) / len(self.signed_errors)

    @property
    def mean_absolute_error(self) -> float | None:
        if not self.signed_errors:
            return None
        return sum(abs(error) for error in self.signed_errors) / len(self.signed_errors)

    @property
    def lean(self) -> str:
        mean = self.mean_signed_error
        if mean is None:
            return "no labels"
        if abs(mean) < 0.1:
            return "no consistent lean"
        return "scores higher than the labels" if mean > 0 else "scores lower than the labels"


@dataclass
class Disagreement:
    """One place the engine and the labels differ."""

    intake_id: str
    process_id: str
    process_name: str
    criterion_id: str
    criterion_label: str
    gold_score: int
    engine_score: int
    engine_rationale: str
    gold_note: str | None = None

    @property
    def difference(self) -> int:
        return self.engine_score - self.gold_score


@dataclass
class IntakeComparison:
    """Everything learned from comparing one intake against its labels."""

    intake_id: str
    gold_path: Path
    intake_path: Path
    labelled_processes: int = 0
    unlabelled_criteria: int = 0
    band_matches: int = 0
    band_compared: int = 0
    ranking_pairs_agreed: int = 0
    ranking_pairs_compared: int = 0
    ranking_pairs_tied_in_gold: int = 0
    disagreements: list[Disagreement] = field(default_factory=list)
    process_rows: list[dict[str, Any]] = field(default_factory=list)


def _read_gold(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError as exc:
        raise GoldError(f"{path} is not valid JSON: {exc.msg} at line {exc.lineno}") from None


def _check_gold_shape(gold: dict[str, Any], path: Path, rubric: Rubric) -> None:
    for key in ("intake_file", "rubric_version", "processes"):
        if key not in gold:
            raise GoldError(f"{path} is missing the required key {key!r}")

    if gold["rubric_version"] != rubric.version:
        raise GoldError(
            f"{path} was labelled against rubric {gold['rubric_version']} but the "
            f"current rubric is {rubric.version}. Scores from different rubrics are "
            "not comparable. Either check out the matching rubric or relabel."
        )

    has_any_label = any(
        value is not None
        for entry in gold["processes"].values()
        for value in entry.get("criteria", {}).values()
    )
    if has_any_label and not gold.get("labelled_by"):
        # An untouched template is fine and is reported as unlabelled. A file with
        # scores in it must say whose judgement they are, because that is what the
        # agreement figure is agreement with.
        raise GoldError(
            f"{path} contains labels but has no labelled_by. Every set of labels must "
            "say who wrote it, because the labels are one person's judgement and the "
            "results should say whose."
        )

    known = set(rubric.criterion_ids)
    for process_id, entry in gold["processes"].items():
        unknown = sorted(set(entry.get("criteria", {})) - known)
        if unknown:
            raise GoldError(
                f"{path} labels criteria that are not in rubric {rubric.version} for "
                f"process {process_id}: {', '.join(unknown)}"
            )
        stray = sorted(set(entry.get("rationales", {})) - known)
        if stray:
            raise GoldError(
                f"{path} has rationales for criteria that are not in rubric "
                f"{rubric.version} for process {process_id}: {', '.join(stray)}"
            )


def _gold_weighted_score(
    criteria: dict[str, Any], rubric: Rubric
) -> float | None:
    """Work out what the rubric would say if the labels were the scores.

    This is arithmetic over labels a person wrote. It does not create labels. It
    returns None unless every criterion has been labelled, because a partial set
    would produce a band that looks authoritative and is not.
    """
    total = 0.0
    for criterion in rubric.criteria:
        value = criteria.get(criterion.id)
        if value is None:
            return None
        total += rubric.effective_score(criterion.id, float(value)) * criterion.weight
    return round(total, 4)


def compare_one(gold_path: Path, rubric: Rubric, stub_behaviour: str = "heuristic") -> IntakeComparison:
    """Compare the engine against one gold file."""
    gold = _read_gold(gold_path)
    _check_gold_shape(gold, gold_path, rubric)

    intake_path = (gold_path.parent / gold["intake_file"]).resolve()
    if not intake_path.exists():
        intake_path = (EVAL_DIR / gold["intake_file"]).resolve()
    if not intake_path.exists():
        raise GoldError(f"{gold_path} points at {gold['intake_file']}, which does not exist")

    intake = normalize_intake(load_intake(intake_path))
    result = score_intake(intake, judge=get_judge("stub", behaviour=stub_behaviour), rubric=rubric)
    by_id: dict[str, Opportunity] = {item.process.id: item for item in result.opportunities}

    comparison = IntakeComparison(
        intake_id=intake.intake_id,
        gold_path=gold_path,
        intake_path=intake_path,
    )

    gold_scores: dict[str, float] = {}

    for process_id, entry in gold["processes"].items():
        opportunity = by_id.get(process_id)
        if opportunity is None:
            raise GoldError(
                f"{gold_path} labels a process {process_id!r} that is not in "
                f"{intake_path.name}. Process ids must match exactly."
            )

        labelled_criteria = entry.get("criteria", {})
        rationales = entry.get("rationales", {})
        labelled_any = False
        for criterion in rubric.criteria:
            gold_value = labelled_criteria.get(criterion.id)
            if gold_value is None:
                comparison.unlabelled_criteria += 1
                continue
            labelled_any = True
            engine = opportunity.criterion(criterion.id)
            if engine.raw_score != int(gold_value):
                comparison.disagreements.append(
                    Disagreement(
                        intake_id=intake.intake_id,
                        process_id=process_id,
                        process_name=opportunity.process.name,
                        criterion_id=criterion.id,
                        criterion_label=criterion.label,
                        gold_score=int(gold_value),
                        engine_score=engine.raw_score,
                        engine_rationale=engine.rationale,
                        # The reasoning written for this specific criterion, falling
                        # back to the process level note when none was written.
                        gold_note=(
                            (rationales.get(criterion.id) or "").strip()
                            or (entry.get("notes") or "").strip()
                            or None
                        ),
                    )
                )

        if labelled_any:
            comparison.labelled_processes += 1

        gold_weighted = _gold_weighted_score(labelled_criteria, rubric)
        if gold_weighted is not None:
            gold_scores[process_id] = gold_weighted
            comparison.band_compared += 1
            gold_band = rubric.band_for(gold_weighted)
            if gold_band.id == opportunity.band.id:
                comparison.band_matches += 1

        comparison.process_rows.append(
            {
                "process_id": process_id,
                "process_name": opportunity.process.name,
                "engine_score": opportunity.weighted_score,
                "engine_band": opportunity.band.label,
                "gold_score": gold_weighted,
                "gold_band": rubric.band_for(gold_weighted).label if gold_weighted is not None else None,
                "engine_rank": opportunity.rank,
            }
        )

    # Ranking agreement over every pair of fully labelled processes. This is the
    # metric closest to what the product actually claims, which is an order.
    for left, right in combinations(sorted(gold_scores), 2):
        comparison.ranking_pairs_compared += 1
        gold_delta = gold_scores[left] - gold_scores[right]
        engine_delta = by_id[left].weighted_score - by_id[right].weighted_score
        if gold_delta == 0:
            comparison.ranking_pairs_tied_in_gold += 1
            continue
        if (gold_delta > 0) == (engine_delta > 0):
            comparison.ranking_pairs_agreed += 1

    return comparison


def summarise(
    comparisons: list[IntakeComparison], rubric: Rubric
) -> tuple[dict[str, CriterionAgreement], dict[str, Any]]:
    """Roll individual comparisons up into per criterion and overall figures."""
    per_criterion = {
        criterion.id: CriterionAgreement(criterion.id, criterion.label)
        for criterion in rubric.criteria
    }

    # Every labelled criterion counts once. Disagreements are already recorded, so
    # agreement is derived from the totals rather than counted twice.
    labelled_total = 0
    for comparison in comparisons:
        labelled_here: dict[str, int] = {criterion.id: 0 for criterion in rubric.criteria}
        gold = _read_gold(comparison.gold_path)
        for entry in gold["processes"].values():
            for criterion_id, value in entry.get("criteria", {}).items():
                if value is not None:
                    labelled_here[criterion_id] = labelled_here.get(criterion_id, 0) + 1
        for criterion_id, count in labelled_here.items():
            per_criterion[criterion_id].labelled += count
            labelled_total += count

    for comparison in comparisons:
        for disagreement in comparison.disagreements:
            agreement = per_criterion[disagreement.criterion_id]
            agreement.signed_errors.append(disagreement.difference)

    for agreement in per_criterion.values():
        disagreements = len(agreement.signed_errors)
        agreement.exact = agreement.labelled - disagreements
        near_misses = sum(
            1 for error in agreement.signed_errors if abs(error) <= NEAR_MISS_TOLERANCE
        )
        agreement.within_tolerance = agreement.exact + near_misses
        # Agreements contribute a zero error each, so the mean reflects every label.
        agreement.signed_errors.extend([0] * agreement.exact)

    total_disagreements = sum(len(c.disagreements) for c in comparisons)
    exact_total = labelled_total - total_disagreements
    near_total = exact_total + sum(
        1
        for comparison in comparisons
        for disagreement in comparison.disagreements
        if abs(disagreement.difference) <= NEAR_MISS_TOLERANCE
    )
    band_compared = sum(c.band_compared for c in comparisons)
    band_matches = sum(c.band_matches for c in comparisons)
    pairs_compared = sum(c.ranking_pairs_compared for c in comparisons)
    pairs_tied = sum(c.ranking_pairs_tied_in_gold for c in comparisons)
    pairs_agreed = sum(c.ranking_pairs_agreed for c in comparisons)
    pairs_scoreable = pairs_compared - pairs_tied

    overall = {
        "labels_compared": labelled_total,
        "exact_agreement": exact_total / labelled_total if labelled_total else None,
        "agreement_within_one": near_total / labelled_total if labelled_total else None,
        "band_agreement": band_matches / band_compared if band_compared else None,
        "band_comparisons": band_compared,
        "ranking_pair_agreement": pairs_agreed / pairs_scoreable if pairs_scoreable else None,
        "ranking_pairs_compared": pairs_scoreable,
        "ranking_pairs_tied_in_gold": pairs_tied,
        "unlabelled_criteria": sum(c.unlabelled_criteria for c in comparisons),
        "disagreements": total_disagreements,
    }
    return per_criterion, overall


def _percent(value: float | None) -> str:
    return "no labels yet" if value is None else f"{value * 100:.1f}%"


def write_failure_analysis(
    comparisons: list[IntakeComparison],
    per_criterion: dict[str, CriterionAgreement],
    overall: dict[str, Any],
    rubric: Rubric,
    judge_id: str,
    path: Path,
) -> Path:
    """Write every disagreement out, with the engine rationale next to the label."""
    lines: list[str] = []
    lines.append("# Failure analysis")
    lines.append("")
    lines.append(
        f"Rubric {rubric.version}. Judge {judge_id}. "
        f"Run on {date.today().isoformat()}. "
        f"{len(comparisons)} intake(s), {overall['labels_compared']} label(s) compared."
    )
    lines.append("")
    lines.append(
        "Every row below is a place the engine and the labels disagree. Both sets of "
        "reasoning are shown as written, so that a wrong score and a bad reason can be "
        "told apart. A score that is right for the wrong reason is also a failure and "
        "will not show up here, which is a limit of this file."
    )
    lines.append("")

    if not overall["labels_compared"]:
        lines.append("## Nothing to analyse")
        lines.append("")
        lines.append(
            "No labels have been written yet. Fill in a gold file under eval/gold and "
            "run this again. See eval/gold/README.md."
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    lines.append("## Headline")
    lines.append("")
    lines.append(f"- Exact agreement: {_percent(overall['exact_agreement'])}")
    lines.append(f"- Agreement within one point: {_percent(overall['agreement_within_one'])}")
    lines.append(
        f"- Band agreement: {_percent(overall['band_agreement'])} "
        f"over {overall['band_comparisons']} fully labelled process(es)"
    )
    lines.append(
        f"- Ranking agreement on pairs: {_percent(overall['ranking_pair_agreement'])} "
        f"over {overall['ranking_pairs_compared']} pair(s)"
    )
    lines.append(f"- Criteria left unlabelled: {overall['unlabelled_criteria']}")
    lines.append("")

    lines.append("## By criterion")
    lines.append("")
    lines.append("| Criterion | Labels | Exact | Within one | Mean error | Lean |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for agreement in per_criterion.values():
        mean = agreement.mean_signed_error
        lines.append(
            f"| {agreement.label} | {agreement.labelled} | {_percent(agreement.exact_rate)} "
            f"| {_percent(agreement.near_rate)} "
            f"| {'n/a' if mean is None else f'{mean:+.2f}'} | {agreement.lean} |"
        )
    lines.append("")

    all_disagreements = [d for c in comparisons for d in c.disagreements]
    all_disagreements.sort(key=lambda d: (-abs(d.difference), d.intake_id, d.process_id, d.criterion_id))

    lines.append(f"## Every disagreement ({len(all_disagreements)})")
    lines.append("")
    lines.append("Largest gaps first.")
    lines.append("")

    for disagreement in all_disagreements:
        lines.append(
            f"### {disagreement.criterion_label}: {disagreement.process_name} "
            f"({disagreement.intake_id})"
        )
        lines.append("")
        lines.append(f"- Label: **{disagreement.gold_score}**")
        lines.append(
            f"- Engine: **{disagreement.engine_score}** "
            f"({disagreement.difference:+d} against the label)"
        )
        lines.append(f"- Engine reasoning: {disagreement.engine_rationale}")
        if disagreement.gold_note:
            lines.append(f"- Your reasoning: {disagreement.gold_note}")
        else:
            lines.append(
                "- Your reasoning: none written for this criterion, so there is nothing "
                "to compare the engine's reasoning against."
            )
        lines.append("")

    lines.append("## Scores side by side")
    lines.append("")
    lines.append("| Intake | Process | Engine | Engine band | Label | Label band |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for comparison in comparisons:
        for row in comparison.process_rows:
            gold_score = "not fully labelled" if row["gold_score"] is None else f"{row['gold_score']:.2f}"
            gold_band = row["gold_band"] or "n/a"
            lines.append(
                f"| {comparison.intake_id} | {row['process_name']} | "
                f"{row['engine_score']:.2f} | {row['engine_band']} | {gold_score} | {gold_band} |"
            )
    lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def run(
    gold_paths: list[Path],
    rubric: Rubric,
    output_dir: Path,
    stub_behaviour: str = "heuristic",
) -> dict[str, Any]:
    """Compare every gold file and write the results."""
    comparisons = [compare_one(path, rubric, stub_behaviour) for path in gold_paths]
    per_criterion, overall = summarise(comparisons, rubric)
    judge_id = get_judge("stub", behaviour=stub_behaviour).judge_id

    output_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "run_on": date.today().isoformat(),
        "rubric_version": rubric.version,
        "rubric_approved": rubric.approved,
        "judge_id": judge_id,
        "judge_mode": "stub",
        "overall": overall,
        "per_criterion": {
            agreement.criterion_id: {
                "label": agreement.label,
                "labels": agreement.labelled,
                "exact_agreement": agreement.exact_rate,
                "agreement_within_one": agreement.near_rate,
                "mean_signed_error": agreement.mean_signed_error,
                "mean_absolute_error": agreement.mean_absolute_error,
            }
            for agreement in per_criterion.values()
        },
        "intakes": [
            {
                "intake_id": comparison.intake_id,
                "gold_file": comparison.gold_path.name,
                "labelled_processes": comparison.labelled_processes,
                "disagreements": len(comparison.disagreements),
                "processes": comparison.process_rows,
            }
            for comparison in comparisons
        ],
    }
    (output_dir / "agreement.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    write_failure_analysis(
        comparisons, per_criterion, overall, rubric, judge_id, output_dir / "failure_analysis.md"
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="eval.harness",
        description="Compare engine scores against hand written gold labels.",
    )
    parser.add_argument(
        "--gold",
        type=Path,
        action="append",
        default=None,
        help="a specific gold file, repeatable. Defaults to every *.gold.json in eval/gold.",
    )
    parser.add_argument("--rubric", type=Path, default=None, help="path to rubric.md")
    parser.add_argument("--out", type=Path, default=OUTPUT_DIR, help="output directory")
    parser.add_argument(
        "--stub-behaviour", choices=["heuristic", "fixed"], default="heuristic"
    )
    args = parser.parse_args(argv)

    gold_paths = args.gold or sorted(GOLD_DIR.glob("*.gold.json"))
    if not gold_paths:
        print(
            f"No gold files found in {GOLD_DIR}. Nothing can be measured until labels "
            "exist. See eval/gold/README.md for how to write them.",
            file=sys.stderr,
        )
        return 1

    rubric = load_rubric(args.rubric)
    try:
        report = run(gold_paths, rubric, args.out, args.stub_behaviour)
    except GoldError as exc:
        print(f"Gold file problem: {exc}", file=sys.stderr)
        return 1

    overall = report["overall"]
    print(f"Rubric {rubric.version}, judge {report['judge_id']} (stub mode)")
    print(f"{len(gold_paths)} gold file(s), {overall['labels_compared']} label(s) compared")
    print()
    print(f"  Exact agreement       {_percent(overall['exact_agreement'])}")
    print(f"  Within one point      {_percent(overall['agreement_within_one'])}")
    print(f"  Band agreement        {_percent(overall['band_agreement'])}")
    print(f"  Ranking pairs agreed  {_percent(overall['ranking_pair_agreement'])}")
    print(f"  Unlabelled criteria   {overall['unlabelled_criteria']}")
    print()
    print(f"Wrote {args.out / 'agreement.json'}")
    print(f"Wrote {args.out / 'failure_analysis.md'}")
    if report["judge_mode"] == "stub":
        print()
        print(
            "These figures measure the stub judge, which applies fixed thresholds. "
            "They say nothing about how a real judge would score."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
