"""Write an empty gold label template for an intake.

    python -m eval.make_gold_template eval/intakes/redwood-plumbing.json

This produces a file with every process and every criterion listed and every score
set to null. It does not fill anything in, and it will refuse to overwrite a file
that already has labels in it. Filling it in is the whole point of the exercise and
is done by a person.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai_process_audit.intake.validator import load_intake
from ai_process_audit.model.normalize import normalize_intake
from ai_process_audit.scoring.rubric import load_rubric

GOLD_DIR = Path(__file__).resolve().parent / "gold"


def _has_labels(path: Path) -> bool:
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True  # unreadable, so treat it as precious and refuse to touch it
    for entry in existing.get("processes", {}).values():
        if any(value is not None for value in entry.get("criteria", {}).values()):
            return True
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="eval.make_gold_template",
        description="Write an empty gold label file for an intake. Scores are left null.",
    )
    parser.add_argument("intake", type=Path, help="path to an intake JSON file")
    parser.add_argument("--out", type=Path, default=None, help="where to write the template")
    parser.add_argument("--rubric", type=Path, default=None, help="path to rubric.md")
    args = parser.parse_args(argv)

    rubric = load_rubric(args.rubric)
    intake = normalize_intake(load_intake(args.intake))

    out_path = args.out or GOLD_DIR / f"{intake.intake_id}.gold.json"
    if out_path.exists() and _has_labels(out_path):
        print(
            f"{out_path} already contains labels. Refusing to overwrite it. "
            "Gold labels are never regenerated.",
            file=sys.stderr,
        )
        return 1

    intake_path = Path(args.intake).resolve()
    try:
        # A path relative to the gold file keeps the pair movable together.
        intake_reference = os.path.relpath(intake_path, out_path.parent.resolve())
    except ValueError:
        # Different drives on Windows have no relative path between them.
        intake_reference = str(intake_path)
    intake_reference = intake_reference.replace("\\", "/")

    template = {
        "gold_format": "2",
        "intake_file": intake_reference,
        "rubric_version": rubric.version,
        "labelled_by": "",
        "labelled_on": "",
        "labelling_notes": (
            "Score each criterion from 1 to 5 using the anchor tables in rubric.md. "
            "Score implementation risk as risk, where 5 is riskiest. Leave a score as "
            "null if you have not decided yet. Put your reasoning for a score in "
            "rationales under the same criterion id, which is what the failure "
            "analysis prints next to the engine's reasoning. Do not change a label to "
            "make the engine agree."
        ),
        "processes": {
            process.id: {
                "process_name": process.name,
                "notes": "",
                "criteria": {criterion.id: None for criterion in rubric.criteria},
                "rationales": {criterion.id: "" for criterion in rubric.criteria},
            }
            for process in intake.processes
        },
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(template, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote empty template to {out_path}")
    print(
        f"{len(intake.processes)} process(es) x {len(rubric.criteria)} criteria "
        f"= {len(intake.processes) * len(rubric.criteria)} labels to write."
    )
    print("Set labelled_by before running the harness, it will refuse the file without it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
