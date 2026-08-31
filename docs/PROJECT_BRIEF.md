# PROJECT_BRIEF.md

## The problem

A large energy-sector organisation of roughly one million employees pays salaries, allowances and
benefits through a payroll system fed by many upstream HR processes. Some of those payments are not
owed. An employee based at head office draws a remote-site allowance. A terminated worker stays on
payroll for eight months. Two unrelated employees are paid into the same bank account. Someone's
salary steps up 30% with no promotion record behind it.

Individually these are small. At a million employees, over 24 months, they are a material financial
and regulatory exposure — and today nobody can see them, because the only way to find them is to
know what to look for and then go looking, one query at a time.

## What this project builds

A batch anomaly-detection platform that reads HR and payroll data, finds employees receiving pay or
benefits they are not entitled to, and presents each finding to a **non-technical HR or audit
reviewer** with the evidence attached and a recommended next step.

Real HR data is not available, so the project also builds the dataset: a realistic synthetic
population with deliberately injected, labelled anomalies. That is not a workaround — it is what
makes the detector measurable. Because ground truth is injected, recall and precision are exact
facts rather than estimates.

## Who it is for

**Primary user: an HR or internal-audit reviewer.** Not a data scientist. They are comfortable with
a spreadsheet and a case-management queue. They are accountable for the decisions they make, which
means an alert is worthless to them unless it says:

- *why* it fired — in language they can repeat to the employee's manager,
- *what the evidence is* — the actual figures, the actual policy clause,
- *what to do next* — a concrete action, not "investigate further".

Explainability is therefore a first-class requirement, not a feature. This is why the architecture
is rules-first: a policy violation is a **fact** with a citable clause, and that is what a reviewer
can act on. Statistical and machine-learning layers exist to catch what the rules do not know to
look for, and everything they produce is rendered back in business terms — SAR amounts and peer
counts, never model jargon.

**Secondary users**: an investigator working grouped cases, and an administrator tuning thresholds
and watching run health.

## Goals

1. **Find the four families of anomaly**: entitlement/policy violations, compensation outliers
   against peer groups, identity and payroll fraud, and behavioural or temporal drift.
2. **Explain every alert** well enough that a reviewer can act without asking a data scientist.
3. **Respect the reviewer's attention.** A hard alert budget — roughly 500 critical and 5,000 high
   per full run, the rest as a filterable watchlist. Alert fatigue kills these systems more often
   than poor detection does.
4. **Quantify the money.** Every alert carries a monthly and cumulative financial impact. This is
   what lets reviewers prioritise, and it is what makes the tool defensible to a budget holder.
5. **Run on one laptop.** Full 1M-employee, 24-month batch in under 15 minutes, peak RAM under
   12 GB, on an i9-14900HX / 24-core / 16 GB / RTX 5060 machine — and lift unchanged to a GPU server.
6. **Work air-gapped.** No external tile server, CDN or font host. The local LLM is optional and the
   product is fully usable with it switched off.

## Non-goals

- **Not real-time.** This is a batch system with a cached alert store and an on-demand re-score
  endpoint. Streaming payroll surveillance is a different product.
- **Not an HR system of record.** It reads data and writes findings. It never writes back to payroll.
- **Not an automated enforcement tool.** It recommends; a human dispositions. No payment is ever
  stopped by the software.
- **Not a foundation model over tabular HR data.** Gradient boosting and isolation forests beat it
  decisively at this data shape and cost, and more importantly they can explain themselves to an
  auditor. This is a deliberate decision, recorded in `docs/PLAN.md` §3.4.
- **Not a real organisation's data or brand.** The population is synthetic. The theme is
  *inspired by* the sector's visual language; no trademarked asset is used.

## Success criteria

| Dimension | Target |
|---|---|
| Family A (policy) detection | 100% recall, 100% precision — these are deterministic facts |
| Family B (compensation) | ≥ 85% recall |
| Families C/D (fraud, behavioural) | ≥ 75% recall |
| False positives | Planted confounders must not be scored CRITICAL |
| Alert budget | Within ±20% of 500 critical / 5,000 high at 1M scale |
| Full 1M batch | < 15 minutes wall clock, < 12 GB peak RAM |
| Reviewer comprehension | Every alert answers why / evidence / what next, with no ML jargon on screen |

## Glossary

| Term | Meaning |
|---|---|
| **Allowance** | A recurring payment on top of base salary, governed by an eligibility rule (housing, transport, remote-site, hardship, offshore…). 26 codes; see `policy/allowance_rules.yaml`. |
| **Anomaly code** | An identifier like `A01` for one specific kind of finding. 34 of them; see `docs/ANOMALY_CATALOG.md`. |
| **Alert** | One finding about one employee in one run, carrying an evidence bundle. |
| **Case** | A group of related alerts assigned to an investigator. |
| **Cohort / peer group** | The comparison set for an employee — same grade, job family, site class and so on. Built by a fallback ladder until at least 30 peers are found. |
| **Confounder** | A deliberately planted *legitimate* oddity that the detector must not flag. The false-positive test. |
| **Disposition** | A reviewer's decision on an alert: confirm, dismiss, escalate, or request info. |
| **Evidence bundle** | The self-contained JSON object behind every alert. See `docs/EVIDENCE_CONTRACT.md`. |
| **GOSI** | The Saudi social-insurance scheme. Contribution class depends on nationality class. |
| **Hardship tier** | 0–3 per site. Determines whether a hardship allowance is owed at all. |
| **Iqama** | Saudi residency permit for a non-national. An expired one on active payroll is a regulatory exposure. |
| **Nationality class** | `saudi`, `gcc`, or `expat`. Drives salary bands, GOSI class and benefit eligibility. |
| **Rotation** | A work pattern of consecutive on/off days (28/28, 14/14), typical at remote and offshore sites. |
| **Run** | One end-to-end batch scoring pass, identified by a `run_id`, producing a comparable set of alerts. |
| **Saudization** | Regulatory targets for the proportion of Saudi nationals employed. |
| **Watchlist** | Findings below the HIGH threshold — retained and filterable, but not queued for review. |
