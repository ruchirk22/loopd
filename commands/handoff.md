---
description: Write .agentic/brief.md — the handover brief that seeds a loopd run
---

You are handing the current task off to loopd, an autonomous engineering runtime. loopd's
planner will know NOTHING about this conversation except what you write now — whatever you
leave out, it will not know.

Write the file `.agentic/brief.md` in the repository root (create the `.agentic/`
directory if needed) with EXACTLY these sections, in this order:

# Task brief

## Objective
What must exist when this is done, stated in testable terms — not aspirations.

## Repo facts
Stack, layout, and the build/test/lint commands you have VERIFIED actually work in this
repo (with the exact invocations).

## Decisions already made
Every decision from this conversation the loop must respect, each WITH its rationale so
the PM doesn't re-litigate it.

## Environment
Target infrastructure (e.g. GCP project/services), emulators or local substitutes
available, and required secret/config NAMES only — NEVER values.

## Gotchas / dead ends
Anything already tried that failed, quirks that look wrong but are right, traps the
developer would otherwise walk into.

## Out of scope
What the loop must NOT do, even if it seems helpful.

## Definition of done
A checklist of independently checkable statements that must ALL hold for the task to be
complete. Where possible phrase each so a shell command could verify it.

Be exhaustive and concrete. After writing the file, print its path and tell the user to
review it, then launch loopd from the repo root with:

    loopd --brief .agentic/brief.md [--budget N]
