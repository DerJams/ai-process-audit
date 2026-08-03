# AI Process Audit

A decision engine that takes a structured description of a small business and its
processes, and produces a ranked set of automation opportunities with a one page
audit report.

It is a pipeline with exactly one judgement step, and that step is measured against
hand written labels. The rest is arithmetic.

**Status: early. The rubric is a draft, the judge is a stub, and no gold labels have
been written yet, so there are no results to report. The Honest results section
below is empty on purpose and stays empty until there is something real to put in
it.**

---

## The problem

Most small businesses that ask about AI automation are not asking a technology
question. The technology to read an email, extract a number, and put it in a system
has existed for years and is cheap. What nobody can tell them is which of their
fifteen daily annoyances is worth automating and which will waste four months.

That is a judgement problem. It depends on how often the process runs, how many
items go through it, whether the information the process needs exists anywhere a
computer can read, what happens if the automation is wrong, and whether the return
is worth the build. Those are not hard questions individually. They are hard to ask
consistently across fifteen processes and three businesses without the answers
drifting.

The failure mode this project is built against is the confident audit. A consultant
or a chatbot produces a document full of specific claims, delivered in a tone that
suggests analysis, where the underlying judgement was made once, quickly, and is not
written down anywhere that anyone can check. The output looks the same whether the
judgement was good or bad.

So the design goal is not "score processes accurately". It is: make the judgement
explicit, make it the same judgement every time, and make it possible to find out
whether it is any good.

## What it does

Give it an intake file describing a business and its processes. It returns a ranked
list of automation opportunities, each with a score from 1.0 to 5.0, a
recommendation band, a per criterion breakdown with a sentence of reasoning behind
every score, and an inferred process map. It writes that as markdown and as a one
page PDF.

```bash
python -m ai_process_audit report eval/intakes/corner-pharmacy.json --out out/pharmacy
```

## Architecture

Six stages. Five are deterministic. One is not, and it is the only one that may ever
call a model.

```
intake.json
    |
    | 1. validate        jsonschema against intake/schema.json
    |                    every error reported at once, not just the first
    v
    | 2. normalise       into the internal model in model/models.py
    |                    frequency and volume become yearly figures here
    v
    | 3. map             processmap/steps.py splits the description into steps
    |                    processmap/mermaid.py renders them as Mermaid text
    v
    | 4. score  <------  scoring/judge.py, THE ONLY MODEL CALL IN THE SYSTEM
    |                    one score and one sentence per criterion
    |                    currently a stub, no API call exists in this repository
    v
    | 5. aggregate       scoring/score.py applies weights and direction, then ranks
    |                    no judgement here, only arithmetic
    v
    | 6. report          report/render.py writes markdown, HTML, and PDF
    v
report.md, report.html, report.pdf, maps/*.mmd
```

The layout:

```
ai_process_audit/
  intake/schema.json      the contract for what an intake must contain
  intake/validator.py     validation, reports every problem in one pass
  model/models.py         the internal process model, frozen dataclasses
  model/normalize.py      raw intake to internal model, with stated assumptions
  processmap/steps.py     rule based step inference from free text
  processmap/mermaid.py   step list to Mermaid flowchart text
  scoring/judge.py        the one judgement step, stubbed
  scoring/rubric.py       reads the rubric out of rubric.md
  scoring/score.py        weighting, banding, ranking
  report/render.py        report generation
  report/templates/       report.html.j2 and report.md.j2
  pipeline.py             the six stages, in order, in twenty lines
  cli.py                  validate, map, score, report, rubric
rubric.md                 the criteria and weights, human readable and machine read
eval/                     the validation harness and the gold labels
tests/                    unittest, no third party test dependency
```

Two decisions are worth calling out.

**The rubric lives in markdown.** `rubric.md` contains the anchor tables a person
reads and argues with, and one fenced `rubric-spec` block that the code parses. The
definitions humans discuss and the definitions the code applies cannot drift apart,
because they are in the same file. Change a weight in the prose without changing the
block and the tests notice.

**Direction is applied by the engine, not the judge.** Five criteria point the same
way, where a higher score is a better candidate. Implementation risk points the other
way. The judge always scores a criterion in its natural direction, so risk is scored
as risk, and `scoring/rubric.py` inverts it during aggregation. This removes a whole
class of labelling error, since a human labeller and a model judge would both
otherwise have to remember to flip one criterion out of six.

**Two rules sit outside the weighted average.** A weighted average lets five good
criteria outvote one bad one, which is right for a score and wrong for a
recommendation. So the rubric has two kinds of cap, both applied by the engine and
both reported.

A **band cap** limits the recommendation after the weighting, and never changes the
score. A process scoring 5 on implementation risk cannot be recommended above Worth a
pilot, whatever it scored. The band it earned is printed next to the capped one, so
the reader can see what was overridden. This is a cap rather than a heavier weight on
purpose: raising the weight would nudge every process, including the ones where risk
is a 2 and the change is noise, whereas the cap does nothing until risk reaches the
top of the scale.

A **criterion cap** limits one score before the weighting, and therefore does change
the weighted score. There is one: a process with no `baseline_metric` cannot score
above 2 on return band. Not because the return is small, but because nobody could
demonstrate it. A process burning six hundred hours a year with nothing measured is
not a strong return, it is an unproven one, and the honest recommendation is to start
counting first. The report states that no baseline exists so the return cannot be
evidenced, and shows the score the cap overrode.

Both caps are declared as data in the `rubric-spec` block rather than written into
the scorer, and a criterion cap may only use a condition the engine knows how to
check, so a typo fails when the rubric loads instead of silently never firing.

## Intake fields

The schema is `intake/schema.json`, currently version 1.2.0. The rule behind it: the
intake only asks what a business owner can answer from memory, in one sitting,
without opening a system or asking their accountant. That is why there are no cost
fields, no case identifiers, no event logs, and no org chart. A field nobody can
answer accurately is worse than no field, because it produces a number that looks
like evidence.

Per business:

| Field | Required | What it is |
| --- | --- | --- |
| `industry` | yes | What the business does, in its own words |
| `headcount` | yes | Everyone, including part time and owners |
| `tools_in_use` | yes | Software the business already pays for or relies on |
| `name`, `notes` | no | Trading name, and anything that does not fit the fields above |

Per process:

| Field | Required | What it is | Read by |
| --- | --- | --- | --- |
| `name` | yes | What staff call it | report |
| `description` | yes | How it runs today, start to finish, in order | process map |
| `frequency` | yes | How often it runs, from a fixed list | frequency |
| `volume` | yes | Count, unit, and period, normalised to items a year | volume |
| `people_involved` | yes | How many people, their roles, optional hours per run | process map, report |
| `time_spent` | yes | Hours a week or minutes per item, at least one | return band |
| `current_tools` | yes | Tools used in this process. Paper and phone are valid entries | data availability, process map |
| `pain_description` | yes | What goes wrong, what it costs, who feels it | pain |
| `decision_type` | no | `rule_based`, `mixed`, or `judgment_heavy` | implementation risk |
| `baseline_metric` | no | Any number the business tracks for this today. Null means none | return band, and its cap |
| `customer_facing` | no | Whether a mistake is seen by a customer. The only place customer reach is recorded | implementation risk |
| `risk_flags` | no | Money, regulated data, safety, or legal weight. Customer reach is not here, it has its own field | implementation risk |
| `data_notes` | no | Where the information lives and what shape it is in | data availability |
| `owner` | no | Who is accountable | report |
| `id` | no | Stable identifier. Gold labels key on it, so set it if the intake will be labelled | eval harness |

Two of these carry more weight than their size suggests. `time_spent` is required
because it is the denominator behind the whole return band criterion, and a return
estimated without it is a guess. `baseline_metric` is optional but consequential:
leaving it null is a valid and common answer, and it caps the return band at 2,
because a saving nobody can measure is a saving nobody can show.

An optional field being absent never means the answer is favourable. Absent
`decision_type` is read as `mixed` rather than `rule_based`, and implementation risk
is never scored 1 when both `decision_type` and `customer_facing` are missing, since a
1 claims nothing can go wrong and that claim needs evidence. The rationale says which
fields were missing and that the score was made conservatively.

## Why a pipeline and not an agent

This system could have been an agent. It would have been faster to write. A loop
with tools for reading the intake, thinking about processes, and writing a report
would produce plausible output on the first run. That is exactly the problem.

The case against, in the order that matters:

**You cannot measure a moving target.** The point of this project is to find out
whether the judgements are any good. That requires the same input to produce the
same judgement, so that when a score disagrees with a label you learn something
about the rubric instead of something about that run. An agent that decides its own
path produces a different decomposition each time. Every disagreement then has two
possible causes and no way to tell them apart.

**Most of the work is not judgement.** Validating a schema, converting weekly volume
into yearly volume, applying six weights, sorting a list. A model doing arithmetic is
slower, more expensive, and occasionally wrong, and there is no upside because the
correct answer is defined. Handing these to a model does not make the system smarter,
it makes the failures harder to locate.

**Failures need an address.** When a pipeline produces a bad result, the bad stage is
identifiable: the map is wrong, or the score is wrong, or the weighting is wrong.
When an agent produces a bad result, the answer is "something in the trajectory". The
first is a bug report. The second is a shrug.

**The judgement is small and repeated.** Six criteria per process, each answerable in
a sentence. That shape is well suited to a narrow call with a fixed rubric and badly
suited to open ended reasoning, which will produce six subtly different standards
across fifteen processes.

**One place to look.** Because there is exactly one model call, the questions "what
was the model asked" and "what did it say" have one answer each, in one file, and the
answer appears in the report as a rationale the reader can disagree with.

Where an agent would genuinely help is the part deliberately out of scope: the
conversation that collects the intake, where the next question really does depend on
the last answer. That is a different problem with a different shape, and the boundary
between the two is the schema in `intake/schema.json`.

None of this is an argument that agents are bad. It is an argument that this
particular problem is a scoring problem wearing a trench coat, and scoring problems
want pipelines.

## How validation works

An engine that produces confident output nobody has checked is the thing this
project exists to avoid, so the validation harness is not an afterthought.

The method:

1. A person writes gold labels by hand for every process in the synthetic intakes,
   scoring all six criteria against the anchors in `rubric.md`, without looking at
   engine output first.
2. `eval/harness.py` runs the engine over the same intakes and compares.
3. It reports exact agreement, agreement within one point, the mean signed error per
   criterion, band agreement, and pairwise ranking agreement.
4. It writes `eval/out/failure_analysis.md` listing every disagreement with the
   engine's rationale printed next to the label, so a wrong score can be told apart
   from a right score reached badly.

Two rules are enforced in code rather than left to good intentions. The harness has
no path that writes to a gold file, and no label anywhere may be generated. A gold
file records the rubric version it was written against, and the harness refuses to
compare labels from one rubric version against scores from another.

The rule that matters most is not enforceable in code: **gold labels are never edited
to make the engine agree.** If a label is right and the engine disagrees, the engine
is wrong, and that disagreement is the finding.

The metrics and their limits are set out in `eval/gold/README.md`. The short version
is that agreement with one labeller measures agreement with one labeller. It does not
measure whether the rubric picks automation projects that succeed. Only building some
and watching what happens can answer that, and this repository will not pretend
otherwise.

```bash
python -m eval.make_gold_template eval/intakes/corner-pharmacy.json  # writes nulls
# fill it in by hand
python -m eval.harness
```

### Who wrote what, and why it matters

The synthetic intakes were **generated by Gemini 3.1 Pro**. The judge runs on
**Claude**. Intake authorship and scoring authorship are therefore different model
families, which is deliberate.

If one model both invented the businesses and scored them, part of any agreement
figure would be that model recognising its own habits: the phrasing it reaches for,
how it describes a painful process, how much detail it gives when data is messy. That
inflates the number without improving the engine. Having a different family write the
intakes does not eliminate the effect, but it does mean the engine is reading prose in
someone else's voice.

The gold labels are written by a person, by hand, and no model writes a label. That
rule is enforced in code and described in `eval/gold/README.md`.

### The limitation that remains

**These are still synthetic businesses.** Nobody visited them, because they do not
exist. Every figure was invented to be plausible rather than observed, and plausible
is not the same as real. Synthetic intakes are systematically tidier than real ones in
ways that are hard to see from the inside: a real owner contradicts himself, forgets a
process entirely, gives a number he has not checked in three years, and describes the
thing that annoys him rather than the thing that costs the most.

**The gold set only becomes fully real once actual prospect intakes accumulate.**
Until then, agreement measured here says the engine behaves consistently against one
labeller's judgement on invented cases. It does not say the engine picks good
automation candidates for real businesses. Those are different claims, and this
project should keep making only the first one until it has the evidence for the
second.

## Honest results

Nothing yet.

No gold labels have been written, so no agreement has been measured. The stub judge
produces scores and the pipeline runs end to end, which demonstrates that the
plumbing works and demonstrates nothing at all about whether the judgements are any
good.

This section will be filled in after labelling, and it will report what was found
rather than what would be nice to report. If exact agreement is 40 percent, it will
say 40 percent.

To be filled in:

| Measure | Result | Labels compared |
| --- | --- | --- |
| Exact agreement | not measured | 0 |
| Agreement within one point | not measured | 0 |
| Band agreement | not measured | 0 |
| Ranking pair agreement | not measured | 0 |

Three intakes are waiting to be labelled, at 30 labels each, so the first run will
compare 90 labels against rubric 1.2.0-draft.

Per criterion agreement, which criterion is weakest, which direction the engine
leans, and what was changed as a result: all to follow.

## Getting started

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Then:

```bash
python -m ai_process_audit validate eval/intakes/corner-pharmacy.json
python -m ai_process_audit rubric
python -m ai_process_audit map eval/intakes/boutique-landscaping.json
python -m ai_process_audit score eval/intakes/bean-and-bark-roasters.json
python -m ai_process_audit report eval/intakes/corner-pharmacy.json --out out/pharmacy
python -m unittest discover -s tests
```

Three synthetic intakes are included, covering a coffee roaster, a landscaping and
garden design firm, and an independent pharmacy. Fifteen processes in total. Seven
track no baseline metric and two report neither `decision_type` nor
`customer_facing`, so the conservative scoring paths are exercised by real fixtures
rather than only by unit tests.

They were **generated by Gemini 3.1 Pro**, not by Claude. Provenance is in
[eval/intakes/PROVENANCE.md](ai-process-audit/eval/intakes/PROVENANCE.md).

Three content problems were found in the first generation, sent back to Gemini rather
than patched by hand, and fixed there. All three had passed schema validation, which
is the useful lesson: a schema checks shape, not sense. The record is in
[docs/known_issues_intakes.md](ai-process-audit/docs/known_issues_intakes.md), kept
out of the intakes folder and written without reference to how anything would score,
so that reading it does not compromise a blind labelling pass.

## Known limits

- **The judge is a stub.** It applies fixed thresholds to the intake. It is not a
  judgement, and any agreement figure produced against it measures the thresholds.
  Live judging is not implemented, and `scoring/judge.py` contains no network code.
  Turning it on is a reviewed change, not a setting.
- **The rubric is a draft.** Version 1.2.0-draft is marked unapproved, and the
  engine prints that warning on every report. The four open questions from the first
  draft are now resolved in `rubric.md`, with all weights kept as drafted. Weights
  change from here only on evidence from the gold set, not on argument.
- **Process maps are shallow.** Steps are inferred by splitting the description on
  sentence and ordering boundaries and matching known verbs. Branches, loops, and
  parallel paths are not inferred. A step is attributed to a person only when that
  person is the subject at the start of the fragment, so many steps are left
  unattributed, which is the honest answer rather than a guessed one.
- **PDF output has two renderers and both are optional.** WeasyPrint is the intended
  one, and it needs Pango and GObject. On Windows that means the GTK runtime, and on
  Windows ARM64 that runtime does not exist, so WeasyPrint cannot render there at
  all. When it is unavailable the engine falls back to headless Microsoft Edge, which
  every Windows machine already has, so this adds no dependency. Edge does not support
  the CSS that puts a running footer on the page, so an Edge rendered PDF has no page
  footer. Everything the footer carries is repeated in the body, so nothing is lost.
  If neither renderer works, the markdown and HTML are still written and the failure
  is reported plainly. Set `AI_PROCESS_AUDIT_EDGE` to point at a specific Chromium
  binary if yours is installed somewhere unusual.
- **The WeasyPrint path is only exercised in CI.** Since it cannot run on the machine
  this was built on, `.github/workflows/ci.yml` runs the whole pipeline on
  ubuntu-latest with the native libraries installed, checks that a non empty PDF was
  produced for every synthetic intake, and uploads the reports as build artifacts.
  The report command is built to degrade rather than fail, so CI checks for the PDF
  file itself rather than trusting the exit code.
- **Nothing in a report is verified.** Volumes, times, and pain all come from the
  intake exactly as given.

## Scope

In scope: the engine. Intake validation, normalisation, process mapping, scoring,
reporting, and the evaluation harness.

Out of scope for now: the chatbot that collects the intake, the website embed, and
any CRM integration. The boundary between this engine and all of those is
`intake/schema.json`. Anything that can produce a file matching that schema can drive
this engine.

## Licence

MIT.
