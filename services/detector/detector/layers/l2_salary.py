"""The expected-salary model and its per-feature attribution, in SAR.

A gradient-boosted regressor predicts base salary from *legitimate* drivers
only -- grade, job family, service, education, performance, site class,
nationality class -- and the residual is the anomaly signal. It is never
trained on `labels_anomaly`: this is an unsupervised residual, not a classifier
(docs/specs/detector.md, layer 2).

The point of the layer is the sentence it produces. TreeSHAP decomposes the gap
between an employee's salary and the population baseline into one number per
driver, and those numbers are already in riyals because the model predicts
riyals:

    actual - baseline = sum(contributions) + residual
    "expected 18,400, actual 31,200; grade explains +2,100, site +900,
     unexplained +9,800"

That last clause is what a reviewer can take to a manager, which is why the
decomposition is exact rather than indicative: the identity above holds to the
riyal for every employee, and the phase-4 gate asserts it.

`shap` is a hard dependency in `requirements.txt`, but the attribution is not
allowed to be the reason a run fails, so a deterministic fallback with the same
additive property is used if TreeSHAP cannot explain the fitted model. Which
one ran is recorded and reported -- never guessed at.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import duckdb
import numpy as np

# What the UI renders. Feature names are internal identifiers and are never
# displayed (docs/specs/detector.md, feature build).
DRIVER_LABELS: dict[str, str] = {
    "grade": "Grade",
    "job_family": "Job family",
    "service_years": "Years of service",
    "education_rank": "Education level",
    "performance_rating_mean": "Performance rating",
    "site_class": "Type of site",
    "nationality_class": "Nationality class",
    "age_years": "Age",
    "months_in_grade": "Months in grade",
}


def driver_label(name: str) -> str:
    return DRIVER_LABELS.get(name, name.replace("_", " ").capitalize())


@dataclass
class SalaryExpectation:
    """One fitted expected-salary model and what it said about every employee."""

    drivers: tuple[str, ...]
    rows: int
    # How many of those rows carry a per-driver attribution. Below `rows` once
    # `attribution_max_rows` binds -- a record the model accounts for has no
    # gap to explain.
    attributed: int
    baseline: float
    method: str
    mae: float
    median_abs_residual: float
    seconds: float
    table: Any = field(repr=False, default=None)  # pyarrow.Table

    @property
    def trained(self) -> bool:
        return self.rows > 0


def _encode(values: np.ndarray) -> np.ndarray:
    """Categoricals to a stable ordinal code.

    Sorted, so the encoding depends on the values and not on row order: two runs
    over the same lake must fit the same model and quote the same figures.
    `np.unique` does both passes in C, which at 1m is three million fewer
    dictionary lookups than the equivalent comprehension.
    """
    _levels, codes = np.unique(values.astype(str), return_inverse=True)
    return codes.astype(np.float64)


def _matrix(rows: dict[str, np.ndarray], drivers: tuple[str, ...]) -> np.ndarray:
    columns = []
    for name in drivers:
        column = rows[name]
        if column.dtype.kind in "OUS" or column.dtype == object:
            columns.append(_encode(column))
        else:
            columns.append(np.nan_to_num(column.astype(np.float64), nan=np.nan))
    return np.column_stack(columns)


def _treeshap(model, matrix: np.ndarray) -> tuple[np.ndarray, float]:
    import shap

    explainer = shap.TreeExplainer(model)
    values = np.asarray(explainer.shap_values(matrix), dtype=np.float64)
    return values, float(np.asarray(explainer.expected_value).ravel()[0])


def _baseline_attributions(
    model, matrix: np.ndarray, medians: np.ndarray | None = None
) -> tuple[np.ndarray, float]:
    """Fallback attribution with the same additive guarantee as TreeSHAP.

    Each driver is replaced, one at a time, by the population median and the
    change in prediction is that driver's raw contribution; the raw
    contributions are then scaled so they sum exactly to `prediction - baseline`.
    Less principled than Shapley values -- it ignores interaction ordering --
    but it is deterministic, needs no extra dependency, and keeps the identity
    the evidence bundle and the reviewer's sentence both rely on.
    """
    # The population's median record, not the explained subset's: the point of
    # reference a contribution is measured from must be the same for everybody.
    medians = np.median(matrix, axis=0) if medians is None else medians
    predicted = model.predict(matrix)
    neutral = np.tile(medians, (matrix.shape[0], 1))
    baseline = float(model.predict(neutral[:1])[0])

    raw = np.zeros_like(matrix, dtype=np.float64)
    for column in range(matrix.shape[1]):
        swapped = matrix.copy()
        swapped[:, column] = medians[column]
        raw[:, column] = predicted - model.predict(swapped)

    target = predicted - baseline
    total = raw.sum(axis=1)
    scale = np.divide(target, total, out=np.ones_like(target), where=np.abs(total) > 1e-9)
    return raw * scale[:, None], baseline


def _attribution_json(
    drivers: tuple[str, ...],
    contributions: np.ndarray,
    values: dict[str, np.ndarray],
    position: int,
    top_n: int,
    min_sar: float,
    row: int | None = None,
) -> str:
    """The `feature_attributions` array of the evidence bundle, in SAR.

    `position` indexes the explained rows, `row` the population. They differ
    once `attribution_max_rows` binds and only some records are explained.
    """
    row = position if row is None else row
    order = np.argsort(-np.abs(contributions[position]))
    out = []
    for index in order[:top_n]:
        amount = float(contributions[position, index])
        if abs(amount) < min_sar:
            continue
        name = drivers[index]
        raw = values[name][row]
        out.append(
            {
                "feature": name,
                "label_en": driver_label(name),
                "contribution": round(amount, 2),
                "direction": "increases" if amount >= 0 else "reduces",
                "value": raw.item() if hasattr(raw, "item") else raw,
            }
        )
    return json.dumps(out, default=str)


def fit(
    con: duckdb.DuckDBPyConnection,
    policy,
    *,
    table: str = "features_employee",
    log=None,
) -> SalaryExpectation:
    """Fit the model over the feature store and register `salary_expectation`.

    The registered table is what every layer-2 detector joins to for the
    expected salary, the residual, and the SAR attribution behind them.
    """
    started = time.perf_counter()
    config = policy.expected_salary
    drivers = tuple(config["drivers"])
    target = str(config["target"])

    columns = ", ".join(["employee_id", target, *drivers])
    frame = con.execute(
        f"SELECT {columns} FROM {table} "
        f"WHERE {target} IS NOT NULL AND {target} > 0 ORDER BY employee_id"
    ).to_arrow_table()
    values = {name: frame.column(name).to_numpy(zero_copy_only=False)
              for name in frame.column_names}
    employees = values["employee_id"].astype(str)
    actual = values[target].astype(np.float64)
    rows = len(employees)
    if rows == 0:
        raise ValueError(f"{table} has no rows with a positive {target}")

    from sklearn.ensemble import HistGradientBoostingRegressor

    matrix = _matrix(values, drivers)
    model = HistGradientBoostingRegressor(
        random_state=int(config["random_state"]),
        max_iter=int(config["max_iter"]),
        learning_rate=float(config["learning_rate"]),
        max_depth=int(config["max_depth"]),
        min_samples_leaf=int(config["min_samples_leaf"]),
        l2_regularization=float(config["l2_regularization"]),
        early_stopping=bool(config["early_stopping"]),
    )
    model.fit(matrix, actual)
    predicted = model.predict(matrix)
    residual = actual - predicted

    # TreeSHAP over a million records costs minutes and is read for a few
    # hundred: an attribution exists to explain a *gap*, and a record the model
    # accounts for has no gap to explain. The rows explained are therefore the
    # widest residuals, capped by `attribution_max_rows`. At 10k the cap is
    # above the population and every employee still carries one.
    limit = int(config.get("attribution_max_rows") or 0)
    if limit and rows > limit:
        chosen = np.sort(np.argpartition(-np.abs(residual), limit - 1)[:limit])
    else:
        chosen = np.arange(rows)
    subset = matrix[chosen]

    try:
        contributions, baseline = _treeshap(model, subset)
        method = "treeshap"
    except Exception as exc:  # noqa: BLE001 - any explainer failure falls back
        if log:
            log(f"  shap unavailable ({type(exc).__name__}); "
                "using the deterministic baseline attribution")
        contributions, baseline = _baseline_attributions(
            model, subset, medians=np.median(matrix, axis=0)
        )
        method = "baseline-substitution"

    # The identity the reviewer's sentence rests on. Restated per row rather
    # than assumed: an attribution that does not add up is worse than none.
    explained = np.zeros(rows, dtype=np.float64)
    explained[chosen] = contributions.sum(axis=1)
    drift = (
        float(np.max(np.abs(baseline + explained[chosen] - predicted[chosen])))
        if len(chosen)
        else 0.0
    )
    if drift > 1.0:
        raise ValueError(
            f"{method} attributions do not reconstruct the prediction "
            f"(worst gap SAR {drift:,.2f}); the evidence would not add up"
        )

    top_n = int(config["attribution_top_n"])
    min_sar = float(config["attribution_min_sar"])
    payload = [""] * rows
    for position, row in enumerate(chosen):
        payload[int(row)] = _attribution_json(
            drivers, contributions, values, position, top_n, min_sar, row=int(row)
        )

    import pyarrow as pa

    out = pa.table(
        {
            "employee_id": pa.array(employees, pa.string()),
            "expected_salary": pa.array(np.round(predicted, 2), pa.float64()),
            "salary_residual": pa.array(np.round(residual, 2), pa.float64()),
            "explained_sar": pa.array(np.round(explained, 2), pa.float64()),
            "unexplained_sar": pa.array(np.round(residual, 2), pa.float64()),
            "attribution_baseline": pa.array(np.full(rows, round(baseline, 2)),
                                             pa.float64()),
            "attributions_json": pa.array(payload, pa.string()),
        }
    )
    con.register("salary_expectation_arrow", out)
    con.execute(
        "CREATE OR REPLACE TEMP TABLE salary_expectation AS "
        "SELECT * FROM salary_expectation_arrow"
    )
    con.unregister("salary_expectation_arrow")

    result = SalaryExpectation(
        drivers=drivers,
        rows=rows,
        attributed=len(chosen),
        baseline=round(baseline, 2),
        method=method,
        mae=round(float(np.mean(np.abs(residual))), 2),
        median_abs_residual=round(float(np.median(np.abs(residual))), 2),
        seconds=round(time.perf_counter() - started, 3),
        table=out,
    )
    if log:
        log(
            f"  expected salary  {rows:,} employees, {method}, "
            f"median gap SAR {result.median_abs_residual:,.0f}  "
            f"{result.seconds:.2f}s"
        )
    return result


def additive_gap(expectation: SalaryExpectation) -> float:
    """The worst riyal by which a decomposition fails to add up.

    `baseline + sum(contributions) = expected salary` is the property the
    reviewer's sentence rests on -- "grade adds 2,100, site adds 900, SAR 9,800
    unaccounted for" is only true if the parts sum to the whole. Checked here so
    the phase gate can assert it rather than trust it.
    """
    table = expectation.table
    if table is None:
        return 0.0
    expected = table.column("expected_salary").to_numpy(zero_copy_only=False)
    baseline = table.column("attribution_baseline").to_numpy(zero_copy_only=False)
    explained = table.column("explained_sar").to_numpy(zero_copy_only=False)
    return float(np.max(np.abs(baseline + explained - expected))) if len(expected) else 0.0
