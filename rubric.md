# Automation Opportunity Rubric

**Version: 1.2.0-draft**
**Status: DRAFT. Not approved. Weights and criteria are proposals awaiting review.**

This file is the single source of truth for scoring. The engine reads the machine
readable block at the bottom of this file. If you change a definition here, change
the matching entry in that block, and raise the version number.

## How scoring works

Every process in an intake is scored against six criteria. Each criterion gets an
integer score from 1 to 5 and a one sentence rationale. Scores are combined into a
single number from 1.0 to 5.0 using the weights below, and that number maps to a
recommendation band.

Five of the six criteria point the same way: a higher score means a better
automation candidate. One criterion, implementation risk, points the other way. A
score of 5 on implementation risk means the work is risky, so the engine inverts it
before combining. The inversion is applied in code, not by the person or model doing
the scoring. When you score implementation risk, score the risk itself.

## Scoring discipline

Two rules keep the scores comparable across businesses:

1. Score what the intake actually says. If the intake does not contain the
   information a criterion asks about, score it 3 and say so in the rationale.
   Do not assume a well run business, and do not assume a badly run one.
2. Score the process as it is described today, not the process as it might be after
   a cleanup project.

## Criteria

### 1. Pain (weight 0.20)

How much trouble the process causes the people who run it today. Look at the pain
description, not just the process description.

| Score | Anchor |
| --- | --- |
| 1 | Nobody complains about it. It runs quietly and errors are rare. |
| 2 | Mild irritation. Occasional rework, no real consequence when it goes wrong. |
| 3 | A recognised annoyance. Staff mention it, and mistakes cost some rework. |
| 4 | A regular source of stress or errors that reach customers or the books. |
| 5 | A named problem that the business is actively losing money, staff, or customers over. |

### 2. Frequency (weight 0.15)

How often the process runs. Taken from the frequency field and normalised to runs
per year. Frequency is scored separately from volume because a process that runs
once a month over a thousand records has a different automation shape than one that
runs fifty times a day over one record.

| Score | Anchor |
| --- | --- |
| 1 | Runs once a year or less. |
| 2 | Runs a few times a year, roughly quarterly. |
| 3 | Runs monthly, or on an irregular schedule averaging about monthly. |
| 4 | Runs weekly or several times a week. |
| 5 | Runs daily or continuously through the working day. |

### 3. Volume (weight 0.15)

How many items pass through the process in a year. An item is whatever the process
handles: an invoice, a booking, an application, a support message.

| Score | Anchor |
| --- | --- |
| 1 | Under 50 items a year. |
| 2 | 50 to 250 items a year. |
| 3 | 250 to 1,000 items a year. |
| 4 | 1,000 to 10,000 items a year. |
| 5 | Over 10,000 items a year. |

### 4. Data availability (weight 0.20)

Whether the information the process needs already exists somewhere a computer can
read it. This is the criterion that most often kills an otherwise good candidate.

| Score | Anchor |
| --- | --- |
| 1 | The information lives in people's heads, on paper, or in conversation. Nothing is recorded in a system. |
| 2 | Recorded, but in free text or scanned images with no consistent structure, and often incomplete. |
| 3 | Recorded in spreadsheets or email in a mostly consistent shape, readable with effort and cleanup. |
| 4 | Held in a real system with structured fields, though export or access needs manual steps. |
| 5 | Held in a system with structured fields and a documented way to read it programmatically. |

### 5. Implementation risk (weight 0.15, inverted)

What could go wrong if the automation is wrong, and how hard it is to catch. Higher
means riskier. Score the risk, not the desirability. The engine inverts this.

Two intake fields feed this criterion directly.

**decision_type** is how much of the work is judgement. A `rule_based` process can be
checked against its own rules, so a wrong output is findable. A `judgment_heavy`
process cannot, because there is no rule to check it against, and two experienced
people might disagree about whether an output was even wrong. That is what makes it
risky to automate, so `judgment_heavy` pushes the score up and `rule_based` pulls it
down.

**customer_facing** is the blast radius. An error caught inside the business is
rework, which costs time. An error that reaches a customer is a relationship cost,
which is not recovered by fixing the error. The same mistake is worth more when
somebody outside sees it.

| Score | Anchor |
| --- | --- |
| 1 | Rule based, internal only, easily reversed, and a mistake is obvious immediately. |
| 2 | Rule based or mixed, internal, reversible, and a mistake would be caught within a day by normal checks. |
| 3 | Mixed judgement, or customer facing but low consequence, where a mistake takes a few days to surface and costs rework. |
| 4 | Judgement heavy, or customer facing where an error damages a relationship, or it moves money or writes to the books, and errors are hard to spot. |
| 5 | Legally binding, safety related, or regulated, where a single wrong output is a serious event, whoever sees it. |

**When these fields are absent**, score conservatively rather than optimistically,
and say in the rationale that the field was not reported. Absent `decision_type` is
read as `mixed`, not as `rule_based`, because a business that has not thought about
whether a process is rule based usually has not written the rules down. Absent
`customer_facing` is read as customer facing when the process description mentions
customers at all, and as internal only when it does not. Never score 1 on a process
where both fields are missing, because a 1 is a claim that nothing can go wrong, and
that claim needs evidence.

### 6. Return band (weight 0.15)

The size of the return relative to the effort. This is a band, not a forecast. It is
deliberately coarse because a precise number here would be false precision.

Two intake fields feed this criterion directly.

**time_spent** is the denominator, and it is required for exactly that reason. It is
given as hours a week, or as minutes per item which is multiplied by the yearly
volume, and the engine turns either into hours a year over a 48 week working year.
Score the band from that figure.

| Score | Anchor | Roughly |
| --- | --- | --- |
| 1 | Saves under an hour a month. The build would take longer to pay back than it is worth. | under 12 hours a year |
| 2 | Saves a few hours a month, or removes a minor recurring error. | 12 to 50 hours a year |
| 3 | Saves roughly a day a month, or removes an error that costs real money a few times a year. | 50 to 150 hours a year |
| 4 | Saves several days a month, or frees a person from work they were hired to stop doing. | 150 to 500 hours a year |
| 5 | Saves a role's worth of time, or removes an error class that is currently costing the business regularly. | over 500 hours a year |

**baseline_metric** is whether the business already tracks any number for this
process. It is the difference between a return you can show and a return you can
only assert.

**A process with no baseline_metric is capped at 2 on this criterion, whatever the
time figure says.** Not because the return is small, but because nobody would be able
to demonstrate it. A process consuming six hundred hours a year with nothing measured
is not a strong return, it is an unproven one, and the honest thing is to say that
the first piece of work is to start counting. The engine applies this cap, and the
report states plainly that no baseline exists so the return cannot be evidenced.

The cap is not a judgement about the process. It is a judgement about what can be
claimed on the evidence available, which is the same standard the rest of this rubric
applies.

## Recommendation bands

The weighted score maps to one of four bands. The band is what goes at the top of
the report, and the number is shown next to it so the reader can see how close a
call it was.

| Band | Weighted score | Meaning |
| --- | --- | --- |
| Strong candidate | 4.00 and above | Worth scoping properly now. |
| Worth a pilot | 3.00 to 3.99 | Worth a small time boxed test before committing. |
| Watch list | 2.00 to 2.99 | Not now. Revisit if volume grows or the data improves. |
| Not a fit | Below 2.00 | Automation is not the answer to this one. |

## Two kinds of cap

There are two caps in this rubric and they do different jobs.

A **criterion cap** limits one score, before the weighting. The return band cap
described above is one: no baseline metric means the return criterion cannot go
above 2. It changes the weighted score, because it changes an input to it.

A **band cap** limits the recommendation, after the weighting. The implementation
risk cap below is one. It never changes the score, only what may be recommended on
the strength of it.

Both are applied by the engine rather than by the judge, and both are reported.

## Band caps

A weighted average can be talked round by its own arithmetic. A process that is
frequent, high volume, painful, and well recorded will score above 4.00 even when
getting it wrong would be a serious event, because five good criteria outvote one
bad one. That is the right behaviour for a score and the wrong behaviour for a
recommendation.

So one rule sits outside the arithmetic:

**A process scoring 5 on implementation risk cannot be recommended above Worth a
pilot, whatever its weighted score.**

The score itself is not changed. It is reported exactly as calculated, and the
report says the cap was applied and why. A reader who disagrees can see the number
the cap overrode.

This is a cap and not a weight on purpose. Raising the weight of implementation risk
would move every process a little, including the ones where risk is a 2 and the
weight change is just noise. The cap does nothing at all until risk hits the top of
the scale, which is where the anchor says a single wrong output is a serious event.
That is a statement about what may be recommended, not a statement about how much
risk counts, and the two should not be confused by hiding one inside the other.

## Decisions taken on the open questions

The first draft listed four questions. They are resolved below with the simplest
rule that can be defended, on the principle that a rubric nobody can explain is
worse than one that is slightly wrong in a way anyone can see.

**These weights do not change again except on evidence from the gold set.** Not on
argument, not on a reading of a single report that looks off, and not because a
number feels low. If a weight is wrong, the failure analysis will show it as a
criterion that consistently disagrees with the labels in one direction, and that is
what a change has to cite.

1. **Pain and data availability stay level at 0.20 each.** The case for lifting data
   availability is real, but it rests on a claim about which criterion best predicts
   a successful build, and nothing here has measured that yet. Leave them level and
   let the failure analysis argue for a change.
2. **Implementation risk stays at 0.15**, and the band cap above handles the case
   that prompted the question.
3. **Frequency and volume both stay at 0.15.** They are correlated but not the same:
   a monthly process over a thousand records and a daily process over one record
   need different builds, and separating them is what makes that visible. If the
   gold set shows them moving together, merge them into one criterion at 0.30 rather
   than quietly shading one down.
4. **Return band stays judged rather than computed.** Computing it from the others
   would make it a restatement of scores already counted, which is real double
   counting, unlike question 3. It is scored only on time spent and the cost of
   errors the process produces today, and the judge is told not to look at the other
   criteria when scoring it.

## Change log

| Version | Date | Change |
| --- | --- | --- |
| 1.0.0-draft | 2026-08-02 | First draft. Not approved for use. |
| 1.1.0-draft | 2026-08-02 | Added the implementation risk band cap. Resolved the four open questions, keeping all weights as drafted. Weights now change only on gold set evidence. Still not approved. |
| 1.2.0-draft | 2026-08-02 | Return band anchors now read time spent and baseline metric, with a criterion cap at 2 where nothing is tracked. Implementation risk anchors now read decision type and customer facing, with stated conservative defaults when either is absent. Weights unchanged. Risk band cap unchanged. Still not approved. |

## Machine readable specification

The engine parses the block below. Nothing else in this file is read by code.

```rubric-spec
{
  "version": "1.2.0-draft",
  "approved": false,
  "scale": {"min": 1, "max": 5},
  "criteria": [
    {
      "id": "pain",
      "label": "Pain",
      "weight": 0.20,
      "direction": "higher_is_better",
      "question": "How much trouble does this process cause the people who run it today?"
    },
    {
      "id": "frequency",
      "label": "Frequency",
      "weight": 0.15,
      "direction": "higher_is_better",
      "question": "How often does this process run?"
    },
    {
      "id": "volume",
      "label": "Volume",
      "weight": 0.15,
      "direction": "higher_is_better",
      "question": "How many items pass through this process in a year?"
    },
    {
      "id": "data_availability",
      "label": "Data availability",
      "weight": 0.20,
      "direction": "higher_is_better",
      "question": "Does the information this process needs already exist somewhere a computer can read?"
    },
    {
      "id": "implementation_risk",
      "label": "Implementation risk",
      "weight": 0.15,
      "direction": "higher_is_worse",
      "question": "What could go wrong if the automation is wrong, and how hard would it be to catch?"
    },
    {
      "id": "return_band",
      "label": "Return band",
      "weight": 0.15,
      "direction": "higher_is_better",
      "question": "How large is the likely return relative to the effort of building it?"
    }
  ],
  "bands": [
    {"id": "strong", "label": "Strong candidate", "min_score": 4.00},
    {"id": "pilot", "label": "Worth a pilot", "min_score": 3.00},
    {"id": "watch", "label": "Watch list", "min_score": 2.00},
    {"id": "not_a_fit", "label": "Not a fit", "min_score": 0.00}
  ],
  "band_caps": [
    {
      "criterion": "implementation_risk",
      "at_or_above": 5,
      "max_band": "pilot",
      "reason": "Getting this wrong is a serious event, so it cannot be recommended above a time boxed pilot however well it scores elsewhere."
    }
  ],
  "criterion_caps": [
    {
      "criterion": "return_band",
      "condition": "no_baseline_metric",
      "max_score": 2,
      "reason": "The business tracks no number for this process today, so any saving could be claimed but not shown. The first piece of work here is to start counting."
    }
  ]
}
```
