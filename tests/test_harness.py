"""Tests for the evaluation harness.

These matter more than they look. The harness is the only thing in the repository
that can tell you the engine is wrong, so a harness that quietly agrees with itself
would be worse than having no harness at all.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from ai_process_audit.scoring.rubric import load_rubric
from eval.harness import GoldError, compare_one, run, summarise

INTAKE = REPO_ROOT / "eval" / "intakes" / "redwood-plumbing.json"


def gold_document(rubric_version: str, criteria: dict[str, int | None]) -> dict:
    return {
        "gold_format": "1",
        "intake_file": str(INTAKE).replace("\\", "/"),
        "rubric_version": rubric_version,
        "labelled_by": "A Person",
        "labelled_on": "2026-08-01",
        "processes": {
            "job-sheet-to-invoice": {
                "process_name": "Turning job sheets into invoices",
                "notes": "a note",
                "criteria": dict(criteria),
            }
        },
    }


class HarnessTestCase(unittest.TestCase):
    def setUp(self):
        self.rubric = load_rubric()
        self.full_labels = {criterion.id: 3 for criterion in self.rubric.criteria}

    def write_gold(self, directory: str, document: dict, name: str = "x.gold.json") -> Path:
        path = Path(directory) / name
        path.write_text(json.dumps(document), encoding="utf-8")
        return path


class TestGoldGuards(HarnessTestCase):
    def test_rubric_version_mismatch_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_gold(
                directory, gold_document("0.0.1-other", self.full_labels)
            )
            with self.assertRaises(GoldError) as caught:
                compare_one(path, self.rubric)
            self.assertIn("not comparable", str(caught.exception))

    def test_missing_labelled_by_is_refused(self):
        document = gold_document(self.rubric.version, self.full_labels)
        document["labelled_by"] = ""
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_gold(directory, document)
            with self.assertRaises(GoldError):
                compare_one(path, self.rubric)

    def test_unknown_criterion_is_refused(self):
        labels = dict(self.full_labels)
        labels["vibes"] = 5
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_gold(directory, gold_document(self.rubric.version, labels))
            with self.assertRaises(GoldError) as caught:
                compare_one(path, self.rubric)
            self.assertIn("vibes", str(caught.exception))

    def test_unknown_process_id_is_refused(self):
        document = gold_document(self.rubric.version, self.full_labels)
        document["processes"]["not-a-real-process"] = document["processes"].pop(
            "job-sheet-to-invoice"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_gold(directory, document)
            with self.assertRaises(GoldError):
                compare_one(path, self.rubric)

    def test_harness_never_writes_to_the_gold_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_gold(
                directory, gold_document(self.rubric.version, self.full_labels)
            )
            before = path.read_bytes()
            run([path], self.rubric, Path(directory) / "out")
            self.assertEqual(path.read_bytes(), before)


class TestAgreementMaths(HarnessTestCase):
    def test_perfect_agreement_reports_one_hundred_percent(self):
        # Label with exactly what the engine says, then check the harness notices.
        with tempfile.TemporaryDirectory() as directory:
            seed = self.write_gold(
                directory, gold_document(self.rubric.version, self.full_labels)
            )
            comparison = compare_one(seed, self.rubric)
            engine_scores = {
                row["process_id"]: row for row in comparison.process_rows
            }
            self.assertIn("job-sheet-to-invoice", engine_scores)

            from ai_process_audit.pipeline import audit_file

            result = audit_file(INTAKE)
            opportunity = next(
                item for item in result.opportunities if item.process.id == "job-sheet-to-invoice"
            )
            matching = {c.id: c.raw_score for c in opportunity.criteria}
            path = self.write_gold(
                directory,
                gold_document(self.rubric.version, matching),
                name="perfect.gold.json",
            )
            report = run([path], self.rubric, Path(directory) / "out")
            self.assertEqual(report["overall"]["exact_agreement"], 1.0)
            self.assertEqual(report["overall"]["disagreements"], 0)

    def test_nulls_are_counted_as_unlabelled_not_as_agreement(self):
        labels = {criterion.id: None for criterion in self.rubric.criteria}
        labels["pain"] = 3
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_gold(directory, gold_document(self.rubric.version, labels))
            report = run([path], self.rubric, Path(directory) / "out")
            self.assertEqual(report["overall"]["labels_compared"], 1)
            self.assertEqual(
                report["overall"]["unlabelled_criteria"], len(self.rubric.criteria) - 1
            )

    def test_partially_labelled_process_gets_no_band(self):
        labels = {criterion.id: None for criterion in self.rubric.criteria}
        labels["pain"] = 5
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_gold(directory, gold_document(self.rubric.version, labels))
            report = run([path], self.rubric, Path(directory) / "out")
            self.assertEqual(report["overall"]["band_comparisons"], 0)
            self.assertIsNone(report["intakes"][0]["processes"][0]["gold_score"])

    def test_signed_error_shows_the_direction_of_the_lean(self):
        # Label everything at 1, so any engine score above 1 leans high.
        labels = {criterion.id: 1 for criterion in self.rubric.criteria}
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_gold(directory, gold_document(self.rubric.version, labels))
            report = run([path], self.rubric, Path(directory) / "out")
            leans = [
                entry["mean_signed_error"]
                for entry in report["per_criterion"].values()
                if entry["mean_signed_error"] is not None
            ]
            self.assertTrue(any(value > 0 for value in leans))

    def test_outputs_are_written(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_gold(
                directory, gold_document(self.rubric.version, self.full_labels)
            )
            out = Path(directory) / "out"
            run([path], self.rubric, out)
            self.assertTrue((out / "agreement.json").exists())
            analysis = (out / "failure_analysis.md").read_text(encoding="utf-8")
            self.assertIn("Failure analysis", analysis)
            self.assertIn("Every disagreement", analysis)

    def test_failure_analysis_shows_rationale_beside_the_label(self):
        labels = {criterion.id: 1 for criterion in self.rubric.criteria}
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_gold(directory, gold_document(self.rubric.version, labels))
            out = Path(directory) / "out"
            run([path], self.rubric, out)
            analysis = (out / "failure_analysis.md").read_text(encoding="utf-8")
            self.assertIn("Label: **1**", analysis)
            self.assertIn("Engine rationale:", analysis)
            self.assertIn("Label note: a note", analysis)

    def test_empty_gold_set_produces_an_honest_empty_report(self):
        labels = {criterion.id: None for criterion in self.rubric.criteria}
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_gold(directory, gold_document(self.rubric.version, labels))
            out = Path(directory) / "out"
            report = run([path], self.rubric, out)
            self.assertIsNone(report["overall"]["exact_agreement"])
            self.assertIn(
                "No labels have been written yet",
                (out / "failure_analysis.md").read_text(encoding="utf-8"),
            )


class TestTemplateGenerator(unittest.TestCase):
    def test_template_contains_only_nulls(self):
        from eval.make_gold_template import main as make_template

        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "generated.gold.json"
            code = make_template([str(INTAKE), "--out", str(out)])
            self.assertEqual(code, 0)
            document = json.loads(out.read_text(encoding="utf-8"))
            for entry in document["processes"].values():
                for value in entry["criteria"].values():
                    self.assertIsNone(value)
            self.assertEqual(document["labelled_by"], "")

    def test_template_refuses_to_overwrite_real_labels(self):
        from eval.make_gold_template import main as make_template

        rubric = load_rubric()
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "existing.gold.json"
            out.write_text(
                json.dumps(gold_document(rubric.version, {"pain": 4})), encoding="utf-8"
            )
            before = out.read_bytes()
            code = make_template([str(INTAKE), "--out", str(out)])
            self.assertEqual(code, 1)
            self.assertEqual(out.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
