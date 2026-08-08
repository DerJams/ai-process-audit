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

## Out of scope for the engine

Recorded here so it stays out: the chatbot that collects the intake, the website
embed, and any CRM integration. The boundary between the engine and all three is
`intake/schema.json`.
