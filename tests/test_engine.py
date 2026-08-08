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
from ai_process_audit.model.normalize import (
    WORKING_WEEKS_PER_YEAR,
    items_per_year,
    normalize_intake,
    slugify,
)
from ai_process_audit.pipeline import audit_document
from ai_process_audit.processmap.mermaid import render_mermaid
from ai_process_audit.processmap.steps import build_process_map
from ai_process_audit.report.render import (
    PdfUnavailableError,
    find_edge,
    render_html,
    render_markdown,
    write_reports,
)
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
        "schema_version": "1.2.0",
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
                "time_spent": {"hours_per_week": 4},
                "current_tools": ["paper", "Xero"],
                "pain_description": "It is slow and errors reach the customer.",
                "baseline_metric": "forms turned round the same day, currently about half",
            }
        ],
    }


def high_scoring_risky_intake() -> dict:
    """An intake that scores well on everything and is maximally risky.

    Used to exercise the band cap, which only does anything when a process would
    otherwise be recommended above a pilot.
    """
    document = minimal_intake()
    process = document["processes"][0]
    process["frequency"] = "daily"
    process["volume"] = {"count": 500, "unit": "forms", "period": "per_day"}
    process["people_involved"]["hours_per_run"] = 8
    process["time_spent"] = {"hours_per_week": 12}
    # Nothing physical, so software automatability is high and the band cap is what
    # holds this back rather than the new criterion.
    process["description"] = (
        "The administrator opens the queue, then types each record into Xero, "
        "then emails the customer to confirm."
    )
    process["pain_description"] = (
        "Errors reach customers, staff work overtime and weekends, two people quit, "
        "and the business lost money on refunds and a penalty."
    )
    process["data_notes"] = "Everything is in the database with an api available."
    process["risk_flags"] = ["safety_critical"]
    return document


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

    def test_process_without_time_spent_is_rejected(self):
        document = minimal_intake()
        del document["processes"][0]["time_spent"]
        with self.assertRaises(IntakeValidationError) as caught:
            validate_intake(document)
        self.assertIn("time_spent", str(caught.exception))

    def test_time_spent_needs_at_least_one_figure(self):
        document = minimal_intake()
        document["processes"][0]["time_spent"] = {}
        with self.assertRaises(IntakeValidationError):
            validate_intake(document)

    def test_either_time_spent_figure_alone_is_enough(self):
        for field in ("hours_per_week", "minutes_per_case"):
            with self.subTest(field=field):
                document = minimal_intake()
                document["processes"][0]["time_spent"] = {field: 5}
                self.assertIsNotNone(validate_intake(document))

    def test_baseline_metric_may_be_null(self):
        document = minimal_intake()
        document["processes"][0]["baseline_metric"] = None
        self.assertIsNotNone(validate_intake(document))

    def test_exception_rate_is_accepted(self):
        for value in ("rare", "occasional", "frequent"):
            with self.subTest(value=value):
                document = minimal_intake()
                document["processes"][0]["exception_rate"] = value
                self.assertIsNotNone(validate_intake(document))

    def test_bad_exception_rate_is_rejected(self):
        document = minimal_intake()
        document["processes"][0]["exception_rate"] = "loads"
        with self.assertRaises(IntakeValidationError):
            validate_intake(document)

    def test_planned_system_change_is_accepted(self):
        document = minimal_intake()
        document["processes"][0]["planned_system_change"] = {
            "description": "Moving from the old system to a new one next year.",
            "timeframe": "three_to_12_months",
        }
        self.assertIsNotNone(validate_intake(document))

    def test_planned_system_change_timeframe_is_optional(self):
        document = minimal_intake()
        document["processes"][0]["planned_system_change"] = {
            "description": "Replacing the booking system at some point."
        }
        self.assertIsNotNone(validate_intake(document))

    def test_planned_system_change_needs_a_description(self):
        document = minimal_intake()
        document["processes"][0]["planned_system_change"] = {
            "timeframe": "within_3_months"
        }
        with self.assertRaises(IntakeValidationError) as caught:
            validate_intake(document)
        self.assertIn("description", str(caught.exception))

    def test_bad_planned_change_timeframe_is_rejected(self):
        document = minimal_intake()
        document["processes"][0]["planned_system_change"] = {
            "description": "Something is changing.",
            "timeframe": "soon_ish",
        }
        with self.assertRaises(IntakeValidationError):
            validate_intake(document)

    def test_intakes_written_against_1_2_0_are_still_accepted(self):
        # 1.3.0 only adds an optional field, so it is backward compatible and the
        # three synthetic intakes are not edited to keep up.
        document = minimal_intake()
        document["schema_version"] = "1.2.0"
        self.assertIsNotNone(validate_intake(document))

    def test_customer_facing_is_not_a_risk_flag(self):
        # It has its own boolean field. Allowing both would let one fact be counted
        # twice in the risk score, and let an intake contradict itself.
        document = minimal_intake()
        document["processes"][0]["risk_flags"] = ["customer_facing"]
        with self.assertRaises(IntakeValidationError):
            validate_intake(document)

    def test_the_remaining_risk_flags_are_still_accepted(self):
        document = minimal_intake()
        document["processes"][0]["risk_flags"] = [
            "handles_money",
            "regulated_data",
            "safety_critical",
            "legally_binding",
        ]
        self.assertIsNotNone(validate_intake(document))

    def test_no_synthetic_intake_uses_the_retired_flag(self):
        for path in ALL_INTAKES:
            with self.subTest(intake=path.name):
                document = load_intake(path)
                for process in document["processes"]:
                    self.assertNotIn("customer_facing", process.get("risk_flags", []))

    def test_bad_decision_type_is_rejected(self):
        document = minimal_intake()
        document["processes"][0]["decision_type"] = "gut_feel"
        with self.assertRaises(IntakeValidationError):
            validate_intake(document)

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

    def test_weekly_time_becomes_a_working_year(self):
        intake = normalize_intake(validate_intake(minimal_intake()))
        time_spent = intake.processes[0].time_spent
        self.assertEqual(time_spent.hours_per_year, 4 * WORKING_WEEKS_PER_YEAR)
        self.assertIsNone(time_spent.hours_per_year_from_cases)

    def test_minutes_per_case_uses_yearly_volume(self):
        document = minimal_intake()
        # 10 forms a week is 520 a year, at 30 minutes each, so 260 hours.
        document["processes"][0]["time_spent"] = {"minutes_per_case": 30}
        intake = normalize_intake(validate_intake(document))
        self.assertEqual(intake.processes[0].time_spent.hours_per_year, 260.0)

    def test_both_time_figures_keep_the_weekly_one_as_primary(self):
        document = minimal_intake()
        document["processes"][0]["time_spent"] = {
            "hours_per_week": 4,
            "minutes_per_case": 30,
        }
        time_spent = normalize_intake(validate_intake(document)).processes[0].time_spent
        self.assertEqual(time_spent.hours_per_year, 4 * WORKING_WEEKS_PER_YEAR)
        self.assertEqual(time_spent.hours_per_year_from_cases, 260.0)
        self.assertTrue(time_spent.has_cross_check)

    def test_absent_and_null_baseline_mean_the_same_thing(self):
        absent = minimal_intake()
        del absent["processes"][0]["baseline_metric"]
        explicit_null = minimal_intake()
        explicit_null["processes"][0]["baseline_metric"] = None
        for name, document in (("absent", absent), ("null", explicit_null)):
            with self.subTest(case=name):
                intake = normalize_intake(validate_intake(document))
                self.assertFalse(intake.processes[0].has_baseline)

    def test_optional_fields_default_to_none(self):
        document = minimal_intake()
        intake = normalize_intake(validate_intake(document))
        process = intake.processes[0]
        self.assertIsNone(process.decision_type)
        self.assertIsNone(process.customer_facing)

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

    def test_shorten_cuts_on_a_word_boundary(self):
        from ai_process_audit.processmap.mermaid import _shorten

        text = "The administrator collects every single completed registration form today"
        for limit in range(20, 60, 7):
            with self.subTest(limit=limit):
                out = _shorten(text, limit)
                self.assertLessEqual(len(out), limit)
                self.assertTrue(out.endswith("..."))
                body = out[:-3]
                # The kept text is a prefix of the original, and the original has a
                # space right after it, so nothing was cut through a word.
                self.assertTrue(text.startswith(body))
                self.assertEqual(text[len(body)], " ")

    def test_short_labels_are_left_alone(self):
        from ai_process_audit.processmap.mermaid import _shorten

        self.assertEqual(_shorten("Types it into Xero", 72), "Types it into Xero")

    def test_rendered_labels_are_never_cut_mid_word(self):
        document = minimal_intake()
        document["processes"][0]["description"] = (
            "The administrator collects every single completed customer registration "
            "form from the front counter and checks it against the appointment diary, "
            "then types the whole thing into the system."
        )
        intake = normalize_intake(validate_intake(document))
        process_map = build_process_map(intake.processes[0], intake.business)
        text = render_mermaid(process_map)
        truncated = [line for line in text.splitlines() if '..."' in line]
        self.assertTrue(truncated, "this fixture is meant to produce a long label")
        sources = [step.text for step in process_map.steps]
        for line in truncated:
            label = line.split('"')[1]
            body = label[:-3].split(": ", 1)[-1]
            self.assertTrue(
                any(source.startswith(body) and source[len(body) : len(body) + 1] == " " for source in sources),
                f"label was cut inside a word: {label}",
            )

    def test_map_carries_a_legend_for_the_shapes_it_uses(self):
        intake = normalize_intake(validate_intake(minimal_intake()))
        process_map = build_process_map(intake.processes[0], intake.business)
        text = render_mermaid(process_map)
        self.assertIn('subgraph legend["What the shapes mean"]', text)
        # Only shapes actually on the map are explained.
        self.assertIn("Typing or copying data", text)
        self.assertNotIn("Waiting on someone", text)

    def test_legend_is_omitted_when_there_is_nothing_to_explain(self):
        document = minimal_intake()
        document["processes"][0]["description"] = (
            "The administrator types the form into the system every single morning."
        )
        intake = normalize_intake(validate_intake(document))
        text = render_mermaid(build_process_map(intake.processes[0], intake.business))
        self.assertNotIn("subgraph legend", text)

    def test_handoffs_are_counted_across_elided_subjects(self):
        # The subject is named once and then dropped, which is how people write. Only
        # counting steps that name someone missed almost every handoff, so these came
        # out at zero on processes that visibly pass work between people.
        document = minimal_intake()
        document["processes"][0]["people_involved"]["roles"] = ["designer"]
        document["processes"][0]["description"] = (
            "The designer prepares the proof, then emails it out. "
            "The customer reviews it and replies with changes. "
            "The designer amends the artwork and sends it back."
        )
        intake = normalize_intake(validate_intake(document))
        process_map = build_process_map(intake.processes[0], intake.business)
        self.assertEqual(process_map.handoff_count, 2)

    def test_a_subject_after_a_subordinating_conjunction_is_found(self):
        # "When the customer replies" has the same subject as "The customer replies",
        # and skipping it was part of why handoffs read as zero.
        document = minimal_intake()
        document["processes"][0]["description"] = (
            "The administrator files the form. When the customer replies to the email, "
            "the job is closed off."
        )
        intake = normalize_intake(validate_intake(document))
        process_map = build_process_map(intake.processes[0], intake.business)
        actors = [step.actor for step in process_map.steps if step.actor]
        self.assertIn("Customer", actors)

    def test_carried_actors_are_not_shown_as_if_they_were_read(self):
        document = minimal_intake()
        document["processes"][0]["people_involved"]["roles"] = ["designer"]
        document["processes"][0]["description"] = (
            "A designer prepares the proof, then emails it out to the printer."
        )
        intake = normalize_intake(validate_intake(document))
        process_map = build_process_map(intake.processes[0], intake.business)
        # Carried forward for counting, absent from the step itself and from the map.
        self.assertEqual(process_map.effective_actors[1], "designer")
        self.assertIsNone(process_map.steps[1].actor)
        text = render_mermaid(process_map)
        self.assertEqual(text.count("designer:"), 1)

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
        self.assertEqual(len(self.rubric.criteria), 7)
        self.assertEqual(self.rubric.scale_min, 1)
        self.assertEqual(self.rubric.scale_max, 5)

    def test_the_rescale_preserved_every_relative_relationship(self):
        # 1.4.0 multiplied the original six by 0.8 to make room for the new criterion.
        # The point of a mechanical rescale is that no relationship moves.
        weights = {c.id: c.weight for c in self.rubric.criteria}
        self.assertAlmostEqual(weights["software_automatability"], 0.20, places=6)
        for heavier in ("pain", "data_availability"):
            self.assertAlmostEqual(weights[heavier], 0.16, places=6)
        for lighter in ("frequency", "volume", "implementation_risk", "return_band"):
            self.assertAlmostEqual(weights[lighter], 0.12, places=6)
        # Pain and data availability level with each other, and heavier than the four.
        self.assertEqual(weights["pain"], weights["data_availability"])
        self.assertGreater(weights["pain"], weights["frequency"])
        # The original six still sum to 0.80 between them.
        original = [c for c in self.rubric.criteria if c.id != "software_automatability"]
        self.assertAlmostEqual(sum(c.weight for c in original), 0.80, places=6)

    def test_software_automatability_is_defined_and_points_the_right_way(self):
        criterion = self.rubric.criterion("software_automatability")
        self.assertEqual(criterion.direction, "higher_is_better")
        self.assertFalse(criterion.is_inverted)
        self.assertIn("software", criterion.question.lower())

    def test_the_disqualification_is_defined(self):
        self.assertTrue(self.rubric.disqualifications)
        rule = self.rubric.disqualifications[0]
        self.assertEqual(rule.criterion, "software_automatability")
        self.assertEqual(rule.at_or_below, 1)
        self.assertEqual(
            rule.referral,
            "This process would benefit from collaboration with a robotics "
            "automation specialist local to your area.",
        )

    def test_disqualification_without_a_referral_is_rejected(self):
        text = (REPO_ROOT / "rubric.md").read_text(encoding="utf-8")
        broken = text.replace(
            '"referral": "This process would benefit', '"not_a_referral": "This process would benefit'
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rubric.md"
            path.write_text(broken, encoding="utf-8")
            with self.assertRaises(RubricError) as caught:
                load_rubric(path)
            self.assertIn("referral", str(caught.exception))

    def test_disqualification_fires_only_at_the_stated_score(self):
        self.assertIsNotNone(
            self.rubric.disqualification_for({"software_automatability": 1})
        )
        for score in (2, 3, 4, 5):
            with self.subTest(score=score):
                self.assertIsNone(
                    self.rubric.disqualification_for({"software_automatability": score})
                )

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

    def test_the_return_band_criterion_cap_is_defined(self):
        self.assertTrue(self.rubric.criterion_caps)
        cap = self.rubric.criterion_caps[0]
        self.assertEqual(cap.criterion, "return_band")
        self.assertEqual(cap.condition, "no_baseline_metric")
        self.assertEqual(cap.max_score, 2)
        self.assertTrue(cap.reason)

    def test_unknown_cap_condition_is_rejected(self):
        text = (REPO_ROOT / "rubric.md").read_text(encoding="utf-8")
        broken = text.replace('"condition": "no_baseline_metric"', '"condition": "feels_wrong"')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rubric.md"
            path.write_text(broken, encoding="utf-8")
            with self.assertRaises(RubricError) as caught:
                load_rubric(path)
            self.assertIn("feels_wrong", str(caught.exception))

    def test_the_risk_cap_is_defined(self):
        self.assertTrue(self.rubric.band_caps)
        cap = self.rubric.band_caps[0]
        self.assertEqual(cap.criterion, "implementation_risk")
        self.assertEqual(cap.at_or_above, 5)
        self.assertEqual(cap.max_band, "pilot")
        self.assertTrue(cap.reason)

    def test_cap_lowers_a_strong_band_when_risk_is_top_of_scale(self):
        strong = self.rubric.band_for(4.6)
        self.assertEqual(strong.id, "strong")
        band, applied = self.rubric.apply_caps(strong, {"implementation_risk": 5})
        self.assertEqual(band.id, "pilot")
        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0].band_before.id, "strong")
        self.assertEqual(applied[0].band_after.id, "pilot")

    def test_cap_does_nothing_below_the_threshold(self):
        strong = self.rubric.band_for(4.6)
        band, applied = self.rubric.apply_caps(strong, {"implementation_risk": 4})
        self.assertEqual(band.id, "strong")
        self.assertEqual(applied, ())

    def test_cap_never_raises_a_band(self):
        watch = self.rubric.band_for(2.5)
        band, applied = self.rubric.apply_caps(watch, {"implementation_risk": 5})
        self.assertEqual(band.id, "watch")
        self.assertEqual(applied, ())

    def test_cap_referring_to_an_unknown_criterion_is_rejected(self):
        text = (REPO_ROOT / "rubric.md").read_text(encoding="utf-8")
        broken = text.replace('"criterion": "implementation_risk"', '"criterion": "vibes"')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rubric.md"
            path.write_text(broken, encoding="utf-8")
            with self.assertRaises(RubricError):
                load_rubric(path)

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

    def test_return_band_is_scored_from_time_spent(self):
        cases = [
            ({"hours_per_week": 12}, 5),
            ({"hours_per_week": 4}, 4),
            ({"hours_per_week": 1.5}, 3),
            ({"hours_per_week": 0.5}, 2),
            ({"hours_per_week": 0.2}, 1),
        ]
        for time_spent, expected in cases:
            with self.subTest(time_spent=time_spent):
                document = minimal_intake()
                document["processes"][0]["time_spent"] = time_spent
                intake = normalize_intake(validate_intake(document))
                process = intake.processes[0]
                verdict = StubJudge().judge(
                    process, build_process_map(process, intake.business), self.rubric
                )
                self.assertEqual(verdict.scores["return_band"].score, expected)

    def test_return_band_rationale_names_the_baseline_or_its_absence(self):
        with_baseline = normalize_intake(validate_intake(minimal_intake())).processes[0]
        document = minimal_intake()
        document["processes"][0]["baseline_metric"] = None
        without = normalize_intake(validate_intake(document)).processes[0]

        for process, expected in ((with_baseline, "already tracks"), (without, "tracks no number")):
            with self.subTest(baseline=expected):
                verdict = StubJudge().judge(
                    process, build_process_map(process), self.rubric
                )
                self.assertIn(expected, verdict.scores["return_band"].rationale)

    def test_risk_rationale_names_decision_type_and_blast_radius(self):
        document = minimal_intake()
        document["processes"][0]["decision_type"] = "judgment_heavy"
        document["processes"][0]["customer_facing"] = True
        intake = normalize_intake(validate_intake(document))
        process = intake.processes[0]
        verdict = StubJudge().judge(
            process, build_process_map(process, intake.business), self.rubric
        )
        rationale = verdict.scores["implementation_risk"].rationale
        self.assertIn("judgment heavy", rationale)
        self.assertIn("seen by a customer", rationale)

    def test_judgment_heavy_scores_riskier_than_rule_based(self):
        scores = {}
        for decision_type in ("rule_based", "mixed", "judgment_heavy"):
            document = minimal_intake()
            document["processes"][0]["decision_type"] = decision_type
            document["processes"][0]["customer_facing"] = False
            intake = normalize_intake(validate_intake(document))
            process = intake.processes[0]
            verdict = StubJudge().judge(
                process, build_process_map(process, intake.business), self.rubric
            )
            scores[decision_type] = verdict.scores["implementation_risk"].score
        self.assertLess(scores["rule_based"], scores["judgment_heavy"])
        self.assertLessEqual(scores["rule_based"], scores["mixed"])
        self.assertLessEqual(scores["mixed"], scores["judgment_heavy"])

    def test_customer_facing_raises_risk_over_internal_only(self):
        scores = {}
        for customer_facing in (False, True):
            document = minimal_intake()
            document["processes"][0]["decision_type"] = "mixed"
            document["processes"][0]["customer_facing"] = customer_facing
            intake = normalize_intake(validate_intake(document))
            process = intake.processes[0]
            verdict = StubJudge().judge(
                process, build_process_map(process, intake.business), self.rubric
            )
            scores[customer_facing] = verdict.scores["implementation_risk"].score
        self.assertLess(scores[False], scores[True])

    def test_frequent_exceptions_score_riskier_than_rare(self):
        scores = {}
        for rate in ("rare", "occasional", "frequent"):
            document = minimal_intake()
            document["processes"][0]["decision_type"] = "mixed"
            document["processes"][0]["customer_facing"] = False
            document["processes"][0]["exception_rate"] = rate
            intake = normalize_intake(validate_intake(document))
            process = intake.processes[0]
            verdict = StubJudge().judge(
                process, build_process_map(process, intake.business), self.rubric
            )
            scores[rate] = verdict.scores["implementation_risk"].score
        self.assertLess(scores["rare"], scores["frequent"])
        self.assertLessEqual(scores["rare"], scores["occasional"])
        self.assertLessEqual(scores["occasional"], scores["frequent"])

    def test_absent_exception_rate_is_read_as_occasional_not_rare(self):
        absent = minimal_intake()
        absent["processes"][0]["decision_type"] = "mixed"
        absent["processes"][0]["customer_facing"] = False
        stated_rare = copy.deepcopy(absent)
        stated_rare["processes"][0]["exception_rate"] = "rare"
        stated_occasional = copy.deepcopy(absent)
        stated_occasional["processes"][0]["exception_rate"] = "occasional"

        results = {}
        for name, document in (
            ("absent", absent),
            ("rare", stated_rare),
            ("occasional", stated_occasional),
        ):
            intake = normalize_intake(validate_intake(document))
            process = intake.processes[0]
            verdict = StubJudge().judge(
                process, build_process_map(process, intake.business), self.rubric
            )
            results[name] = verdict.scores["implementation_risk"]

        self.assertEqual(results["absent"].score, results["occasional"].score)
        self.assertGreater(results["absent"].score, results["rare"].score)
        self.assertIn("exception rate was not reported", results["absent"].rationale)

    def test_risk_is_scorable_when_both_optional_fields_are_absent(self):
        document = minimal_intake()
        document["processes"][0].pop("decision_type", None)
        document["processes"][0].pop("customer_facing", None)
        intake = normalize_intake(validate_intake(document))
        process = intake.processes[0]
        verdict = StubJudge().judge(
            process, build_process_map(process, intake.business), self.rubric
        )
        score = verdict.scores["implementation_risk"]
        self.assertGreaterEqual(score.score, self.rubric.scale_min)
        self.assertLessEqual(score.score, self.rubric.scale_max)
        # The rubric forbids a 1 when neither field was reported, because a 1 claims
        # nothing can go wrong and that claim needs evidence.
        self.assertGreaterEqual(score.score, 2)
        self.assertIn("not reported", score.rationale)
        self.assertIn("conservatively", score.rationale)

    def test_absent_decision_type_is_read_as_mixed_not_rule_based(self):
        absent = minimal_intake()
        absent["processes"][0].pop("decision_type", None)
        absent["processes"][0]["customer_facing"] = False
        stated = minimal_intake()
        stated["processes"][0]["decision_type"] = "rule_based"
        stated["processes"][0]["customer_facing"] = False

        results = {}
        for name, document in (("absent", absent), ("rule_based", stated)):
            intake = normalize_intake(validate_intake(document))
            process = intake.processes[0]
            verdict = StubJudge().judge(
                process, build_process_map(process, intake.business), self.rubric
            )
            results[name] = verdict.scores["implementation_risk"].score
        self.assertGreater(results["absent"], results["rule_based"])

    def test_a_tool_name_is_not_mistaken_for_physical_work(self):
        # "shared drive" matched the verb drive, and a step that was entirely typing
        # and saving read as physical work. Whole word matching does not help when the
        # word is genuinely the same word in a different sense.
        document = minimal_intake()
        document["processes"][0]["description"] = (
            "The administrator types the completion into the spreadsheet, "
            "then saves the photos to the shared drive under the job number."
        )
        document["processes"][0]["current_tools"] = ["Excel", "shared drive"]
        intake = normalize_intake(validate_intake(document))
        process = intake.processes[0]
        verdict = StubJudge().judge(
            process, build_process_map(process, intake.business), self.rubric
        )
        score = verdict.scores["software_automatability"]
        self.assertGreaterEqual(score.score, 4)
        self.assertIn("nothing in the description", score.rationale)

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


def physical_intake() -> dict:
    """A process made almost entirely of physical steps, so it disqualifies."""
    document = minimal_intake()
    process = document["processes"][0]
    process["id"] = "yard-work"
    process["name"] = "Loading customer orders onto the trucks"
    process["description"] = (
        "The yard hand walks the racking and collects the order. "
        "Then he loads it onto the truck by hand. "
        "Then he drives it to the customer and hands it over in person."
    )
    process["people_involved"]["roles"] = ["yard hand"]
    return document


class TestDisqualification(unittest.TestCase):
    """The third mechanism: out of the ranking rather than low in it."""

    def test_a_physical_process_leaves_the_ranking(self):
        result = audit_document(physical_intake())
        self.assertEqual(result.opportunities, ())
        self.assertEqual(len(result.disqualified), 1)

        item = result.disqualified[0]
        self.assertEqual(item.trigger.raw_score, 1)
        # No score and no band, rather than a low score and a low band.
        self.assertIsNone(item.weighted_score)
        self.assertIsNone(item.band)
        self.assertEqual(item.rank, 0)

    def test_the_referral_and_reasoning_are_carried(self):
        item = audit_document(physical_intake()).disqualified[0]
        self.assertIn("robotics automation specialist", item.disqualification.referral)
        self.assertIn("no part of this process", item.disqualification.reason.lower())
        # One sentence, drawn from the description rather than written by the engine.
        self.assertTrue(item.evidence.endswith("."))
        self.assertIn(item.evidence.rstrip("."), item.process.description)

    def test_a_score_of_two_stays_in_the_ranking(self):
        document = physical_intake()
        # One step of the four is now software work, which lifts it off the floor.
        document["processes"][0]["description"] = (
            "The yard hand walks the racking and collects the order. "
            "Then he loads it onto the truck by hand. "
            "Then he types the delivery note into the system."
        )
        result = audit_document(document)
        self.assertEqual(result.disqualified, ())
        self.assertEqual(len(result.opportunities), 1)
        self.assertGreaterEqual(result.opportunities[0].criterion(
            "software_automatability").raw_score, 2)

    def test_disqualified_processes_still_carry_every_criterion(self):
        item = audit_document(physical_intake()).disqualified[0]
        self.assertEqual(
            {criterion.id for criterion in item.criteria},
            set(load_rubric().criterion_ids),
        )

    def test_a_mixed_intake_splits_into_both_lists(self):
        document = physical_intake()
        desk_work = copy.deepcopy(minimal_intake()["processes"][0])
        desk_work["id"] = "desk-work"
        desk_work["name"] = "Typing orders into the system"
        desk_work["description"] = (
            "The administrator opens the order queue, then types each order into Xero, "
            "then emails the customer to confirm."
        )
        document["processes"].append(desk_work)

        result = audit_document(document)
        self.assertEqual([o.process.id for o in result.opportunities], ["desk-work"])
        self.assertEqual([d.process.id for d in result.disqualified], ["yard-work"])
        self.assertEqual(len(result.all_processes), 2)

    def test_the_report_says_so_plainly_in_both_formats(self):
        result = audit_document(physical_intake())
        text = render_markdown(result, generated_on=date(2026, 8, 8))
        html = render_html(result, generated_on=date(2026, 8, 8))
        for name, content in (("markdown", text), ("html", html)):
            with self.subTest(output=name):
                self.assertIn(
                    "This process would benefit from collaboration with a robotics "
                    "automation specialist local to your area.",
                    content,
                )
                self.assertIn("Outside software automation", content)
                self.assertIn("Loading customer orders onto the trucks", content)

    def test_a_fully_disqualified_intake_still_reads_as_a_report(self):
        result = audit_document(physical_intake())
        text = render_markdown(result, generated_on=date(2026, 8, 8))
        self.assertIn("## In short", text)
        self.assertIn("None of the 1 process", text)
        self.assertIn("generated by an AI system", text)


class TestRationaleContract(unittest.TestCase):
    """The rules every rationale must follow, checked across every real fixture.

    Two rules, both learned from reading a report that had been generated and looked
    wrong to a person who could see the intake next to it:

    1. Describe what was read. Never claim the business's own words said nothing.
    2. Never surface the token that matched. Name the system or the problem.
    """

    # Phrasings that assert an absence of evidence in what the business wrote, or that
    # leak the matcher. Claims about an optional structured field not being filled in
    # are allowed and are not listed here.
    BANNED_FRAGMENTS = (
        "does not name",
        "does not say",
        "did not say",
        "no specific",
        "the best source named is",
        "the intake points at",
        "sign(s)",
        "handoff(s)",
        "step(s)",
        "(s)",
    )

    def rationales(self):
        for path in ALL_INTAKES:
            result = audit_document(load_intake(path))
            for opportunity in result.opportunities:
                for criterion in opportunity.criteria:
                    yield path.name, opportunity.process.id, criterion.id, criterion.rationale

    def test_no_rationale_asserts_an_absence_or_leaks_a_token(self):
        for intake, process_id, criterion_id, rationale in self.rationales():
            for banned in self.BANNED_FRAGMENTS:
                with self.subTest(intake=intake, process=process_id, criterion=criterion_id):
                    self.assertNotIn(banned, rationale.lower())

    def test_every_rationale_is_a_readable_sentence(self):
        for intake, process_id, criterion_id, rationale in self.rationales():
            with self.subTest(intake=intake, process=process_id, criterion=criterion_id):
                self.assertTrue(rationale.strip())
                self.assertTrue(rationale[0].isupper(), rationale)
                self.assertTrue(rationale.rstrip().endswith("."), rationale)

    def test_pain_rationale_describes_the_text_when_nothing_matches(self):
        document = minimal_intake()
        # Real trouble, described in words the category lists do not contain.
        document["processes"][0]["pain_description"] = (
            "The owner finds it tedious and it eats the start of every week."
        )
        result = audit_document(document)
        rationale = result.opportunities[0].criterion("pain").rationale
        self.assertIn("tedious", rationale.lower())
        for banned in ("does not name", "does not say", "no specific"):
            self.assertNotIn(banned, rationale.lower())

    def test_data_rationale_names_the_actual_system(self):
        document = minimal_intake()
        document["processes"][0]["current_tools"] = ["QuickBooks Online", "paper"]
        document["processes"][0]["data_notes"] = "It all lives in QuickBooks Online."
        result = audit_document(document)
        rationale = result.opportunities[0].criterion("data_availability").rationale
        self.assertIn("QuickBooks Online", rationale)


class TestScoring(unittest.TestCase):
    def setUp(self):
        self.rubric = load_rubric()
        self.intake = normalize_intake(load_intake(INTAKE_DIR / "corner-pharmacy.json"))

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

    def test_fixed_stub_leaves_only_cap_driven_variation(self):
        # The fixed stub returns the midpoint for everything, so any difference in
        # weighted score has to come from the engine rather than the judge. The only
        # engine rule that changes a score is the return band cap, so processes
        # should fall into exactly two groups: those with a baseline and those
        # without.
        result = score_intake(self.intake, judge=StubJudge("fixed"), rubric=self.rubric)
        by_baseline: dict[bool, set[float]] = {}
        for item in result.opportunities:
            by_baseline.setdefault(item.process.has_baseline, set()).add(item.weighted_score)
        for has_baseline, scores in by_baseline.items():
            with self.subTest(has_baseline=has_baseline):
                self.assertEqual(len(scores), 1)
        self.assertEqual(len(by_baseline), 2, "this intake should cover both cases")

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

    def test_top_risk_process_cannot_be_recommended_above_a_pilot(self):
        # A process that scores well on everything and is maximally risky. The cap
        # must hold it back, and the score itself must be left alone.
        result = audit_document(high_scoring_risky_intake())
        opportunity = result.opportunities[0]
        self.assertEqual(opportunity.criterion("implementation_risk").raw_score, 5)
        self.assertEqual(opportunity.band_before_caps.id, "strong")
        self.assertEqual(opportunity.band.id, "pilot")
        self.assertTrue(opportunity.was_capped)
        # The score is reported as calculated, not lowered to match the band.
        self.assertGreaterEqual(opportunity.weighted_score, 4.0)

    def test_roi_cap_fires_when_no_baseline_is_tracked(self):
        document = minimal_intake()
        # Plenty of time spent, so the judge would score the return band at the top.
        document["processes"][0]["time_spent"] = {"hours_per_week": 20}
        document["processes"][0]["baseline_metric"] = None
        result = audit_document(document)
        return_band = result.opportunities[0].criterion("return_band")
        self.assertEqual(return_band.judge_score, 5)
        self.assertEqual(return_band.raw_score, 2)
        self.assertTrue(return_band.was_capped)
        self.assertEqual(return_band.cap.score_before, 5)
        self.assertEqual(return_band.cap.score_after, 2)

    def test_roi_cap_does_not_fire_when_a_baseline_exists(self):
        document = minimal_intake()
        document["processes"][0]["time_spent"] = {"hours_per_week": 20}
        result = audit_document(document)
        return_band = result.opportunities[0].criterion("return_band")
        self.assertEqual(return_band.raw_score, 5)
        self.assertFalse(return_band.was_capped)

    def test_roi_cap_lowers_the_weighted_score(self):
        with_baseline = minimal_intake()
        with_baseline["processes"][0]["time_spent"] = {"hours_per_week": 20}
        without = copy.deepcopy(with_baseline)
        without["processes"][0]["baseline_metric"] = None

        high = audit_document(with_baseline).opportunities[0]
        low = audit_document(without).opportunities[0]
        # Three points of return band at its weight, which is 0.12 from rubric 1.4.0.
        weight = load_rubric().criterion("return_band").weight
        self.assertAlmostEqual(
            high.weighted_score - low.weighted_score, 3 * weight, places=4
        )

    def test_roi_cap_never_raises_a_low_score(self):
        document = minimal_intake()
        document["processes"][0]["time_spent"] = {"hours_per_week": 0.2}
        document["processes"][0]["baseline_metric"] = None
        result = audit_document(document)
        return_band = result.opportunities[0].criterion("return_band")
        self.assertEqual(return_band.raw_score, 1)
        self.assertFalse(return_band.was_capped)

    def test_only_the_return_band_is_capped_by_a_missing_baseline(self):
        document = minimal_intake()
        document["processes"][0]["baseline_metric"] = None
        result = audit_document(document)
        capped = [item.id for item in result.opportunities[0].capped_criteria]
        self.assertNotIn("pain", capped)
        self.assertNotIn("implementation_risk", capped)

    def test_planned_system_change_moves_no_number(self):
        # The field is carried and printed, and must reach no criterion. Every score,
        # every cap, and the band have to come out identical with and without it.
        without = minimal_intake()
        with_change = copy.deepcopy(without)
        with_change["processes"][0]["planned_system_change"] = {
            "description": (
                "Replacing the whole system with a regulated database that has an api, "
                "which is stressful, error prone, and losing us customers every week."
            ),
            "timeframe": "within_3_months",
        }

        plain = audit_document(without).opportunities[0]
        changed = audit_document(with_change).opportunities[0]

        self.assertEqual(
            {item.id: (item.judge_score, item.raw_score) for item in plain.criteria},
            {item.id: (item.judge_score, item.raw_score) for item in changed.criteria},
        )
        self.assertEqual(plain.weighted_score, changed.weighted_score)
        self.assertEqual(plain.band.id, changed.band.id)
        self.assertEqual(plain.band_before_caps.id, changed.band_before_caps.id)
        self.assertEqual(len(plain.applied_caps), len(changed.applied_caps))
        # The description above is stuffed with words the stub scans for, so if the
        # field ever leaked into the scored text this test would fail loudly.
        self.assertNotIn("api", changed.process.all_text.lower().split())

    def test_planned_system_change_is_carried_into_the_model(self):
        document = minimal_intake()
        document["processes"][0]["planned_system_change"] = {
            "description": "Moving the booking system.",
            "timeframe": "later_or_unknown",
        }
        process = normalize_intake(validate_intake(document)).processes[0]
        self.assertIsNotNone(process.planned_system_change)
        self.assertEqual(process.planned_system_change.description, "Moving the booking system.")
        self.assertEqual(
            process.planned_system_change.describe_timeframe(), "later, or not yet known"
        )

    def test_absent_planned_system_change_is_none(self):
        process = normalize_intake(validate_intake(minimal_intake())).processes[0]
        self.assertIsNone(process.planned_system_change)

    def test_uncapped_process_records_no_cap(self):
        result = audit_document(minimal_intake())
        opportunity = result.opportunities[0]
        self.assertFalse(opportunity.was_capped)
        self.assertEqual(opportunity.band.id, opportunity.band_before_caps.id)

    def test_verdict_from_another_rubric_version_is_refused(self):
        from ai_process_audit.scoring.score import score_process

        process = self.intake.processes[0]
        process_map = build_process_map(process, self.intake.business)
        stale = JudgeVerdict(process.id, "stub", "stub", "0.0.0-other", {})
        with self.assertRaises(ValueError):
            score_process(process, process_map, stale, self.rubric)

    def test_ranking_ties_break_predictably(self):
        from itertools import groupby

        result = score_intake(self.intake, judge=StubJudge("fixed"), rubric=self.rubric)
        # Within each block of equal scores the order must be by id, so that two runs
        # over the same intake always produce the same report.
        for score, group in groupby(
            result.opportunities, key=lambda item: item.weighted_score
        ):
            with self.subTest(score=score):
                ids = [item.process.id for item in group]
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
        self.intake = normalize_intake(load_intake(INTAKE_DIR / "boutique-landscaping.json"))
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
                self.assertIsNone(paths.pdf_renderer)
            else:
                self.assertTrue(paths.pdf.exists())
                self.assertIn(paths.pdf_renderer, {"weasyprint", "edge"})
                self.assertIsNone(paths.pdf_error)

    def test_a_capped_process_says_so_in_both_formats(self):
        result = audit_document(high_scoring_risky_intake())
        opportunity = result.opportunities[0]
        self.assertTrue(opportunity.was_capped, "the fixture is meant to trip the cap")
        text = render_markdown(result, generated_on=date(2026, 8, 1))
        html = render_html(result, generated_on=date(2026, 8, 1))
        for name, content in (("markdown", text), ("html", html)):
            with self.subTest(output=name):
                self.assertIn("capped", content.lower())
                # The band the score earned must still be visible, so the reader can
                # see what the cap overrode.
                self.assertIn(opportunity.band_before_caps.label, content)

    def test_planned_system_change_appears_as_a_sequencing_note(self):
        document = minimal_intake()
        document["processes"][0]["planned_system_change"] = {
            "description": "Replacing the booking system with a new one.",
            "timeframe": "within_3_months",
        }
        result = audit_document(document)
        text = render_markdown(result, generated_on=date(2026, 8, 1))
        html = render_html(result, generated_on=date(2026, 8, 1))
        for name, content in (("markdown", text), ("html", html)):
            with self.subTest(output=name):
                self.assertIn("Replacing the booking system with a new one.", content)
                self.assertIn("within 3 months", content)
                # The report has to say plainly that it changed nothing.
                self.assertIn("did not affect", content.lower())

    def test_no_sequencing_note_when_no_change_is_planned(self):
        text = render_markdown(self.result, generated_on=date(2026, 8, 1))
        self.assertNotIn("Sequencing note", text)

    def test_headline_leads_the_report_and_names_the_top_reason(self):
        text = render_markdown(self.result, generated_on=date(2026, 8, 1))
        html = render_html(self.result, generated_on=date(2026, 8, 1))
        top = self.result.opportunities[0]

        self.assertIn("## In short", text)
        self.assertIn(top.process.name, text)
        self.assertIn(top.strongest.rationale, text)
        self.assertIn(top.strongest.rationale, html)

        # The headline must come before the disclosure, not after it.
        self.assertLess(text.index("## In short"), text.index("generated by an AI system"))
        self.assertLess(
            html.index('class="headline"'), html.index("generated by an AI system")
        )

    def test_disclosure_is_still_present_after_the_headline(self):
        text = render_markdown(self.result, generated_on=date(2026, 8, 1))
        html = render_html(self.result, generated_on=date(2026, 8, 1))
        self.assertIn("generated by an AI system", text)
        self.assertIn("generated by an AI system", html)

    def test_step_counts_agree_with_their_verbs(self):
        # "of which 1 are done by hand" was the bug.
        for path in ALL_INTAKES:
            result = audit_document(load_intake(path))
            text = render_markdown(result, generated_on=date(2026, 8, 1))
            with self.subTest(intake=path.name):
                self.assertNotIn("which 1 are done", text)
                self.assertNotIn("1 handoffs", text)
                self.assertNotIn("1 waiting steps", text)
                self.assertNotIn("1 steps from", text)

    def test_reports_explain_the_cap_rule(self):
        text = render_markdown(self.result, generated_on=date(2026, 8, 1))
        self.assertIn("cannot be recommended above", text)

    def test_disclosure_survives_a_renderer_without_page_footers(self):
        # Edge does not render the CSS page footer, so the same facts must appear in
        # the body or an Edge rendered PDF would lose them.
        html = render_html(self.result, generated_on=date(2026, 8, 1))
        body = html.split("</style>", 1)[1]
        self.assertIn("generated by an AI system", body)
        self.assertIn("2026-08-01", body)
        self.assertIn(self.rubric.version, body)

    def test_no_em_dashes_anywhere_in_the_output(self):
        text = render_markdown(self.result, generated_on=date(2026, 8, 1))
        html = render_html(self.result, generated_on=date(2026, 8, 1))
        for name, content in (("markdown", text), ("html", html)):
            with self.subTest(output=name):
                self.assertNotIn(EM_DASH, content)
                self.assertNotIn("&mdash;", content)


class TestPdfFallback(unittest.TestCase):
    """The Edge fallback, which is what produces a PDF on Windows ARM64."""

    def setUp(self):
        self.intake = normalize_intake(validate_intake(minimal_intake()))
        self.result = score_intake(self.intake)

    def test_missing_edge_reports_plainly_rather_than_crashing(self):
        import os

        from ai_process_audit.report.render import EDGE_ENV_VAR, render_pdf_with_edge

        previous = os.environ.get(EDGE_ENV_VAR)
        os.environ[EDGE_ENV_VAR] = str(REPO_ROOT / "no-such-browser.exe")
        try:
            with tempfile.TemporaryDirectory() as directory:
                html_path = Path(directory) / "report.html"
                html_path.write_text("<p>hello</p>", encoding="utf-8")
                with self.assertRaises(PdfUnavailableError) as caught:
                    render_pdf_with_edge(html_path, Path(directory) / "report.pdf")
                self.assertIn("Edge was not found", str(caught.exception))
        finally:
            if previous is None:
                os.environ.pop(EDGE_ENV_VAR, None)
            else:
                os.environ[EDGE_ENV_VAR] = previous

    @unittest.skipIf(find_edge() is None, "Edge is not installed on this machine")
    def test_edge_renders_a_real_pdf(self):
        from ai_process_audit.report.render import render_pdf_with_edge

        with tempfile.TemporaryDirectory() as directory:
            html_path = Path(directory) / "report.html"
            html_path.write_text(
                render_html(self.result, generated_on=date(2026, 8, 1)), encoding="utf-8"
            )
            pdf_path = render_pdf_with_edge(html_path, Path(directory) / "report.pdf")
            self.assertTrue(pdf_path.exists())
            self.assertGreater(pdf_path.stat().st_size, 1000)
            with pdf_path.open("rb") as handle:
                self.assertEqual(handle.read(5), b"%PDF-")

    @unittest.skipIf(find_edge() is None, "Edge is not installed on this machine")
    def test_a_relative_destination_still_works(self):
        # Edge resolves a relative output path against its own working directory and
        # then exits zero having written nothing, which looked like a missing browser
        # rather than a bad path.
        import os

        from ai_process_audit.report.render import render_pdf_with_edge

        previous_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                html_path = Path("report.html")
                html_path.write_text("<p>relative</p>", encoding="utf-8")
                pdf_path = render_pdf_with_edge(html_path, Path("nested") / "report.pdf")
                self.assertTrue(pdf_path.exists())
                self.assertGreater(pdf_path.stat().st_size, 500)
            finally:
                os.chdir(previous_cwd)

    @unittest.skipIf(find_edge() is None, "Edge is not installed on this machine")
    def test_a_stale_pdf_is_not_mistaken_for_a_fresh_one(self):
        from ai_process_audit.report.render import render_pdf_with_edge

        with tempfile.TemporaryDirectory() as directory:
            html_path = Path(directory) / "report.html"
            html_path.write_text("<p>fresh</p>", encoding="utf-8")
            pdf_path = Path(directory) / "report.pdf"
            pdf_path.write_bytes(b"stale content that is not a pdf")
            render_pdf_with_edge(html_path, pdf_path)
            with pdf_path.open("rb") as handle:
                self.assertEqual(handle.read(5), b"%PDF-")

class TestNoEmDashesInSource(unittest.TestCase):
    """The no em dash rule applies to the repository, not only to reports."""

    def test_repository_text_has_no_em_dashes(self):
        patterns = ("*.py", "*.md", "*.json", "*.j2", "*.txt", "*.toml", "*.yml", "*.yaml")
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
            code = cli_main(["score", str(INTAKE_DIR / "bean-and-bark-roasters.json"), "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["judge_mode"], "stub")
        self.assertFalse(payload["rubric_approved"])


if __name__ == "__main__":
    unittest.main()
