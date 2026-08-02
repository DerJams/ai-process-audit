"""Tests for the engine.

Written against unittest from the standard library rather than pytest, so that the
dependency list stays at three packages.

    python -m unittest discover -s tests -v

No test in this file makes a network call, and one test exists specifically to prove
that live judging cannot happen by accident.
"""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai_process_audit.intake.validator import IntakeValidationError, load_intake, validate_intake
from ai_process_audit.model.normalize import items_per_year, normalize_intake, slugify
from ai_process_audit.pipeline import audit_document
from ai_process_audit.processmap.mermaid import render_mermaid
from ai_process_audit.processmap.steps import build_process_map
from ai_process_audit.report.render import render_html, render_markdown, write_reports
from ai_process_audit.scoring.judge import (
    LIVE_ENV_FLAG,
    JudgeVerdict,
    LiveJudge,
    StubJudge,
    get_judge,
)
from ai_process_audit.scoring.rubric import RubricError, load_rubric
from ai_process_audit.scoring.score import rank, score_intake

REPO_ROOT = Path(__file__).resolve().parents[1]
INTAKE_DIR = REPO_ROOT / "eval" / "intakes"
ALL_INTAKES = sorted(INTAKE_DIR.glob("*.json"))

# Built from its code point rather than written out, so that this file does not
# itself trip the check it performs over the repository.
EM_DASH = chr(0x2014)


def minimal_intake() -> dict:
    return {
        "schema_version": "1.0.0",
        "intake_id": "test-business",
        "business": {
            "name": "Test Business",
            "industry": "Testing",
            "headcount": 4,
            "tools_in_use": ["Xero", "Excel"],
        },
        "processes": [
            {
                "id": "a-process",
                "name": "A process",
                "description": (
                    "The administrator collects the paper forms, then types each one "
                    "into Xero, then emails the customer to confirm."
                ),
                "frequency": "weekly",
                "volume": {"count": 10, "unit": "forms", "period": "per_week"},
                "people_involved": {
                    "count": 2,
                    "roles": ["administrator"],
                    "hours_per_run": 2,
                },
                "current_tools": ["paper", "Xero"],
                "pain_description": "It is slow and errors reach the customer.",
            }
        ],
    }


class TestValidator(unittest.TestCase):
    def test_minimal_intake_is_valid(self):
        self.assertIsNotNone(validate_intake(minimal_intake()))

    def test_every_synthetic_intake_is_valid(self):
        self.assertEqual(len(ALL_INTAKES), 3, "expected three synthetic intakes")
        for path in ALL_INTAKES:
            with self.subTest(intake=path.name):
                self.assertIsNotNone(load_intake(path))

    def test_missing_required_field_is_reported(self):
        document = minimal_intake()
        del document["processes"][0]["pain_description"]
        with self.assertRaises(IntakeValidationError) as caught:
            validate_intake(document)
        self.assertIn("pain_description", str(caught.exception))

    def test_unknown_field_is_rejected(self):
        document = minimal_intake()
        document["processes"][0]["urgency"] = "high"
        with self.assertRaises(IntakeValidationError):
            validate_intake(document)

    def test_all_errors_are_reported_at_once(self):
        document = minimal_intake()
        del document["processes"][0]["pain_description"]
        document["business"]["headcount"] = 0
        with self.assertRaises(IntakeValidationError) as caught:
            validate_intake(document)
        self.assertGreaterEqual(len(caught.exception.issues), 2)

    def test_bad_frequency_is_rejected(self):
        document = minimal_intake()
        document["processes"][0]["frequency"] = "sometimes"
        with self.assertRaises(IntakeValidationError):
            validate_intake(document)

    def test_missing_file_raises_validation_error(self):
        with self.assertRaises(IntakeValidationError):
            load_intake(REPO_ROOT / "does-not-exist.json")


class TestNormalise(unittest.TestCase):
    def test_weekly_volume_becomes_yearly(self):
        self.assertEqual(items_per_year(10, "per_week", 52), 520)

    def test_per_run_volume_uses_frequency(self):
        self.assertEqual(items_per_year(9, "per_run", 250), 2250)

    def test_slugify_is_stable_and_safe(self):
        self.assertEqual(slugify("Turning job sheets into invoices!"), "turning-job-sheets-into-invoices")
        self.assertEqual(slugify("   "), "process")

    def test_ad_hoc_frequency_is_flagged_as_assumed(self):
        document = minimal_intake()
        document["processes"][0]["frequency"] = "ad_hoc"
        intake = normalize_intake(validate_intake(document))
        self.assertTrue(intake.processes[0].frequency_is_assumed)

    def test_hours_per_year_is_derived(self):
        intake = normalize_intake(validate_intake(minimal_intake()))
        self.assertEqual(intake.processes[0].people.hours_per_year, 2 * 52)

    def test_duplicate_ids_are_made_unique(self):
        document = minimal_intake()
        second = copy.deepcopy(document["processes"][0])
        del document["processes"][0]["id"]
        del second["id"]
        document["processes"].append(second)
        intake = normalize_intake(validate_intake(document))
        ids = [process.id for process in intake.processes]
        self.assertEqual(len(ids), len(set(ids)))


class TestProcessMap(unittest.TestCase):
    def setUp(self):
        self.intake = normalize_intake(validate_intake(minimal_intake()))
        self.process = self.intake.processes[0]

    def test_steps_are_split_on_connectors(self):
        process_map = build_process_map(self.process, self.intake.business)
        self.assertGreaterEqual(process_map.step_count, 3)

    def test_step_kinds_are_recognised(self):
        process_map = build_process_map(self.process, self.intake.business)
        kinds = {step.kind for step in process_map.steps}
        self.assertIn("data_entry", kinds)
        self.assertIn("communication", kinds)

    def test_tools_are_detected(self):
        process_map = build_process_map(self.process, self.intake.business)
        self.assertIn("Xero", process_map.tools_touched)

    def test_actor_is_not_taken_from_object_position(self):
        # "emails the customer" must not make the customer the actor.
        process_map = build_process_map(self.process, self.intake.business)
        email_steps = [step for step in process_map.steps if "email" in step.text.lower()]
        self.assertTrue(email_steps)
        for step in email_steps:
            self.assertNotEqual(step.actor, "Customer")

    def test_empty_description_still_produces_a_map(self):
        document = minimal_intake()
        document["processes"][0]["description"] = "Someone. Does. It. Somehow. Occasionally."
        intake = normalize_intake(validate_intake(document))
        process_map = build_process_map(intake.processes[0], intake.business)
        self.assertGreaterEqual(process_map.step_count, 1)

    def test_mermaid_is_well_formed(self):
        for path in ALL_INTAKES:
            intake = normalize_intake(load_intake(path))
            for process in intake.processes:
                with self.subTest(process=process.id):
                    text = render_mermaid(build_process_map(process, intake.business))
                    self.assertTrue(text.startswith("flowchart TD"))
                    self.assertIn("start --> S1", text)
                    self.assertIn("--> finish", text)
                    # Quotes inside labels would break the diagram.
                    for line in text.splitlines():
                        if line.strip().startswith("S") and '"' in line:
                            self.assertEqual(line.count('"'), 2, line)

    def test_mermaid_labels_have_no_stray_markup(self):
        document = minimal_intake()
        document["processes"][0]["description"] = (
            'The admin types a "note" into the system <urgent>, then files it under #42.'
        )
        intake = normalize_intake(validate_intake(document))
        text = render_mermaid(build_process_map(intake.processes[0], intake.business))
        # Only the node lines are checked. classDef lines carry hex colours, where a
        # hash is meant to be there.
        node_lines = [
            line for line in text.splitlines() if line.strip().startswith(("S", "start", "finish"))
        ]
        self.assertTrue(node_lines)
        for line in node_lines:
            self.assertNotIn("<", line)
            self.assertNotIn(">", line.split("-->")[0])
            self.assertNotIn("#", line)


class TestRubric(unittest.TestCase):
    def setUp(self):
        self.rubric = load_rubric()

    def test_rubric_loads_from_markdown(self):
        self.assertEqual(len(self.rubric.criteria), 6)
        self.assertEqual(self.rubric.scale_min, 1)
        self.assertEqual(self.rubric.scale_max, 5)

    def test_weights_sum_to_one(self):
        self.assertAlmostEqual(sum(c.weight for c in self.rubric.criteria), 1.0, places=6)

    def test_draft_rubric_is_marked_unapproved(self):
        # This guards the deliverable: the draft must not silently look approved.
        self.assertIn("draft", self.rubric.version)
        self.assertFalse(self.rubric.approved)

    def test_risk_is_inverted_and_nothing_else_is(self):
        self.assertEqual(self.rubric.effective_score("implementation_risk", 5), 1.0)
        self.assertEqual(self.rubric.effective_score("implementation_risk", 1), 5.0)
        self.assertEqual(self.rubric.effective_score("pain", 5), 5.0)

    def test_bands_are_ordered_and_cover_the_scale(self):
        self.assertEqual(self.rubric.band_for(4.5).id, "strong")
        self.assertEqual(self.rubric.band_for(3.0).id, "pilot")
        self.assertEqual(self.rubric.band_for(2.99).id, "watch")
        self.assertEqual(self.rubric.band_for(0.0).id, "not_a_fit")

    def test_bad_weights_are_rejected(self):
        text = (REPO_ROOT / "rubric.md").read_text(encoding="utf-8")
        broken = text.replace('"weight": 0.20', '"weight": 0.90', 1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rubric.md"
            path.write_text(broken, encoding="utf-8")
            with self.assertRaises(RubricError):
                load_rubric(path)

    def test_missing_spec_block_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rubric.md"
            path.write_text("# A rubric with no machine block\n", encoding="utf-8")
            with self.assertRaises(RubricError):
                load_rubric(path)


class TestJudge(unittest.TestCase):
    def setUp(self):
        self.rubric = load_rubric()
        self.intake = normalize_intake(validate_intake(minimal_intake()))
        self.process = self.intake.processes[0]
        self.map = build_process_map(self.process, self.intake.business)

    def test_stub_scores_every_criterion(self):
        verdict = StubJudge().judge(self.process, self.map, self.rubric)
        self.assertEqual(set(verdict.scores), set(self.rubric.criterion_ids))

    def test_stub_scores_are_in_range(self):
        verdict = StubJudge().judge(self.process, self.map, self.rubric)
        for score in verdict.scores.values():
            self.assertGreaterEqual(score.score, self.rubric.scale_min)
            self.assertLessEqual(score.score, self.rubric.scale_max)

    def test_every_score_has_a_rationale(self):
        verdict = StubJudge().judge(self.process, self.map, self.rubric)
        for score in verdict.scores.values():
            self.assertTrue(score.rationale.strip())
            self.assertTrue(score.rationale.rstrip().endswith("."))

    def test_stub_is_deterministic(self):
        first = StubJudge().judge(self.process, self.map, self.rubric)
        second = StubJudge().judge(self.process, self.map, self.rubric)
        self.assertEqual(
            {k: v.score for k, v in first.scores.items()},
            {k: v.score for k, v in second.scores.items()},
        )

    def test_fixed_behaviour_returns_the_midpoint(self):
        verdict = StubJudge("fixed").judge(self.process, self.map, self.rubric)
        self.assertEqual({score.score for score in verdict.scores.values()}, {3})

    def test_keywords_match_whole_words_only(self):
        # Reapit contains the letters api. Substring matching read that as evidence
        # of a programmatic interface and scored data availability at the top.
        document = minimal_intake()
        document["processes"][0]["current_tools"] = ["Reapit"]
        document["processes"][0]["description"] = (
            "The administrator opens Reapit and reads the record, then types the "
            "result onto a paper form."
        )
        document["processes"][0]["data_notes"] = "Everything is in Reapit and on paper."
        intake = normalize_intake(validate_intake(document))
        process = intake.processes[0]
        process_map = build_process_map(process, intake.business)
        verdict = StubJudge().judge(process, process_map, self.rubric)
        rationale = verdict.scores["data_availability"].rationale
        self.assertNotIn("api", rationale.split())

    def test_verdict_records_the_rubric_version(self):
        verdict = StubJudge().judge(self.process, self.map, self.rubric)
        self.assertEqual(verdict.rubric_version, self.rubric.version)

    def test_default_judge_is_the_stub(self):
        self.assertEqual(get_judge().mode, "stub")

    def test_live_judge_is_refused_without_the_environment_flag(self):
        import os

        previous = os.environ.pop(LIVE_ENV_FLAG, None)
        try:
            with self.assertRaises(RuntimeError):
                get_judge("live")
        finally:
            if previous is not None:
                os.environ[LIVE_ENV_FLAG] = previous

    def test_live_judge_is_not_implemented_even_when_constructed(self):
        with self.assertRaises(NotImplementedError):
            LiveJudge().judge(self.process, self.map, self.rubric)

    def test_unknown_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            get_judge("magic")


class TestScoring(unittest.TestCase):
    def setUp(self):
        self.rubric = load_rubric()
        self.intake = normalize_intake(load_intake(INTAKE_DIR / "redwood-plumbing.json"))

    def test_scoring_produces_one_opportunity_per_process(self):
        result = score_intake(self.intake, rubric=self.rubric)
        self.assertEqual(len(result.opportunities), len(self.intake.processes))

    def test_ranks_are_sequential_and_ordered(self):
        result = score_intake(self.intake, rubric=self.rubric)
        scores = [item.weighted_score for item in result.opportunities]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual(
            [item.rank for item in result.opportunities],
            list(range(1, len(scores) + 1)),
        )

    def test_weighted_score_stays_on_the_rubric_scale(self):
        result = score_intake(self.intake, rubric=self.rubric)
        for item in result.opportunities:
            self.assertGreaterEqual(item.weighted_score, self.rubric.scale_min)
            self.assertLessEqual(item.weighted_score, self.rubric.scale_max)

    def test_fixed_stub_gives_every_process_the_same_score(self):
        result = score_intake(self.intake, judge=StubJudge("fixed"), rubric=self.rubric)
        scores = {item.weighted_score for item in result.opportunities}
        self.assertEqual(len(scores), 1)

    def test_high_risk_lowers_the_score(self):
        # A process identical but for a risk flag must not score higher.
        document = minimal_intake()
        risky = copy.deepcopy(document["processes"][0])
        risky["id"] = "risky-process"
        risky["risk_flags"] = ["safety_critical"]
        document["processes"].append(risky)
        result = audit_document(document)
        by_id = {item.process.id: item for item in result.opportunities}
        self.assertLess(
            by_id["risky-process"].weighted_score, by_id["a-process"].weighted_score
        )

    def test_verdict_from_another_rubric_version_is_refused(self):
        from ai_process_audit.scoring.score import score_process

        process = self.intake.processes[0]
        process_map = build_process_map(process, self.intake.business)
        stale = JudgeVerdict(process.id, "stub", "stub", "0.0.0-other", {})
        with self.assertRaises(ValueError):
            score_process(process, process_map, stale, self.rubric)

    def test_ranking_ties_break_predictably(self):
        result = score_intake(self.intake, judge=StubJudge("fixed"), rubric=self.rubric)
        ids = [item.process.id for item in result.opportunities]
        self.assertEqual(ids, sorted(ids))

    def test_whole_pipeline_is_reproducible(self):
        first = score_intake(self.intake, rubric=self.rubric)
        second = score_intake(self.intake, rubric=self.rubric)
        self.assertEqual(
            [(i.process.id, i.weighted_score, i.rank) for i in first.opportunities],
            [(i.process.id, i.weighted_score, i.rank) for i in second.opportunities],
        )


class TestReport(unittest.TestCase):
    def setUp(self):
        self.rubric = load_rubric()
        self.intake = normalize_intake(load_intake(INTAKE_DIR / "northgate-lettings.json"))
        self.result = score_intake(self.intake, rubric=self.rubric)

    def test_markdown_contains_the_disclosure_and_every_process(self):
        text = render_markdown(self.result, generated_on=date(2026, 8, 1))
        self.assertIn("generated by an AI system", text)
        for process in self.intake.processes:
            self.assertIn(process.name, text)

    def test_markdown_includes_a_mermaid_block_per_process(self):
        text = render_markdown(self.result, generated_on=date(2026, 8, 1))
        self.assertEqual(text.count("```mermaid"), len(self.intake.processes))

    def test_html_warns_about_stub_mode_and_the_draft_rubric(self):
        html = render_html(self.result, generated_on=date(2026, 8, 1))
        self.assertIn("stub judge", html)
        self.assertIn("draft", html)

    def test_reports_are_written_even_when_the_pdf_cannot_be(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = write_reports(self.result, directory, generated_on=date(2026, 8, 1))
            self.assertTrue(paths.markdown.exists())
            self.assertTrue(paths.html.exists())
            self.assertEqual(len(paths.mermaid), len(self.intake.processes))
            if paths.pdf is None:
                self.assertIsNotNone(paths.pdf_error)
            else:
                self.assertTrue(paths.pdf.exists())

    def test_no_em_dashes_anywhere_in_the_output(self):
        text = render_markdown(self.result, generated_on=date(2026, 8, 1))
        html = render_html(self.result, generated_on=date(2026, 8, 1))
        for name, content in (("markdown", text), ("html", html)):
            with self.subTest(output=name):
                self.assertNotIn(EM_DASH, content)
                self.assertNotIn("&mdash;", content)


class TestNoEmDashesInSource(unittest.TestCase):
    """The no em dash rule applies to the repository, not only to reports."""

    def test_repository_text_has_no_em_dashes(self):
        patterns = ("*.py", "*.md", "*.json", "*.j2", "*.txt", "*.toml")
        offenders = []
        for pattern in patterns:
            for path in REPO_ROOT.rglob(pattern):
                if any(part in {".venv", ".git", "out", "__pycache__"} for part in path.parts):
                    continue
                if EM_DASH in path.read_text(encoding="utf-8"):
                    offenders.append(str(path.relative_to(REPO_ROOT)))
        self.assertEqual(offenders, [])


class TestEndToEnd(unittest.TestCase):
    def test_every_synthetic_intake_runs_end_to_end(self):
        for path in ALL_INTAKES:
            with self.subTest(intake=path.name):
                document = load_intake(path)
                result = audit_document(document)
                self.assertTrue(result.opportunities)
                with tempfile.TemporaryDirectory() as directory:
                    paths = write_reports(result, directory, generated_on=date(2026, 8, 1))
                    self.assertTrue(paths.markdown.read_text(encoding="utf-8"))

    def test_cli_score_json_is_parseable(self):
        from ai_process_audit.cli import main as cli_main
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli_main(["score", str(INTAKE_DIR / "kilner-food-wholesale.json"), "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["judge_mode"], "stub")
        self.assertFalse(payload["rubric_approved"])


if __name__ == "__main__":
    unittest.main()
