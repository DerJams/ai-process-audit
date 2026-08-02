# Gold labels

This folder holds hand written labels. They are the only thing in this repository
that says whether the engine is any good.

## The rules

1. **A person writes every label.** No label in this folder may be produced by a
   model, copied from engine output, or filled in by a script. The template
   generator writes nulls and nothing else.
2. **Labels are never edited to raise agreement.** If the engine disagrees with a
   label and the label is right, the engine is wrong and that is the finding. A
   label may only change if the labeller decides the label itself was a mistake, and
   when that happens it is worth writing down why in the notes.
3. **Label before you look.** Score the processes without reading the engine output
   for them. Once you have seen a score it is very hard to unsee it.
4. **Labels belong to a rubric version.** Each gold file records the rubric version
   it was written against, and the harness refuses to compare across versions. If
   the rubric changes in a way that moves an anchor, the labels have to be redone.

## How to write a set

Generate an empty template:

```bash
python -m eval.make_gold_template eval/intakes/redwood-plumbing.json
```

That writes `eval/gold/redwood-plumbing.gold.json` with every score set to null.
Then, with rubric.md open beside you:

1. Set `labelled_by` to your name. The harness refuses a file without it.
2. Set `labelled_on` to the date.
3. Read one process in the intake, then score all six criteria for it before moving
   to the next. Scoring one criterion across every process invites you to rank
   rather than to score against the anchors.
4. Score implementation risk as risk. Higher is riskier. The engine does the
   inversion, so do not invert it yourself.
5. Where the intake does not say, score 3 and write why in `notes`. The rubric asks
   the engine to do the same, so doing anything else here measures the wrong thing.
6. Leave a score as null if you genuinely have not decided. Nulls are counted and
   reported, not silently treated as agreement.

A note is worth writing whenever the score was a close call, because that is
exactly the case where a disagreement later is interesting rather than a bug.

## What the harness measures

Run it once labels exist:

```bash
python -m eval.harness
```

It writes `eval/out/agreement.json` and `eval/out/failure_analysis.md`.

- **Exact agreement**: the share of labels where the engine picked the same number.
  This is the honest headline and it will be low. Two careful people scoring the
  same process against the same anchors often differ by a point.
- **Agreement within one point**: the share within one point either way. This is the
  more useful number, because a one point gap on a five point scale rarely changes
  what anyone would do next.
- **Mean signed error**: which way the engine leans when it is wrong. A criterion
  that is consistently one point high is a fixable rubric or prompt problem. One
  that is randomly two points out in both directions is a broken criterion.
- **Band agreement**: whether the engine put the process in the same recommendation
  band the labels imply. Only counted for processes where every criterion is
  labelled.
- **Ranking pair agreement**: for every pair of processes in an intake, whether the
  engine ordered them the same way the labels do. This is the closest measure to
  what the product actually claims, because the output is an order.

## What none of this proves

Agreement with one labeller measures agreement with one labeller. It does not
measure whether the rubric picks good automation candidates, and it never will. To
learn that, someone has to build one of these and find out what actually happened,
which is a slower and more expensive question. Say so plainly whenever the numbers
are quoted.

A second labeller scoring the same intakes would let the labels be checked against
each other, and that figure is worth having before treating the engine numbers as
meaningful. Whatever two people agree on is roughly the ceiling any engine can be
expected to hit.
