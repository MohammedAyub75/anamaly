---
name: phase-handoff
description: Write docs/handoff/PHASE_<n>.md to the fixed template and update docs/handoff/INDEX.md at the end of a build phase. Use after a phase's verify gate passes and before clearing the session.
---

# Write a phase handoff

The handoff is the only thing that survives a session clear. It is the project's memory, its audit
trail, and the input artifact for the next session. Write it as if the reader has never seen this
repo — because they have not.

## Preconditions

1. **`python tasks.py verify <n>` passes.** Do not write a handoff for a failing gate. If the gate
   cannot pass, write the handoff describing what is blocked and say plainly that the gate is red —
   never a green handoff over a red gate.
2. Any contract doc the phase invalidated has already been updated
   (`DATA_DICTIONARY`, `ANOMALY_CATALOG`, `EVIDENCE_CONTRACT`, `API_CONTRACT`).
3. If reality diverged from `docs/specs/<service>.md`, **the spec is already fixed**. Otherwise the
   next session builds against a lie.

## Template — `docs/handoff/PHASE_<n>.md`

```markdown
# PHASE <n> — <deliverable>

**Status**: PASSED | BLOCKED   **Date**: YYYY-MM-DD   **Tag**: phase-<nn>

## What was built
One line per file created or modified, grouped by area. Say what it does, not that it exists.

## Public interfaces added
Function signatures, CLI flags, endpoints, table schemas, config keys. Enough that the next session
can call them without opening the source. This section is why the next session does not need to
read your code.

## Verify output
The actual pasted output of `python tasks.py verify <n>` — real numbers, not a summary.

## Decisions made
Anything a reasonable person would have done differently, and why you did not. Include deviations
from the spec with the reason, and confirm the spec was updated.

## Known gaps / deferred
What is not done, what is stubbed, what is deliberately left for a later phase. Be honest — an
undisclosed gap becomes a mystery bug two phases later.

## Start here (next session)
Read exactly these three files:
1. CLAUDE.md
2. docs/specs/<service>.md
3. docs/handoff/PHASE_<n>.md

First command to run:
    <the exact command>

## Contract doc changes
Which contract docs changed and why, or "none".
```

## Then

1. **Update `docs/handoff/INDEX.md`** — one line per phase: number, deliverable, status, gate
   result, date, tag. This is the whole build at a glance without opening 15 files.
2. **Commit** as `phase(<n>): <summary>` with the verify output pasted in the commit body, so
   `git log` alone tells you which gates passed.
3. **Tag** `phase-<nn>` — a clean rollback point per phase.
4. **Push.** If auth fails, that is not a reason to skip the commit: commit and tag locally, note it
   in the handoff, and push once auth is sorted.
5. **Clear the session.** Not `/compact` — clear. Compaction is for surviving a long single task;
   clearing is for starting the next one.

## Writing well

- **Interfaces over narrative.** "`generate(scale: str, seed: int) -> Manifest` writes to
  `data/raw/scale=<n>/`" beats a paragraph about how generation works.
- **Numbers over adjectives.** "10,000 employees, 240,000 payroll rows, 8.2s" beats "fast".
- **Say what surprised you.** The thing that cost an hour is the thing worth writing down.
- Keep it to about two pages. It is re-read at the start of the next session; length is a real cost.
