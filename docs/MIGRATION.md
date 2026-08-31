# MIGRATION.md

How to hand this project to a different assistant — GPT, Gemini, a local model, or a human who has
never seen it. The build protocol (`docs/PLAN.md` §9) is deliberately model-agnostic: everything of
value lives in files, nothing lives in a conversation.

## Reading order

Read in this order and stop when you can do the task. Do not read the whole repo.

1. **`CLAUDE.md`** — conventions, hard rules, commands. ~130 lines. Always.
2. **`docs/PROJECT_BRIEF.md`** — what this is and who it is for. Once.
3. **`docs/handoff/INDEX.md`** — what has actually been built and which gates passed.
4. **`docs/handoff/PHASE_<n-1>.md`** — the previous phase's output artifact. Always, before building
   phase `n`.
5. **`docs/specs/<service>.md`** — the build spec for the one service you are touching.
6. The relevant contract doc, only if you are changing that contract.

`docs/PLAN.md` is the root document, but it is long. It is needed for phase 0 and for questions
about *why* the build is sequenced as it is. Phases 1–14 do not need it, and reading it every
session is exactly the cost this structure exists to avoid.

## Invariants — do not break these without changing the contract first

1. **`data/` is never read into context.** Query it and read the printed summary. This is the single
   largest cost control in the project.
2. **Polars + DuckDB for bulk data.** Never `pandas.read_parquet` on the 1M / 24M-row tables.
3. **Determinism.** `--seed` controls all generation. No unseeded randomness, no `datetime.now()`
   in generated data.
4. **`labels_anomaly` is never a detector input.** Only the eval harness reads it.
5. **Policy is YAML, read by both the generator and the detector.** An entitlement cannot mean one
   thing when generating and another when detecting.
6. **The evidence bundle is the only channel to the UI and the LLM.** If a figure is not in the
   bundle, it must not appear on screen.
7. **The LLM never computes.** Remove it and no number in the system changes.
8. **No external CDN, tile server or font host.** The app must work air-gapped.
9. **No ML jargon in the UI.** Reviewers are non-technical.
10. **Every phase is gated.** `python tasks.py verify <n>` must pass before the handoff is written.

## Contract files that must not drift

These four define agreements between components. Changing one without the others produces a system
that is internally inconsistent in ways tests will not catch:

| File | Agreement between |
|---|---|
| `docs/DATA_DICTIONARY.md` | datagen ↔ detector ↔ eval |
| `docs/ANOMALY_CATALOG.md` | injector ↔ detector ↔ eval report |
| `docs/EVIDENCE_CONTRACT.md` | detector ↔ API ↔ UI ↔ LLM |
| `docs/API_CONTRACT.md` | API ↔ UI |

Rule: **change the contract doc in the same commit as the code.** If you find the code and the doc
already disagree, the doc is the intended behaviour — fix the code, or fix the doc deliberately and
say so in the handoff.

## The phase protocol

Each phase is one session, cleared between phases. Nothing carries over in context; everything
carries over in files.

1. Start clean. Read exactly the three files named in the previous handoff's "Start here" block.
2. Build against the spec. **If the spec is wrong, fix the spec first, then the code** — otherwise
   the next session builds against a lie and the correction is paid for twice.
3. Run `python tasks.py verify <n>`. The gate is a number or a pass/fail, never an opinion.
4. Write `docs/handoff/PHASE_<n>.md` (template in `docs/PLAN.md` §9.2), update
   `docs/handoff/INDEX.md`.
5. Commit `phase(<n>): …` with the verify output in the body; tag `phase-<nn>`.
6. Clear the session.

## What is Claude-specific and what is not

| Asset | Portable? |
|---|---|
| `docs/`, `policy/`, `tasks.py`, all source | Yes — plain files, no tooling assumptions |
| The phase/handoff protocol | Yes — it is a file protocol, not a tool feature |
| `CLAUDE.md` | Content is portable; rename to `AGENTS.md` or paste as a system prompt |
| `.claude/skills/` | Format is Claude Code-specific. **The content is not** — each `SKILL.md` is a plain-English checklist that any assistant can follow, and a human can read them as contributor docs |

Nothing in the build depends on a Claude-specific capability. If you are a different model reading
this: the skills are checklists, the handoffs are your memory, and the gates are your ground truth.
Trust the gate over your own judgement about whether a phase is done.

## Handing over mid-phase

Do not. Finish to the gate, or write an interim handoff describing exactly what is done, what is
half-done, and which files are in an inconsistent state — then clear. `docs/PLAN.md` §9.3 expects
phases 10 and 14 to need splitting; splitting at a written interim handoff is cheap, and pushing
through a bloated context is not.
