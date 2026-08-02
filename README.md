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
python -m ai_process_audit report eval/intakes/redwood-plumbing.json --out out/redwood
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
python -m eval.make_gold_template eval/intakes/redwood-plumbing.json  # writes nulls
# fill it in by hand
python -m eval.harness
```

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
python -m ai_process_audit validate eval/intakes/redwood-plumbing.json
python -m ai_process_audit rubric
python -m ai_process_audit map eval/intakes/northgate-lettings.json
python -m ai_process_audit score eval/intakes/kilner-food-wholesale.json
python -m ai_process_audit report eval/intakes/redwood-plumbing.json --out out/redwood
python -m unittest discover -s tests
```

Three synthetic intakes are included, covering a plumbing and heating firm, a letting
agency, and a food wholesaler. They are written to be realistic rather than tidy:
inconsistent tooling, estimates the business admits are guesses, processes that are
poor automation candidates, and knowledge that exists only in one person's head. They
are for labelling and testing, and they describe invented businesses.

## Known limits

- **The judge is a stub.** It applies fixed thresholds to the intake. It is not a
  judgement, and any agreement figure produced against it measures the thresholds.
  Live judging is not implemented, and `scoring/judge.py` contains no network code.
  Turning it on is a reviewed change, not a setting.
- **The rubric is a draft.** Version 1.0.0-draft is marked unapproved, and the
  engine prints that warning on every report. The open questions about the weights
  are listed at the bottom of `rubric.md`.
- **Process maps are shallow.** Steps are inferred by splitting the description on
  sentence and ordering boundaries and matching known verbs. Branches, loops, and
  parallel paths are not inferred. A step is attributed to a person only when that
  person is the subject at the start of the fragment, so many steps are left
  unattributed, which is the honest answer rather than a guessed one.
- **PDF output needs native libraries.** WeasyPrint needs Pango and GObject. On
  Windows that means the GTK runtime, and on Windows ARM64 that runtime is not
  available, so the PDF cannot be produced on those machines. The markdown and HTML
  are always written, the failure is reported plainly, and the HTML prints to PDF
  from any browser.
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
