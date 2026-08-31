"""`employee_master` -- the widest table and the join hub for everything else.

Two things make this module the most consequential one in the generator.

**The distributions have to be skewed the way a real workforce is.**  A uniform
population makes every anomaly trivially separable, so the evaluation would
measure nothing.  Nationality mix varies by site class, the grade pyramid tapers
at the top and is floored at the hard sites, tenure is right-skewed, and site
assignment follows `headcount_weight` -- which is exactly why every map metric
has to normalise per 1,000 employees.

**The population is generated in two passes.**  A first pass draws the fields
that decide where an employee sits (site, org unit, grade, tenure, status);
managers, spousal links and unit heads are then resolved *across* the whole
population, because "a manager is at least two grades higher in the same or a
parent unit" is not a per-row property.  Only then are the wide rows built.
Manager acyclicity falls out of this for free: a manager's grade is strictly
greater than their report's, so no chain can close.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

import numpy as np

from .. import entitlement as ent
from ..config import ScaleConfig
from ..dimensions import org_unit as org_dim
from ..identifiers import (
    badge_numbers,
    employee_ids,
    ibans,
    iqama_numbers,
    national_ids,
    passport_numbers,
)
from ..names import (
    ARAB_FEMALE_GIVEN,
    ARAB_MALE_GIVEN,
    DEGREE_FIELDS,
    EXPAT_FAMILY,
    EXPAT_GIVEN,
    GCC_FAMILY,
    INSTITUTIONS,
    LANGUAGES,
    SAUDI_FAMILY,
    normalise,
)
from ..policy import DatagenPolicy, mix
from ..rng import StreamRegistry, weighted_index
from ..schemas import ALLOWANCE_CODES
from .assignment import Career, build_career

MIN_HIRE_AGE = 21
MAX_AGE = 64

# `months_since_site_change` when the employee has never been moved. Hire is
# not a site change: reading it as one would pay RELOCATION -- 3,500 SAR flat --
# to every new joiner, which on a junior salary would dominate the pay packet.
NEVER_MOVED = 999

RATING_VALUES = (1, 2, 3, 4, 5)
RATING_WEIGHTS = (0.04, 0.13, 0.45, 0.27, 0.11)

SERVICE_BANDS = ((2, "0-2"), (5, "2-5"), (10, "5-10"), (20, "10-20"), (10**6, "20+"))


# --------------------------------------------------------------------------
# Index helpers over the org dimension
# --------------------------------------------------------------------------


@dataclass
class OrgIndex:
    """Random-access views over `dim_org_unit` that the population pass needs."""

    ids: list[str]
    levels: np.ndarray
    cost_centers: list[str]
    site_of_unit: np.ndarray
    ancestors: list[tuple[int, ...]]
    sections_by_site: dict[int, list[int]]

    @classmethod
    def build(cls, table: dict[str, Any], policy: DatagenPolicy) -> OrgIndex:
        ids = list(table["org_unit_id"])
        position_of = {unit: i for i, unit in enumerate(ids)}
        levels = np.asarray(table["level"], dtype=np.int64)
        parents = [
            position_of[p] if p is not None else -1 for p in table["parent_org_unit_id"]
        ]
        ancestors: list[tuple[int, ...]] = []
        for position in range(len(ids)):
            chain = [position]
            cursor = parents[position]
            while cursor >= 0:
                chain.append(cursor)
                cursor = parents[cursor]
            ancestors.append(tuple(chain))

        site_of_unit = np.array(
            [policy.site_index[s] for s in table["primary_site_id"]], dtype=np.int64
        )
        sections_by_site: dict[int, list[int]] = {}
        for position in org_dim.sections(table):
            sections_by_site.setdefault(int(site_of_unit[position]), []).append(int(position))
        return cls(
            ids=ids,
            levels=levels,
            cost_centers=list(table["cost_center"]),
            site_of_unit=site_of_unit,
            ancestors=ancestors,
            sections_by_site=sections_by_site,
        )

    def ancestor_at_level(self, section: int, level: int) -> int:
        """The unit at `level` above (or equal to) this section."""
        for position in self.ancestors[section]:
            if self.levels[position] == level:
                return position
        return self.ancestors[section][-1]


def _choose_by_group(
    draw: np.ndarray, group: np.ndarray, weights: np.ndarray
) -> np.ndarray:
    """Categorical sampling where each row has its own weight vector.

    One uniform per row, drawn in a single call, so the first *n* results of an
    *m*-row draw equal the first *n* results of an *n*-row draw. That prefix
    stability is what makes a small run a genuine slice of a large one.
    """
    cumulative = np.cumsum(weights, axis=1)
    cumulative = cumulative / cumulative[:, -1:]
    rows = cumulative[group]
    picks = (rows < draw[:, None]).sum(axis=1)
    return np.clip(picks, 0, weights.shape[1] - 1)


def _group_weights(
    by_group: dict[str, dict[str, float]], group_names: list[str], categories: list[str]
) -> np.ndarray:
    matrix = np.zeros((len(group_names), len(categories)), dtype=np.float64)
    for row, name in enumerate(group_names):
        spec = by_group[name]
        for column, category in enumerate(categories):
            matrix[row, column] = float(spec.get(category, 0.0))
    # A group with no weight at all would divide by zero; spread it evenly.
    empty = matrix.sum(axis=1) == 0
    matrix[empty] = 1.0
    return matrix


# --------------------------------------------------------------------------
# Pass 1: the population skeleton
# --------------------------------------------------------------------------


@dataclass
class Population:
    """The whole workforce's placement fields, before the wide rows are built."""

    ids: np.ndarray
    section: np.ndarray
    org_pos: np.ndarray
    site_idx: np.ndarray
    grade: np.ndarray
    nationality_class: np.ndarray
    nationality: np.ndarray
    gender: np.ndarray
    female: np.ndarray
    service_years: np.ndarray
    hire: list[date]
    status: np.ndarray
    termination: list[date | None]
    marital: np.ndarray
    manager: np.ndarray
    approver: np.ndarray
    spouse: np.ndarray
    head_of_unit: dict[int, str]

    def __len__(self) -> int:
        return len(self.ids)


def build_population(
    cfg: ScaleConfig, policy: DatagenPolicy, streams: StreamRegistry, org: OrgIndex
) -> Population:
    table = streams.table("employee_master")
    count = cfg.employees
    pop = policy.population
    site_classes = [s.site_class for s in policy.pack.sites]

    # --- where they work -------------------------------------------------
    # Sites with no section cannot be staffed. At every real scale tier the
    # org builder seeds one section per site, so this mask is a no-op; it only
    # bites on the small slices the determinism check and the tests generate.
    staffable = np.array(
        [1.0 if i in org.sections_by_site else 0.0 for i in range(len(policy.pack.sites))]
    )
    site_idx = weighted_index(
        table.field("site"), policy.site_weights * staffable, count
    )
    section_draw = table.field("section").random(count)
    section = np.array(
        [
            org.sections_by_site[int(s)][
                int(section_draw[i] * len(org.sections_by_site[int(s)]))
            ]
            for i, s in enumerate(site_idx)
        ],
        dtype=np.int64,
    )

    # --- grade, floored by how hard the site is --------------------------
    floors = np.array(
        [site_grade_floor(policy, s) for s in policy.pack.sites], dtype=np.int64
    )
    grade_weights = np.tile(policy.grade_weights, (len(policy.pack.sites), 1))
    for row, floor in enumerate(floors):
        grade_weights[row, : floor - 1] = 0.0
    grade = _choose_by_group(table.field("grade").random(count), site_idx, grade_weights) + 1

    # --- who they are ----------------------------------------------------
    classes = ["saudi", "gcc", "expat"]
    class_weights = _group_weights(
        pop["nationality"]["by_site_class"], site_classes, classes
    )
    class_idx = _choose_by_group(
        table.field("nationality_class").random(count), site_idx, class_weights
    )
    nationality_class = np.array(classes, dtype=object)[class_idx]

    countries = sorted({c for spec in pop["nationality"]["countries"].values() for c in spec})
    country_weights = _group_weights(pop["nationality"]["countries"], classes, countries)
    country_idx = _choose_by_group(
        table.field("nationality").random(count), class_idx, country_weights
    )
    nationality = np.array(countries, dtype=object)[country_idx]

    female_share = np.array(
        [float(pop["female_share_by_site_class"][c]) for c in site_classes]
    )
    female = table.field("gender").random(count) < female_share[site_idx]
    gender = np.where(female, "F", "M").astype(object)

    # --- tenure ----------------------------------------------------------
    tenure = pop["tenure"]
    service = table.field("service_years").gamma(
        float(tenure["gamma_shape"]), float(tenure["gamma_scale"]), count
    )
    service = np.minimum(service, float(tenure["max_years"]))
    service = np.maximum(service, float(tenure["min_years_per_grade_step"]) * (grade - 1))
    service = np.minimum(service, MAX_AGE - MIN_HIRE_AGE)
    hire = [cfg.reference_date - timedelta(days=round(y * 365.25)) for y in service]

    # --- status ----------------------------------------------------------
    status_values = ["active", "terminated", "on_leave", "suspended"]
    status_weights = np.array([[float(pop["status"][v]) for v in status_values]])
    status_idx = _choose_by_group(
        table.field("status").random(count), np.zeros(count, dtype=np.int64), status_weights
    )
    status = np.array(status_values, dtype=object)[status_idx]

    termination_draw = table.field("termination").random(count)
    window_days = (cfg.reference_date - cfg.window_start).days
    termination: list[date | None] = []
    for index in range(count):
        if status[index] != "terminated":
            termination.append(None)
            continue
        earliest = max(cfg.window_start, hire[index] + timedelta(days=45))
        span = max(1, (cfg.reference_date - earliest).days)
        offset = int(termination_draw[index] * min(span, window_days))
        termination.append(earliest + timedelta(days=offset))

    # --- marital status --------------------------------------------------
    married_bands = sorted(pop["marital"]["married_share_by_service"].items())
    thresholds = np.array([float(k) for k, _ in married_bands])
    shares = np.array([float(v) for _, v in married_bands])
    married_share = shares[np.clip(np.searchsorted(thresholds, service, "right") - 1, 0, None)]
    marital_draw = table.field("marital").random(count)
    marital = np.where(marital_draw < married_share, "married", "single").astype(object)
    other = marital_draw - married_share
    divorced = float(pop["marital"]["divorced_share"])
    widowed = float(pop["marital"]["widowed_share"])
    marital = np.where(
        (marital == "single") & (other < divorced), "divorced", marital
    )
    marital = np.where(
        (marital == "single") & (other >= divorced) & (other < divorced + widowed),
        "widowed",
        marital,
    )

    # --- org placement, driven by grade ----------------------------------
    level_by_grade = sorted(pop["org_level_by_grade"].items())
    org_pos = np.array(
        [
            org.ancestor_at_level(int(section[i]), _level_for(int(grade[i]), level_by_grade))
            for i in range(count)
        ],
        dtype=np.int64,
    )

    ids = employee_ids(0, count)
    manager, approver = _assign_managers(ids, grade, org_pos, org)
    spouse = _pair_spouses(ids, marital, gender, table, pop)
    heads = _unit_heads(ids, grade, org_pos, org)

    return Population(
        ids=ids,
        section=section,
        org_pos=org_pos,
        site_idx=site_idx,
        grade=grade.astype(np.int64),
        nationality_class=nationality_class,
        nationality=nationality,
        gender=gender,
        female=female,
        service_years=service,
        hire=hire,
        status=status,
        termination=termination,
        marital=marital,
        manager=manager,
        approver=approver,
        spouse=spouse,
        head_of_unit=heads,
    )


def site_grade_floor(policy: DatagenPolicy, site) -> int:
    """The lowest grade a posting at this site implies."""
    spec = policy.population["grade"]
    return max(
        int(spec["hardship_min_grade"][site.hardship_tier]),
        int(spec["site_class_min_grade"].get(site.site_class, 1)),
    )


def _level_for(grade: int, level_by_grade: list[tuple[int, int]]) -> int:
    for ceiling, level in level_by_grade:
        if grade <= ceiling:
            return level
    return level_by_grade[-1][1]


def _assign_managers(
    ids: np.ndarray, grade: np.ndarray, org_pos: np.ndarray, org: OrgIndex
) -> tuple[np.ndarray, np.ndarray]:
    """Manager: lowest-grade employee at least two grades up, in this unit or above.

    Because a manager's grade is strictly greater than their report's, the graph
    cannot contain a cycle -- C05 is made impossible rather than checked for.
    """
    by_unit: dict[int, list[int]] = {}
    for index, unit in enumerate(org_pos):
        by_unit.setdefault(int(unit), []).append(index)

    order = np.lexsort((np.arange(len(ids)), grade))  # by grade, then index
    sorted_grades = grade[order]

    managers = np.empty(len(ids), dtype=object)
    approvers = np.empty(len(ids), dtype=object)
    for index in range(len(ids)):
        needed = grade[index] + 2
        best: tuple[int, int] | None = None
        for unit in org.ancestors[int(org_pos[index])]:
            for candidate in by_unit.get(int(unit), ()):
                if candidate == index or grade[candidate] < needed:
                    continue
                key = (int(grade[candidate]), candidate)
                if best is None or key < best:
                    best = key
            if best is not None:
                break
        if best is None:
            # Nobody senior enough overhead: fall back to the most junior
            # person in the whole company who still clears the two-grade gap.
            position = int(np.searchsorted(sorted_grades, needed, "left"))
            while position < len(order) and int(order[position]) == index:
                position += 1
            best = (0, int(order[position])) if position < len(order) else None
        managers[index] = ids[best[1]] if best is not None else None
        # Self-approval is C05, so an employee with no manager is approved by
        # the most senior person who is not themselves.
        approvers[index] = managers[index] if best is not None else ids[int(order[-1])]
        if approvers[index] == ids[index]:
            approvers[index] = ids[int(order[-2])] if len(order) > 1 else ids[index]
    return managers, approvers


def _pair_spouses(
    ids: np.ndarray, marital: np.ndarray, gender: np.ndarray, table, pop: dict
) -> np.ndarray:
    """Married couples who both work here. FAMILY is never paid to both."""
    spouse = np.empty(len(ids), dtype=object)
    spouse[:] = None
    share = float(pop["marital"]["spouse_internal_share"])
    married = np.flatnonzero(marital == "married")
    if married.size < 2:
        return spouse
    draw = table.field("spouse").random(len(ids))
    candidates = [i for i in married if draw[i] < share]
    men = [i for i in candidates if gender[i] == "M"]
    women = [i for i in candidates if gender[i] == "F"]
    for left, right in zip(men, women, strict=False):
        spouse[left] = ids[right]
        spouse[right] = ids[left]
    return spouse


def _unit_heads(
    ids: np.ndarray, grade: np.ndarray, org_pos: np.ndarray, org: OrgIndex
) -> dict[int, str]:
    """Each unit's head: the highest-graded person in it, ties to the lowest id."""
    best: dict[int, tuple[int, int]] = {}
    for index, unit in enumerate(org_pos):
        key = (-int(grade[index]), index)
        current = best.get(int(unit))
        if current is None or key < current:
            best[int(unit)] = key
    return {unit: ids[position] for unit, (_, position) in best.items()}


# --------------------------------------------------------------------------
# Pass 2: the wide rows
# --------------------------------------------------------------------------


@dataclass
class ChunkResult:
    """One chunk's `employee_master` rows plus what the fact builders need."""

    columns: dict[str, Any]
    careers: list[Career]
    records: list[dict[str, Any]]


def build_chunk(
    cfg: ScaleConfig,
    policy: DatagenPolicy,
    streams: StreamRegistry,
    org: OrgIndex,
    population: Population,
    jobs: dict[str, Any],
    resolver: ent.EntitlementResolver,
    chunk: int,
    start: int,
    count: int,
) -> ChunkResult:
    from .. import noise as noise_module

    table = streams.table("employee_master")
    pop = policy.population
    stop = start + count
    sites = policy.pack.sites
    site_classes = [s.site_class for s in sites]
    ceiling = float(policy.pack.allowance_load["clean_population_ratio_max"])

    site_idx = population.site_idx[start:stop]
    grade = population.grade[start:stop]
    klass = population.nationality_class[start:stop]

    # --- job -------------------------------------------------------------
    families = sorted({f for f in jobs["job_family"]})
    family_weights = _group_weights(
        pop["job_family_by_site_class"], site_classes, families
    )
    family_idx = _choose_by_group(
        table.field("job_family", chunk).random(count), site_idx, family_weights
    )
    job_index = _JobIndex(jobs, families)
    job_draw = table.field("job_code", chunk).random(count)

    # --- personal --------------------------------------------------------
    band_position = table.field("band_position", chunk).beta(2.2, 2.2, count)
    dependents_draw = table.field("dependents", chunk).poisson(2.2, count)
    in_kingdom_draw = table.field("in_kingdom", chunk).random(count)
    age_extra = table.field("age", chunk).gamma(2.0, 2.2, count)
    education_draw = table.field("education", chunk).random(count)
    language_draw = table.field("languages", chunk).random(count)
    training_draw = table.field("training", chunk).random(count)
    rating_draw = table.field("ratings", chunk).random((count, 3))
    housing_draw = table.field("housing", chunk).random(count)
    transport_draw = table.field("transport", chunk).random(count)
    pattern_draw = table.field("work_pattern", chunk).random(count)
    acting_draw = table.field("acting", chunk).random((count, 2))
    misc_draw = table.field("misc", chunk).random((count, 8))
    career_draw = table.field("career", chunk).random((count, 3))
    expiry_draw = table.field("expiry", chunk).random((count, 2))
    name_draw = table.field("names", chunk).random((count, 3))
    bank_draw = table.field("bank", chunk).random(count)

    patterns = sorted({p for spec in pop["work_pattern"]["by_site_class"].values() for p in spec})
    pattern_weights = _group_weights(pop["work_pattern"]["by_site_class"], site_classes, patterns)
    pattern_idx = _choose_by_group(pattern_draw, site_idx, pattern_weights)
    work_pattern = np.array(patterns, dtype=object)[pattern_idx]

    employment_values, employment_w = mix(pop["employment_type"])
    contract_values, contract_w = mix(pop["contract_type"])
    source_values, source_w = mix(pop["source_system"])
    payment_values, payment_w = mix(pop["payment_method"])
    reason_values, reason_w = mix(pop["status"]["termination_reason"])
    zero = np.zeros(count, dtype=np.int64)
    employment_type = np.array(employment_values, dtype=object)[
        _choose_by_group(misc_draw[:, 0], zero, employment_w[None, :])
    ]
    contract_type = np.array(contract_values, dtype=object)[
        _choose_by_group(misc_draw[:, 1], zero, contract_w[None, :])
    ]
    source_system = np.array(source_values, dtype=object)[
        _choose_by_group(misc_draw[:, 2], zero, source_w[None, :])
    ]
    payment_method = np.array(payment_values, dtype=object)[
        _choose_by_group(misc_draw[:, 3], zero, payment_w[None, :])
    ]
    termination_reason_pool = np.array(reason_values, dtype=object)[
        _choose_by_group(misc_draw[:, 4], zero, reason_w[None, :])
    ]
    ratings = np.array(RATING_VALUES, dtype=np.int64)[
        _choose_by_group(
            rating_draw.reshape(-1),
            np.zeros(count * 3, dtype=np.int64),
            np.array([RATING_WEIGHTS]),
        )
    ].reshape(count, 3)

    bank_codes = list(pop["banking"]["bank_codes"])
    bank_code = np.array(bank_codes, dtype=object)[
        np.minimum((bank_draw * len(bank_codes)).astype(np.int64), len(bank_codes) - 1)
    ]
    serials = np.arange(start + 1, stop + 1, dtype=np.int64)
    iban_values = ibans(bank_code, serials * 7919 + 13)

    records: list[dict[str, Any]] = []
    careers: list[Career] = []

    for offset in range(count):
        index = start + offset
        site = sites[int(site_idx[offset])]
        employee_grade = int(grade[offset])
        nationality_class = str(klass[offset])
        job_row = job_index.pick(
            families[int(family_idx[offset])], employee_grade, float(job_draw[offset])
        )
        safety_critical = bool(jobs["safety_critical"][job_row])
        alternative = job_index.pick_non_safety(
            families[int(family_idx[offset])], employee_grade, float(job_draw[offset])
        )

        service = float(population.service_years[index])
        hire = population.hire[index]
        age = min(MAX_AGE, MIN_HIRE_AGE + service + float(age_extra[offset]))
        dob = cfg.reference_date - timedelta(days=round(age * 365.25))

        record = _base_record(
            cfg=cfg,
            policy=policy,
            population=population,
            index=index,
            offset=offset,
            site=site,
            org=org,
            jobs=jobs,
            job_row=job_row,
            dob=dob,
            hire=hire,
            service=service,
            grade=employee_grade,
            nationality_class=nationality_class,
            work_pattern=str(work_pattern[offset]),
            employment_type=str(employment_type[offset]),
            contract_type=str(contract_type[offset]),
            source_system=str(source_system[offset]),
            payment_method=str(payment_method[offset]),
            termination_reason=str(termination_reason_pool[offset]),
            ratings=ratings[offset],
            dependents=int(dependents_draw[offset]),
            in_kingdom_draw=float(in_kingdom_draw[offset]),
            education_draw=float(education_draw[offset]),
            language_draw=float(language_draw[offset]),
            training_draw=float(training_draw[offset]),
            housing_draw=float(housing_draw[offset]),
            transport_draw=float(transport_draw[offset]),
            acting_draw=acting_draw[offset],
            misc_draw=misc_draw[offset],
            expiry_draw=expiry_draw[offset],
            name_draw=name_draw[offset],
            bank_code=str(bank_code[offset]),
            iban=str(iban_values[offset]),
            serial=int(serials[offset]),
        )

        family_name = families[int(family_idx[offset])]

        def job_for_grade(
            at_grade: int,
            _family: str = family_name,
            _safety: bool = safety_critical,
            _draw: float = float(job_draw[offset]),
        ) -> tuple[str, bool]:
            row = job_index.pick_matching(_family, at_grade, _safety, _draw)
            return str(jobs["job_code"][row]), bool(jobs["safety_critical"][row])

        career = build_career(
            policy=policy,
            cfg=cfg,
            hire=hire,
            grade=employee_grade,
            nationality_class=nationality_class,
            job_for_grade=job_for_grade,
            org_unit_id=org.ids[int(population.org_pos[index])],
            site_index=int(site_idx[offset]),
            min_grade=site_grade_floor(policy, site),
            alt_site_index=_similar_site(policy, int(site_idx[offset]), float(career_draw[offset, 2])),
            band_position=float(band_position[offset]),
            draws={
                "promotions": float(career_draw[offset, 0]),
                "transfers": float(career_draw[offset, 1]),
            },
            terminated_on=population.termination[index],
        )
        current = career.current
        job_row = job_index.by_code[current.job_code]
        safety_critical = bool(jobs["safety_critical"][job_row])
        _apply_job(record, jobs, job_row, policy)
        alternative = job_index.pick_non_safety(
            family_name, current.grade, float(job_draw[offset])
        )
        record["base_salary"] = current.base_cents
        record["grade"] = current.grade
        record["work_site_id"] = policy.site_ids[current.site_index]
        record["region_code"] = policy.pack.sites[current.site_index].region_code
        record["residence_city"] = policy.pack.sites[current.site_index].city
        record["pay_grade_step"] = career.steps
        record["last_increment_date"] = career.last_increment
        record["last_promotion_date"] = career.last_promotion
        record["months_in_grade"] = _months_between(
            career.last_promotion or hire, cfg.reference_date
        )
        record["months_since_site_change"] = (
            _months_between(career.site_change, cfg.reference_date)
            if career.site_change
            else NEVER_MOVED
        )

        site = sites[current.site_index]
        row = ent.feature_row(record, site, safety_critical)
        steps = tuple(
            s
            for s in ent.REPAIR_STEPS
            # The salary lever is applied per interval afterwards, not here:
            # raising the current salary does nothing for a period five years
            # ago that is over the ceiling.
            if s != "raise_within_band"
            and (alternative is not None or s != "not_safety_critical")
        )
        # The population repair is driven by the WORST period of the career,
        # not by today. Service years, a recent relocation and the grade held
        # at the time all move entitlement, so an employee can be comfortably
        # under the ceiling now and far over it in their own history -- and
        # every one of those periods is a row in the lake.
        worst_interval, worst_row = _worst_period(
            resolver, record, career, sites, hire, ceiling
        )
        _fit(resolver, record, worst_row, ceiling, policy, worst_interval.grade,
             nationality_class, steps, base_cents=worst_interval.base_cents)
        if record.pop("_needs_non_safety_job", False) and alternative is not None:
            job_row = alternative
            safety_critical = False
            _apply_job(record, jobs, job_row, policy)
            for interval in career.intervals:
                swapped = job_index.pick_matching(
                    family_name, interval.grade, False, float(job_draw[offset])
                )
                interval.job_code = str(jobs["job_code"][swapped])
                interval.safety_critical = bool(jobs["safety_critical"][swapped])

        row = ent.feature_row(record, sites[current.site_index], safety_critical)
        _fit_career(resolver, record, career, ceiling, policy, nationality_class,
                    sites, hire)
        record["base_salary"] = current.base_cents
        row["base_salary"] = ent.to_sar(current.base_cents)

        payments = resolver.payments(row)
        _apply_allowance_columns(record, payments, resolver.total_cents(row))
        record["certifications"] = _certifications(
            jobs["required_certifications"][job_row], cfg, expiry_draw[offset]
        )
        record["certifications_count"] = len(record["certifications"])
        records.append(record)
        careers.append(career)

    if cfg.noise:
        noise_module.apply(
            records,
            streams.table("employee_master").field("noise_missing", chunk),
            streams.table("employee_master").field("noise_name", chunk),
            streams.table("employee_master").field("noise_typo", chunk),
            pop["noise"],
        )
    else:
        for record in records:
            record["dq_flags"] = []
            record["name_en_normalised"] = normalise(str(record["name_en"]))

    return ChunkResult(columns=_to_columns(records), careers=careers, records=records)


def _fit(resolver, record, row, ceiling, policy, grade, klass, steps, base_cents=None):
    band_max = int(policy.band(grade, klass).salary_max * 100)
    return ent.fit_allowance_load(
        resolver, record, row, ceiling, band_max, steps, base_cents
    )


class _JobIndex:
    """`(family, grade)` to the job codes that permit that grade."""

    def __init__(self, jobs: dict[str, Any], families: list[str]) -> None:
        self._all: dict[tuple[str, int], list[int]] = {}
        self._by_safety: dict[tuple[str, int, bool], list[int]] = {}
        self.by_code: dict[str, int] = {
            str(code): row for row, code in enumerate(jobs["job_code"])
        }
        for row, family in enumerate(jobs["job_family"]):
            critical = bool(jobs["safety_critical"][row])
            for grade in range(jobs["min_grade"][row], jobs["max_grade"][row] + 1):
                self._all.setdefault((family, grade), []).append(row)
                self._by_safety.setdefault((family, grade, critical), []).append(row)
        self._fallback = {
            family: [r for r, f in enumerate(jobs["job_family"]) if f == family]
            for family in families
        }

    def pick(self, family: str, grade: int, draw: float) -> int:
        pool = self._all.get((family, grade)) or self._fallback[family]
        return pool[int(draw * len(pool)) % len(pool)]

    def pick_non_safety(self, family: str, grade: int, draw: float) -> int | None:
        pool = self._by_safety.get((family, grade, False))
        return pool[int(draw * len(pool)) % len(pool)] if pool else None

    def pick_matching(
        self, family: str, grade: int, safety_critical: bool, draw: float
    ) -> int:
        """A job at this grade with the same safety classification if one exists.

        Holding the classification steady across a career matters because
        `job.safety_critical` gates ON_CALL, SAFETY and CERT_PREMIUM: letting it
        flip at a promotion would change entitlement for reasons that have
        nothing to do with the promotion.
        """
        pool = self._by_safety.get((family, grade, safety_critical))
        if not pool:
            pool = self._all.get((family, grade)) or self._fallback[family]
        return pool[int(draw * len(pool)) % len(pool)]


_SIMILAR_SITES: dict[int, tuple[int, ...]] = {}


def _similar_site(policy: DatagenPolicy, site_index: int, draw: float) -> int:
    """A transfer destination of the same class and hardship tier.

    Moving somebody from Dhahran to Shaybah would change six site-driven
    allowances at once, which reads as a change-point (D05/D06) in a population
    that is supposed to contain none. A like-for-like move keeps the transfer
    realistic without manufacturing a signal.
    """
    peers = _SIMILAR_SITES.get(site_index)
    if peers is None:
        origin = policy.pack.sites[site_index]
        peers = tuple(
            i
            for i, s in enumerate(policy.pack.sites)
            if i != site_index
            and s.site_class == origin.site_class
            and s.hardship_tier == origin.hardship_tier
        )
        _SIMILAR_SITES[site_index] = peers
    if not peers:
        return site_index
    return peers[int(draw * len(peers)) % len(peers)]


def _months_between(start: date, end: date) -> int:
    return max(0, (end.year - start.year) * 12 + end.month - start.month)


def _service_band(years: float) -> str:
    for ceiling, label in SERVICE_BANDS:
        if years < ceiling:
            return label
    return SERVICE_BANDS[-1][1]


def _apply_job(
    record: dict[str, Any], jobs: dict[str, Any], row: int, policy: DatagenPolicy
) -> None:
    """Adopt a job, keeping the qualification floor it implies satisfied (A11)."""
    record["job_code"] = str(jobs["job_code"][row])
    record["job_family"] = str(jobs["job_family"][row])
    minimum = str(jobs["min_education"][row])
    if policy.education_rank(record["education_level"]) < policy.education_rank(minimum):
        record["education_level"] = minimum


def _period_probe(record, career, interval, sites, hire: date) -> dict[str, Any]:
    """The feature row for an interval, as at the start of that interval.

    Service years are at their lowest then, which is when SAUDI_DEV_SCHEME is
    payable; a relocation is freshest then, which is when RELOCATION is payable.
    Taking the start of the interval therefore bounds the entitlement over every
    period the interval covers.
    """
    as_at = dict(record)
    as_at["grade"] = interval.grade
    as_at["base_salary"] = interval.base_cents
    as_at["service_years"] = max(0.0, (interval.start - hire).days / 365.25)
    as_at["months_since_site_change"] = (
        0
        if career.site_change is not None and career.site_change <= interval.start
        else NEVER_MOVED
    )
    return ent.feature_row(as_at, sites[interval.site_index], interval.safety_critical)


def _worst_period(resolver, record, career, sites, hire: date, ceiling: float):
    """The interval whose allowance load sits highest against its own base pay."""
    worst = career.intervals[0]
    worst_row = _period_probe(record, career, worst, sites, hire)
    worst_ratio = -1.0
    for interval in career.intervals:
        probe = _period_probe(record, career, interval, sites, hire)
        base = max(1, interval.base_cents)
        ratio = resolver.total_cents(probe) / base
        if ratio > worst_ratio:
            worst, worst_row, worst_ratio = interval, probe, ratio
    return worst, worst_row


def _fit_career(
    resolver, record, career, ceiling: float, policy: DatagenPolicy, klass: str,
    sites, hire: date,
) -> None:
    """Keep every period of the career under the allowance-load ceiling.

    The repair ladder settles the current state, but earlier intervals sit at
    lower grades against the same flat site allowances, so a clean employee
    could still breach the ceiling in their own history. Each interval is
    lifted within its own grade band -- never outside it, so B01 and B02 stay
    clean -- and the series is left non-decreasing so no lift manufactures a
    pay cut.
    """
    for interval in career.intervals:
        probe = _period_probe(record, career, interval, sites, hire)
        total = resolver.total_cents(probe)
        if interval.base_cents <= 0 or total <= ceiling * interval.base_cents:
            continue
        band_max = int(policy.band(interval.grade, klass).salary_max * 100)
        for _ in range(4):
            needed = int(total / ceiling) + 1
            if needed <= interval.base_cents:
                break
            interval.base_cents = min(band_max, needed + (-needed % 1000))
            probe["base_salary"] = ent.to_sar(interval.base_cents)
            total = resolver.total_cents(probe)
            if interval.base_cents >= band_max:
                break
    running = 0
    for interval in career.intervals:
        running = max(running, interval.base_cents)
        interval.base_cents = running


def _certifications(required, cfg: ScaleConfig, draws) -> list[dict[str, Any]]:
    """Every required certification, held and unexpired -- pass 1 has no A11."""
    out = []
    for position, code in enumerate(required):
        months = 6 + int(float(draws[position % len(draws)]) * 42)
        expiry = _add_months(cfg.reference_date, months)
        out.append(
            {"code": code, "issued": _add_months(expiry, -36), "expiry": expiry}
        )
    return out


def _add_months(day: date, months: int) -> date:
    total = day.year * 12 + (day.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    last = [31, 29 if year % 4 == 0 and (year % 100 or not year % 400) else 28,
            31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
    return date(year, month, min(day.day, last))


def _apply_allowance_columns(record: dict[str, Any], payments, total: int) -> None:
    paid = {p.code for p in payments}
    for code in ALLOWANCE_CODES:
        record[f"has_{code}"] = code in paid
    record["allowance_total_monthly"] = total
    base = record["base_salary"]
    record["allowance_ratio"] = round(total / base, 6) if base else 0.0


def _base_record(**kwargs) -> dict[str, Any]:
    """Assemble one employee row, in the column order the dictionary declares."""
    cfg: ScaleConfig = kwargs["cfg"]
    policy: DatagenPolicy = kwargs["policy"]
    population: Population = kwargs["population"]
    index: int = kwargs["index"]
    site = kwargs["site"]
    org: OrgIndex = kwargs["org"]
    jobs = kwargs["jobs"]
    job_row = kwargs["job_row"]
    grade = kwargs["grade"]
    klass = kwargs["nationality_class"]
    service = kwargs["service"]
    hire: date = kwargs["hire"]
    misc = kwargs["misc_draw"]
    name_draw = kwargs["name_draw"]
    expiry = kwargs["expiry_draw"]
    acting = kwargs["acting_draw"]
    nationality = str(population.nationality[index])
    gender = str(population.gender[index])
    status = str(population.status[index])

    name_en, name_ar = _name(klass, nationality, gender, name_draw)
    serial = kwargs["serial"]
    unit = int(population.org_pos[index])

    marital = str(population.marital[index])
    dependents = min(int(kwargs["dependents"]), int(policy.population["marital"]["max_dependents"]))
    if marital == "single":
        dependents = 0
    in_kingdom_key = (
        klass
        if klass != "expat"
        else (
            "expat_high_grade"
            if grade >= int(policy.population["marital"]["expat_high_grade_from"])
            else "expat_low_grade"
        )
    )
    in_kingdom_share = float(policy.population["marital"]["in_kingdom_share"][in_kingdom_key])
    in_kingdom = dependents if kwargs["in_kingdom_draw"] < in_kingdom_share else 0

    levels, weights = policy.education_by_grade_band[grade]
    cumulative = np.cumsum(weights) / weights.sum()
    education = levels[int(np.searchsorted(cumulative, kwargs["education_draw"], "right"))]
    if policy.education_rank(education) < policy.education_rank(jobs["min_education"][job_row]):
        education = jobs["min_education"][job_row]

    multi = kwargs["language_draw"] < float(policy.population["education"]["languages_multi_share"])
    languages = ["Arabic", "English"] if multi else ["Arabic"]
    if klass == "expat" and multi:
        languages = ["English", LANGUAGES[serial % len(LANGUAGES)]]

    pattern = kwargs["work_pattern"]
    housing = _housing(site, kwargs["housing_draw"], policy)
    transport, route = _transport(site, kwargs["transport_draw"], policy, serial)
    acting_flag = float(acting[0]) < float(policy.population["career"]["acting_role_share"])
    acting_since = (
        _add_months(
            cfg.reference_date,
            -1 - int(float(acting[1]) * int(policy.population["career"]["acting_max_months_clean"])),
        )
        if acting_flag
        else None
    )

    graduation = (
        kwargs["dob"].year + (22 if policy.education_rank(education) >= 2 else 18)
    )
    is_saudi = klass == "saudi"
    iqama_expiry = (
        None
        if is_saudi
        else _add_months(cfg.reference_date, 2 + int(float(expiry[0]) * 28))
    )

    return {
        "employee_id": str(population.ids[index]),
        "badge_no": str(badge_numbers(np.array([serial]))[0]),
        "name_en": name_en,
        "name_ar": name_ar,
        "name_en_normalised": normalise(name_en),
        "gender": gender,
        "dob": kwargs["dob"],
        "nationality": nationality,
        "nationality_class": klass,
        "national_id": str(national_ids(np.array([serial]))[0]) if is_saudi else None,
        "iqama_no": None if is_saudi else str(iqama_numbers(np.array([serial]))[0]),
        "iqama_expiry": iqama_expiry,
        "passport_no": str(passport_numbers(np.array([nationality]), np.array([serial]))[0]),
        "passport_expiry": _add_months(cfg.reference_date, 6 + int(float(expiry[1]) * 60)),
        "marital_status": marital,
        "dependents_count": dependents,
        "dependents_in_kingdom": in_kingdom,
        "spouse_employed_internally": population.spouse[index] is not None,
        "spouse_employee_id": population.spouse[index],
        "education_level": education,
        "degree_field": DEGREE_FIELDS[serial % len(DEGREE_FIELDS)]
        if policy.education_rank(education) >= 1
        else None,
        "institution": INSTITUTIONS[serial % len(INSTITUTIONS)]
        if policy.education_rank(education) >= 1
        else None,
        "graduation_year": graduation if policy.education_rank(education) >= 1 else None,
        "certifications": [],
        "certifications_count": 0,
        "has_valid_required_certifications": True,
        "languages": languages,
        "languages_count": len(languages),
        "training_hours_ytd": int(
            kwargs["training_draw"] * float(policy.population["education"]["training_hours_max"])
        ),
        "hire_date": hire,
        "service_years": round(service, 3),
        "service_band": _service_band(service),
        "employment_type": kwargs["employment_type"],
        "contract_type": kwargs["contract_type"],
        "status": status,
        "termination_date": population.termination[index],
        "termination_reason": kwargs["termination_reason"] if status == "terminated" else None,
        "probation_end": hire + timedelta(days=90),
        "rehire_flag": bool(misc[5] < 0.04),
        "grade": grade,
        "pay_grade_step": 1,
        "job_code": str(jobs["job_code"][job_row]),
        "job_family": str(jobs["job_family"][job_row]),
        "org_unit_id": org.ids[unit],
        "cost_center": org.cost_centers[unit],
        "manager_id": population.manager[index],
        "position_id": f"POS{index + 1:09d}",
        "acting_role_flag": acting_flag,
        "acting_role_since": acting_since,
        "work_site_id": site.site_id,
        "region_code": site.region_code,
        "residence_city": site.city,
        "work_pattern": pattern,
        "rotation_cycle_days": 28 if pattern == "rotation_28_28" else (
            14 if pattern == "rotation_14_14" else None
        ),
        "housing_type": housing,
        "transport_mode": transport,
        "company_bus_route_id": route,
        "remote_work_approved_flag": pattern in ("remote", "hybrid"),
        "months_since_site_change": NEVER_MOVED,
        "base_salary": 0,
        "currency": "SAR",
        "last_increment_date": None,
        "last_promotion_date": None,
        "months_in_grade": 0,
        "performance_rating_y1": int(kwargs["ratings"][0]) if service >= 1 else None,
        "performance_rating_y2": int(kwargs["ratings"][1]) if service >= 2 else None,
        "performance_rating_y3": int(kwargs["ratings"][2]) if service >= 3 else None,
        "bonus_eligible": kwargs["contract_type"] == "permanent" and status != "terminated",
        "gosi_class": policy.pack.gosi_class_by_nationality[klass],
        "bank_code": kwargs["bank_code"],
        "iban": kwargs["iban"],
        "iban_effective_from": max(hire, cfg.window_start - timedelta(days=365)),
        "payment_method": kwargs["payment_method"],
        "payroll_hold_flag": bool(misc[6] > 0.997),
        "source_system": kwargs["source_system"],
        # Generation-time metadata, derived from the reference date rather than
        # the wall clock so two runs with the same seed stay byte-identical.
        "record_created_at": _stamp(hire),
        "record_updated_at": _stamp(cfg.reference_date),
        "dq_flags": [],
    }


def _stamp(day: date) -> datetime:
    """Generation metadata, pinned to a business date so runs stay reproducible."""
    return datetime.combine(day, time(0, 0), tzinfo=timezone.utc)


def _name(klass: str, nationality: str, gender: str, draw) -> tuple[str, str]:
    if klass == "saudi" or nationality in ("EGY", "LBN", "JOR", "SDN"):
        given = ARAB_FEMALE_GIVEN if gender == "F" else ARAB_MALE_GIVEN
        family = SAUDI_FAMILY if klass == "saudi" else EXPAT_FAMILY.get(nationality, GCC_FAMILY)
    elif klass == "gcc":
        given = ARAB_FEMALE_GIVEN if gender == "F" else ARAB_MALE_GIVEN
        family = GCC_FAMILY
    else:
        given = EXPAT_GIVEN.get(nationality, ARAB_MALE_GIVEN)
        family = EXPAT_FAMILY.get(nationality, GCC_FAMILY)
    first_en, first_ar = given[int(draw[0] * len(given)) % len(given)]
    middle_en, middle_ar = ARAB_MALE_GIVEN[int(draw[1] * len(ARAB_MALE_GIVEN)) % len(ARAB_MALE_GIVEN)]
    last_en, last_ar = family[int(draw[2] * len(family)) % len(family)]
    return f"{first_en} {middle_en} {last_en}", f"{first_ar} {middle_ar} {last_ar}"


def _housing(site, draw: float, policy: DatagenPolicy) -> str:
    spec = policy.population["housing"]
    if site.camp_available and not site.family_housing_available:
        return "company_camp_bachelor" if draw < float(spec["camp_bachelor_share"]) else "own"
    if site.family_housing_available:
        if draw < float(spec["family_housing_share"]):
            return "company_family_housing"
        return "allowance" if draw < float(spec["family_housing_share"]) + float(
            spec["allowance_share_office"]
        ) else "own"
    return "allowance" if draw < float(spec["allowance_share_office"]) else "own"


def _transport(site, draw: float, policy: DatagenPolicy, serial: int) -> tuple[str, str | None]:
    spec = policy.population["transport"]
    bus_share = float(spec["company_bus_share_by_site_class"][site.site_class])
    if draw < bus_share:
        return "company_bus", f"RT{serial % 400:04d}"
    if draw < bus_share + float(spec["allowance_share_remainder"]) * (1 - bus_share):
        return "allowance", None
    return "own", None


def _to_columns(records: list[dict[str, Any]]) -> dict[str, Any]:
    from ..schemas import EMPLOYEE_MASTER

    return {field.name: [r[field.name] for r in records] for field in EMPLOYEE_MASTER}
