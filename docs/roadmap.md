# Roadmap

What is deliberately not built yet, and why. Anything here is a decision that has
been taken, not a wish list.

## Derived complexity indicator

**Status: not built. The first input is being collected.**

`planned_system_change` landed in intake schema 1.3.0. It is carried through the
pipeline and printed in the report as a sequencing note. It feeds no criterion, it
changes no score, cap, or band, and no recommendation is derived from it.

It is being collected now because it is one of three inputs to a complexity indicator
that will be derived later:

1. **Application count.** How many separate systems a process touches. Every extra
   application is another integration to build, another set of credentials, and
   another thing that can change underneath the automation.
2. **API integration tier.** Whether each of those applications can be reached
   programmatically, and how well. A documented API, an export, a portal with no
   interface, and a filing cabinet are four different jobs.
3. **Planned system change.** Whether the ground is about to move.

The indicator will be derived from fields already collected rather than judged, which
keeps it out of the rubric and out of the judge. It is a description of the build, not
an assessment of the opportunity, and mixing the two would make both harder to argue
with.

### Why a planned change is a timing constraint and not a disqualifier

This is the part worth writing down, because the received wisdom points the other way.

In screen scraping RPA, a planned system change is close to fatal. The automation is
coupled to the pixels and the DOM of the thing it drives, so replacing that thing
means rebuilding the automation from nothing. The standard advice, freeze the process
before you automate it, follows from that coupling.

API-first automation does not have the same coupling. The work sits against an
interface rather than against a screen, and the parts that survive a system change are
the parts that took the thinking: the business rules, the decision points, the error
handling, the mapping between what the business calls a thing and what the system
calls it. Replacing the system underneath means rewriting an adapter, which is real
work, but it is not the same as starting again.

So a planned system change changes two things and neither of them is whether to
proceed:

- **When.** Sometimes it is cheaper to wait for the new system and build once. Often
  it is not, because the change is a year out and the process is bleeding now.
- **How.** It argues for a thicker seam between the business logic and whatever it is
  reading from, which is good design regardless and is only worth paying for when a
  change is actually expected.

Treating it as a disqualifier would mean declining to help a business precisely when
it is already committed to a period of disruption, which is when the pain is usually
highest and the appetite to fix it is greatest.

### What has to be decided before it is built

- Whether the indicator is shown as a separate figure alongside the recommendation
  band, or folded into the report as prose. The current preference is separate,
  because a build difficulty estimate and an opportunity score answer different
  questions and a reader should not have to unpick them.
- Whether application count comes from `current_tools` or needs its own field.
  `current_tools` is a free text list today and includes things like paper and phone,
  which are not applications.
- What the API integration tier scale is, and whether it can be derived at all without
  asking the business something they cannot answer from memory. That constraint has
  held for every field in the intake so far and should hold for this one.

## Tighten schema_version before the chatbot writes intakes

**Status: known hole, accepted for now.**

`schema_version` is an enum of `1.2.0` and `1.3.0` rather than a const, because 1.3.0
only adds one optional field and removes nothing, so a 1.2.0 document still satisfies
the schema. That is what lets the Gemini authored intakes stay untouched.

The hole it leaves: a document can declare `1.2.0` and carry `planned_system_change`,
and it will validate. Nothing rejects it, because the version string is checked
against a list rather than against what the document actually contains. So the version
no longer strictly describes the contents, it describes the earliest version the
contents could have been written against, which is not the same claim.

Low risk while intakes are hand managed and there are three of them. It stops being
low risk the moment the intake chatbot is writing files, because then a version string
is being generated rather than typed by someone who knows what they meant, and a
mislabelled document could sit in the eval set for months looking fine.

The fix is a conditional: when `schema_version` is `1.2.0`, no process may carry a
field introduced after it. JSON Schema can express that with `if` and `then` over the
`processes` items, at the cost of a block that has to be extended on every version
bump. That maintenance cost is the reason it is not there yet, and it is worth paying
before anything starts generating intakes rather than after.

Do this before the chatbot writes its first file, not after.

## Rule for the deployment work: assert on the artifact, not the exit code

**Status: a rule, not a task. It applies to anything that shells out.**

Three bugs in this project have had exactly the same shape, all in the Edge PDF
fallback:

1. A fresh profile directory sent Edge into its first run flow, so it exited zero
   without printing.
2. A relative output path was resolved against Edge's working directory rather than
   ours, so it exited zero having written the file somewhere nobody looked.
3. Edge exited before the PDF had been flushed, so the process returning was read as a
   finished render when the file was not there yet.

In all three, the exit code was zero, the artifact was absent, and the failure was
indistinguishable from the tool not being installed. The first two were diagnosed as
"Edge is missing" before anyone looked at the file.

**So: never treat process exit as evidence that an artifact exists. Assert on the
artifact.** Check that the file is there, that it is not empty, and where it matters
that it is the right shape. A subprocess that returns zero has told you it finished,
not that it did the job, and those are different claims.

This is already applied in one place worth copying. The CI workflow does not trust the
report command's exit code, because that command is built to degrade rather than fail
when a renderer is missing. It greps for a non empty PDF for every intake instead. Any
deployment step that produces a file should be checked the same way.

The rule generalises past subprocesses: an upload that returns 200, a deploy that
reports success, and a migration that logs "done" are all the same claim, and none of
them is the artifact.

## Out of scope for the engine

Recorded here so it stays out: the chatbot that collects the intake, the website
embed, and any CRM integration. The boundary between the engine and all three is
`intake/schema.json`.
