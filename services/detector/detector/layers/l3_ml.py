"""Layer 3 -- the two unsupervised models over the employee matrix.

Where layer 1 asks "was a clause broken?" and layer 2 "is this normal for
somebody like them?", layer 3 asks the question neither can: *is this record
like any record we have ever seen?* Nothing here knows what an allowance is.
Both models are fitted on the whole population with no labels at all, which is
the point -- a code nobody thought to write a rule for still leaves a record
that does not fit.

1. **Isolation Forest** over the engineered matrix. A point that can be
   separated from the rest in a handful of random splits is unusual in several
   dimensions at once, which is the shape of most of family C. `contamination`
   comes from the expected anomaly rate in `policy/graph_ml.yaml` and is never
   left at `'auto'` (docs/specs/detector.md).
2. **A tabular denoising autoencoder** (PyTorch): categorical embeddings and a
   numeric branch into a bottleneck and back out. Part of every input row is
   blanked before the encoder sees it, so the model must learn what the rest of
   a row implies rather than copy its input through. The size of the gap
   between what it reconstructs and what was really there is the score, and
   **the gap per feature is the attribution** -- which column of the record the
   model could not account for.

CUDA when the machine has one, CPU when it does not. Slower is fine; a hard
CUDA dependency is not, and the phase-5 gate runs the CPU path to prove it.

Neither model produces an anomaly *code*. They produce one normalised score per
employee, which phase 6 fuses under `layer_weights.ml_unsupervised`, and which
C03 reads as corroboration. Never trained on `labels_anomaly`: the connection
this module is handed cannot see it (`detector/lake.py`).
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, field
from typing import Any

import duckdb
import numpy as np

# What the UI renders. A feature name is an internal identifier and is never
# displayed (docs/specs/detector.md, feature build), so every column the models
# can attribute to needs a phrase a reviewer would recognise. Anything not
# named here falls back to a prettified column name, which is why the fallback
# has to stay readable too.
FEATURE_LABELS: dict[str, str] = {
    "grade": "Grade",
    "pay_grade_step": "Step within grade",
    "age_years": "Age",
    "service_years": "Years of service",
    "tenure_months": "Months since hire",
    "months_in_grade": "Months in grade",
    "education_rank": "Education level",
    "job_min_education_rank": "Education the post requires",
    "certifications_count": "Certifications held",
    "languages_count": "Languages recorded",
    "dependents_count": "Dependents",
    "dependents_in_kingdom": "Dependents in the Kingdom",
    "performance_rating_y1": "Performance rating, last year",
    "performance_rating_y2": "Performance rating, two years ago",
    "performance_rating_y3": "Performance rating, three years ago",
    "performance_rating_mean": "Performance rating",
    "base_salary": "Base salary",
    "band_salary_min": "Bottom of the approved band",
    "band_salary_mid": "Middle of the approved band",
    "band_salary_max": "Top of the approved band",
    "band_position": "Position within the approved band",
    "allowance_total_monthly": "Total allowances",
    "allowance_ratio": "Allowances as a share of base pay",
    "allowance_paid_count": "Number of allowances paid",
    "overtime_ratio": "Overtime against base pay",
    "periods_paid": "Months paid",
    "base_pay_mean": "Average base pay",
    "base_pay_std": "Movement in base pay",
    "base_pay_slope": "Trend in base pay",
    "base_pay_max_jump": "Largest rise in base pay",
    "base_pay_max_jump_pct": "Largest rise in base pay, as a share",
    "allowance_total_mean": "Average allowances",
    "allowance_total_std": "Movement in allowances",
    "allowance_total_slope": "Trend in allowances",
    "allowance_total_max_jump": "Largest rise in allowances",
    "overtime_pay_mean": "Average overtime pay",
    "overtime_pay_std": "Movement in overtime pay",
    "overtime_pay_slope": "Trend in overtime pay",
    "overtime_pay_max_jump": "Largest rise in overtime pay",
    "net_mean": "Average take-home pay",
    "net_std": "Movement in take-home pay",
    "net_slope": "Trend in take-home pay",
    "net_max_jump": "Largest rise in take-home pay",
    "standing_pay_mean": "Average standing pay",
    "standing_pay_std": "Movement in standing pay",
    "standing_pay_slope": "Trend in standing pay",
    "standing_pay_max_jump": "Largest rise in standing pay",
    "standing_pay_max_jump_pct": "Largest rise in standing pay, as a share",
    "allowance_ratio_mean": "Average share of pay taken by allowances",
    "allowance_ratio_std": "Movement in the allowance share",
    "allowance_ratio_slope": "Trend in the allowance share",
    "allowance_ratio_max_jump": "Largest rise in the allowance share",
    "iban_cluster_size": "Employees sharing this bank account",
    "iban_count": "Bank accounts on record",
    "identity_cluster_size": "Records sharing this identity number",
    "manager_depth": "Levels up to the top of the reporting line",
    "manager_cycle_flag": "Reporting line closes on itself",
    "approver_is_self_flag": "Has approved their own record",
    "approvals_given": "Records approved for others",
    "activity_score_mean": "Average recorded activity",
    "badge_swipes_mean": "Average badge entries a month",
    "erp_logins_mean": "Average system logins a month",
    "silent_paid_periods": "Months paid with no badge entry or login",
    "job_safety_critical": "Safety-critical post",
    "site_hardship_tier": "Hardship tier of the site",
    "site_remote_allowance_eligible": "Site approved for remote-site pay",
    "gender": "Gender",
    "nationality": "Nationality",
    "nationality_class": "Nationality class",
    "service_band": "Length of service",
    "employment_type": "Type of employment",
    "contract_type": "Type of contract",
    "status": "Employment status",
    "source_system": "Source system",
    "job_family": "Job family",
    "business_line": "Business line",
    "region_code": "Region",
    "site_class": "Type of site",
    "work_pattern": "Work pattern",
    "housing_type": "Housing arrangement",
    "transport_mode": "Transport arrangement",
}


def feature_label(name: str) -> str:
    """The phrase a reviewer reads for one matrix column."""
    if name in FEATURE_LABELS:
        return FEATURE_LABELS[name]
    return name.replace("_", " ").capitalize()


class MLError(RuntimeError):
    """A layer-3 model that cannot be fitted. Fatal, never silently skipped."""


@dataclass
class FeatureMatrix:
    """The employee matrix both models read, and the names behind its columns."""

    employees: np.ndarray
    numeric: np.ndarray
    numeric_names: tuple[str, ...]
    categorical: np.ndarray
    categorical_names: tuple[str, ...]
    cardinalities: tuple[int, ...]
    values: dict[str, np.ndarray] = field(repr=False, default_factory=dict)

    @property
    def rows(self) -> int:
        return len(self.employees)

    @property
    def features(self) -> int:
        return len(self.numeric_names) + len(self.categorical_names)

    @property
    def names(self) -> tuple[str, ...]:
        return self.numeric_names + self.categorical_names


@dataclass
class MLScores:
    """One layer-3 scoring pass: both models, and what they said about everyone."""

    rows: int
    features: int
    numeric_features: int
    categorical_features: int
    device: str
    cuda_available: bool
    contamination: float
    epochs: int
    final_loss: float
    forest_seconds: float
    autoencoder_seconds: float
    seconds: float
    # How many records carry a per-feature explanation. Below the population
    # at 1m, where only the strangest records are explained.
    attributed: int = 0
    table: Any = field(repr=False, default=None)  # pyarrow.Table

    @property
    def trained(self) -> bool:
        return self.rows > 0

    @property
    def used_cuda(self) -> bool:
        return self.device.startswith("cuda")


# --------------------------------------------------------------------------
# The matrix
# --------------------------------------------------------------------------


class LevelColumn:
    """A categorical column as codes plus levels, never one string per row.

    The attribution needs the *value* behind a code for the handful of records
    it explains, so the levels have to survive the encoding.  Materialising
    them back into a per-row array of strings costs a hundred megabytes a
    column at 1m and is read for perhaps twenty thousand rows, so the column
    stays as it was encoded and is indexed on demand.
    """

    __slots__ = ("codes", "levels")

    def __init__(self, levels: np.ndarray, codes: np.ndarray) -> None:
        self.levels = levels
        self.codes = codes

    def __len__(self) -> int:
        return len(self.codes)

    def __getitem__(self, item):
        if isinstance(item, slice):
            return LevelColumn(self.levels, self.codes[item])
        return self.levels[int(self.codes[item])]


def _encode(values) -> tuple[np.ndarray, int, np.ndarray]:
    """Categoricals to a stable ordinal code, sorted by value not by row order.

    Two runs over one lake must fit the same model and quote the same figures,
    and an encoding that depended on which row DuckDB returned first would
    quietly break that -- hence the sort, which Arrow's dictionary encoding
    does not give (it numbers levels in the order they first appear).

    Vectorised through Arrow rather than a Python dict lookup per row: the
    dictionary form is one pass in C over the column, where the dict was one
    hash per employee per categorical -- fifteen million of them at 1m.
    """
    import pyarrow as pa
    import pyarrow.compute as pc

    array = values if isinstance(values, pa.Array | pa.ChunkedArray) else pa.array(values)
    encoded = pc.dictionary_encode(pc.cast(array, pa.string()).fill_null(""))
    if isinstance(encoded, pa.ChunkedArray):
        encoded = encoded.combine_chunks()
    levels = encoded.dictionary.to_numpy(zero_copy_only=False)
    indices = encoded.indices.to_numpy(zero_copy_only=False).astype(np.int64)
    order = np.argsort(levels, kind="stable")
    rank = np.empty(len(levels), dtype=np.int64)
    rank[order] = np.arange(len(levels), dtype=np.int64)
    return rank[indices], len(levels), levels[order]


def build_matrix(
    con: duckdb.DuckDBPyConnection, policy, *, log=None
) -> FeatureMatrix:
    """Assemble the employee matrix from the feature store.

    Named by exclusion rather than by an include list: `features_employee`
    grows as later layers need more columns, and a hand-maintained include list
    would silently stop feeding the models the day somebody adds a feature.
    """
    config = policy.matrix
    table = str(config["table"])
    excluded = set(config["exclude"])
    wanted_categorical = [c for c in config["categorical"]]

    described = con.execute(f"DESCRIBE {table}").fetchall()
    types = {row[0]: str(row[1]).upper() for row in described}
    missing = sorted(set(wanted_categorical) - set(types))
    if missing:
        raise MLError(
            f"graph_ml.yaml names categorical column(s) {missing} that "
            f"{table} does not have"
        )

    categorical = [c for c in wanted_categorical if c not in excluded]
    numeric = [
        name
        for name, kind in types.items()
        if name not in excluded
        and name not in categorical
        and any(
            token in kind
            for token in ("INT", "DECIMAL", "DOUBLE", "FLOAT", "BOOLEAN", "HUGEINT")
        )
    ]
    if not numeric:
        raise MLError(f"{table} yielded no numeric columns for the layer-3 matrix")

    import pyarrow as pa
    import pyarrow.compute as pc

    columns = ", ".join(["employee_id", *numeric, *categorical])
    frame = con.execute(
        f"SELECT {columns} FROM {table} ORDER BY employee_id"
    ).to_arrow_table()

    employees = frame.column("employee_id").to_numpy(zero_copy_only=False).astype(str)
    rows = len(employees)

    # One column at a time, cast in Arrow rather than converted per value in
    # Python: at 1m the old form was sixty-six million float() calls through an
    # object array, which cost more than fitting either model.
    values = np.empty((rows, len(numeric)), dtype=np.float64)
    for position, name in enumerate(numeric):
        column = pc.cast(frame.column(name), pa.float64())
        as_float = column.to_numpy(zero_copy_only=False).astype(np.float64, copy=False)
        finite = np.isfinite(as_float)
        # A missing value is imputed to the population median rather than to
        # zero: zero is a real salary and a real allowance count, and imputing
        # to it would invent an outlier where the record is merely incomplete.
        fill = float(np.median(as_float[finite])) if finite.any() else 0.0
        values[:, position] = np.where(finite, as_float, fill)

    codes, cardinalities, level_columns = [], [], {}
    for name in categorical:
        encoded, levels, names_of_levels = _encode(frame.column(name))
        codes.append(encoded)
        cardinalities.append(levels)
        level_columns[name] = names_of_levels
    category_matrix = (
        np.column_stack(codes) if codes else np.zeros((rows, 0), dtype=np.int64)
    )
    del frame

    # The per-column view an attribution reads its value from. Numeric columns
    # are views into the matrix itself and cost nothing; categoricals stay in
    # their encoded form (`LevelColumn`) rather than becoming a string a row.
    displayed: dict[str, Any] = {
        name: values[:, position] for position, name in enumerate(numeric)
    }
    for position, name in enumerate(categorical):
        displayed[name] = LevelColumn(level_columns[name], category_matrix[:, position])

    matrix = FeatureMatrix(
        employees=employees,
        numeric=values,
        numeric_names=tuple(numeric),
        categorical=category_matrix,
        categorical_names=tuple(categorical),
        cardinalities=tuple(cardinalities),
        values=displayed,
    )
    if log:
        log(
            f"  matrix    {matrix.rows:,} employees, {len(numeric)} numeric + "
            f"{len(categorical)} categorical features"
        )
    return matrix


def _standardise(values: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Centre on the median and scale by the MAD, for the same reason layer 2 does.

    The mean and the standard deviation of a column are moved by exactly the
    records this layer exists to find; the median and the MAD are not.
    """
    if values.size == 0:
        return values, np.zeros(0), np.ones(0)
    centre = np.median(values, axis=0)
    spread = np.median(np.abs(values - centre), axis=0) * 1.4826
    flat = spread <= 1e-9
    if flat.any():
        fallback = values.std(axis=0)
        spread = np.where(flat, np.where(fallback > 1e-9, fallback, 1.0), spread)
    return (values - centre) / spread, centre, spread


def _percentile(score: np.ndarray) -> np.ndarray:
    """A raw score to its rank within the scored population, 0-100.

    `policy/fusion.yaml` combines layers on percentile ranks, so a layer hands
    up a number that already means the same thing whatever its own scale was.
    """
    if score.size == 0:
        return score
    order = np.argsort(score, kind="stable")
    ranks = np.empty_like(order, dtype=np.float64)
    ranks[order] = np.arange(len(score), dtype=np.float64)
    return ranks / max(len(score) - 1, 1) * 100.0


# --------------------------------------------------------------------------
# Isolation Forest
# --------------------------------------------------------------------------


def fit_forest(matrix: FeatureMatrix, config: dict) -> tuple[np.ndarray, float]:
    """Isolation Forest over the standardised matrix. Higher score = stranger.

    Fitting is cheap whatever the population -- `max_samples` bounds it by
    definition -- and scoring is what grows: every employee walks 300 trees.
    It is therefore done a block of rows at a time (`score_batch_rows`), so
    the peak is one block's worth of tree traversal rather than a million
    rows' worth, and the float32 matrix sklearn wants is built once per block
    instead of doubling the whole matrix.
    """
    from sklearn.ensemble import IsolationForest

    started = time.perf_counter()
    numeric, _, _ = _standardise(matrix.numeric)
    features = (
        np.column_stack([numeric, matrix.categorical]).astype(np.float32, copy=False)
        if matrix.categorical.shape[1]
        else numeric.astype(np.float32, copy=False)
    )
    del numeric
    forest = IsolationForest(
        n_estimators=int(config["n_estimators"]),
        max_samples=int(config["max_samples"]),
        max_features=float(config["max_features"]),
        contamination=float(config["contamination"]),
        random_state=int(config["random_state"]),
        n_jobs=int(config["n_jobs"]),
    )
    forest.fit(features)
    # `score_samples` is higher for normal points; negate so that in this
    # module "bigger" always means "stranger", whichever model produced it.
    block = int(config.get("score_batch_rows") or len(features) or 1)
    raw = np.empty(len(features), dtype=np.float64)
    for start in range(0, len(features), block):
        stop = min(start + block, len(features))
        raw[start:stop] = -np.asarray(
            forest.score_samples(features[start:stop]), dtype=np.float64
        )
    return raw, round(time.perf_counter() - started, 3)


# --------------------------------------------------------------------------
# The denoising autoencoder
# --------------------------------------------------------------------------


def resolve_device(preference: str) -> tuple[str, bool]:
    """`auto` to what this machine actually has. CPU is a normal answer."""
    try:
        import torch
    except ImportError as exc:  # pragma: no cover - environment guard
        raise MLError(
            "torch is not installed; see docs/RUNBOOK.md for the CUDA build"
        ) from exc
    available = bool(torch.cuda.is_available())
    wanted = str(preference or "auto").lower()
    if wanted == "cuda" and not available:
        raise MLError("graph_ml.yaml asks for cuda and this machine has none")
    if wanted == "cpu" or (wanted == "auto" and not available):
        return "cpu", available
    return "cuda", available


def train_autoencoder(
    matrix: FeatureMatrix, config: dict, *, device: str | None = None, log=None
) -> tuple[np.ndarray, np.ndarray, np.ndarray, str, float, int]:
    """Fit the autoencoder and return the per-feature reconstruction gap.

    Returns `(gap_per_feature, above_expected, device, final_loss, epochs)`.
    The gap is what the attribution is built from: one number per column saying
    how far the model's reconstruction of this record was from the record
    itself. `above_expected` says, for the numeric columns, which side of the
    reconstruction the real value sat on -- the difference between "paid more
    than the rest of the record implies" and "paid less".
    """
    import torch
    from torch import nn

    resolved, _available = (
        (device, None) if device else resolve_device(config.get("device", "auto"))
    )
    seed = int(config["seed"])
    torch.manual_seed(seed)
    np.random.seed(seed)
    if bool(config.get("deterministic", True)):
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False
        # Two runs over one lake must quote the same figures. `warn_only` keeps
        # a kernel without a deterministic implementation from failing the run.
        os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")
        torch.use_deterministic_algorithms(True, warn_only=True)

    numeric, _, _ = _standardise(matrix.numeric)
    # The whole population stays on the host and is scored a batch at a time;
    # only what the model trains on is moved to the device. At 1m the matrix
    # is a few hundred megabytes of VRAM that buys nothing -- every row is
    # visited exactly once at scoring time either way.
    numeric_tensor = torch.tensor(numeric, dtype=torch.float32)
    category_tensor = torch.tensor(matrix.categorical, dtype=torch.long)
    del numeric
    n_numeric = numeric_tensor.shape[1]
    cardinalities = list(matrix.cardinalities)

    widths = [
        max(1, min(int(config["embedding_max"]), math.ceil(math.sqrt(levels))))
        for levels in cardinalities
    ]
    hidden = [int(h) for h in config["hidden"]]
    bottleneck = int(config["bottleneck"])

    class Autoencoder(nn.Module):
        """Categorical embeddings and a numeric branch into one bottleneck.

        Decoding is deliberately asymmetric: the numeric branch comes back as
        values and each categorical as a distribution over its own levels, so
        "the model expected a different site class" is a statement the
        attribution can actually make.
        """

        def __init__(self) -> None:
            super().__init__()
            self.embeddings = nn.ModuleList(
                [nn.Embedding(levels, width)
                 for levels, width in zip(cardinalities, widths)]
            )
            width_in = n_numeric + sum(widths)
            encoder: list[nn.Module] = []
            size = width_in
            for layer in hidden:
                encoder += [nn.Linear(size, layer), nn.ReLU()]
                size = layer
            encoder += [nn.Linear(size, bottleneck), nn.ReLU()]
            self.encoder = nn.Sequential(*encoder)
            decoder: list[nn.Module] = []
            size = bottleneck
            for layer in reversed(hidden):
                decoder += [nn.Linear(size, layer), nn.ReLU()]
                size = layer
            self.decoder = nn.Sequential(*decoder)
            self.numeric_head = nn.Linear(size, n_numeric)
            self.category_heads = nn.ModuleList(
                [nn.Linear(size, levels) for levels in cardinalities]
            )

        def forward(self, values, codes):
            parts = [values]
            for index, embedding in enumerate(self.embeddings):
                parts.append(embedding(codes[:, index]))
            latent = self.decoder(self.encoder(torch.cat(parts, dim=1)))
            return (
                self.numeric_head(latent),
                [head(latent) for head in self.category_heads],
            )

    model = Autoencoder().to(resolved)
    optimiser = torch.optim.Adam(
        model.parameters(),
        lr=float(config["learning_rate"]),
        weight_decay=float(config["weight_decay"]),
    )
    mse = nn.MSELoss()
    cross_entropy = nn.CrossEntropyLoss()
    noise_rate = float(config["noise_rate"])
    category_weight = float(config["categorical_loss_weight"])
    batch_size = int(config["batch_size"])
    epochs = int(config["epochs"])
    rows = matrix.rows
    generator = torch.Generator(device="cpu").manual_seed(seed)

    # What the model learns from. An autoencoder over HR records converges on
    # the shape of a normal record, and a sample of a hundred and fifty
    # thousand of them describes that shape as well as a million does -- while
    # a million is 245 optimiser steps an epoch instead of 37, which is the
    # difference between a run that fits in fifteen minutes and one that does
    # not. Every employee is still *scored*; only the fitting is sampled.
    max_train = int(config.get("max_train_rows") or 0)
    if max_train and rows > max_train:
        # Seeded and sorted, so two runs over one lake train on exactly the
        # same records in exactly the same order.
        chosen = np.sort(
            np.random.default_rng(seed).choice(rows, size=max_train, replace=False)
        )
        index_tensor = torch.from_numpy(chosen)
        train_numeric = numeric_tensor.index_select(0, index_tensor).to(resolved)
        train_codes = category_tensor.index_select(0, index_tensor).to(resolved)
    else:
        train_numeric = numeric_tensor.to(resolved)
        train_codes = category_tensor.to(resolved)
    train_rows = train_numeric.shape[0]

    final_loss = 0.0
    model.train()
    for _epoch in range(epochs):
        order = torch.randperm(train_rows, generator=generator).to(resolved)
        total, batches = 0.0, 0
        for start in range(0, train_rows, batch_size):
            index = order[start : start + batch_size]
            values = train_numeric[index]
            codes = train_codes[index]
            # Denoising: blank part of the row before the encoder sees it, so
            # the model has to learn what the rest of a record implies rather
            # than copy its input straight through to the output.
            mask = (torch.rand(values.shape, device=resolved) >= noise_rate).float()
            predicted, logits = model(values * mask, codes)
            loss = mse(predicted, values)
            for position, head in enumerate(logits):
                loss = loss + category_weight * cross_entropy(head, codes[:, position])
            optimiser.zero_grad()
            loss.backward()
            optimiser.step()
            total += float(loss.detach().cpu())
            batches += 1
        final_loss = total / max(batches, 1)

    del train_numeric, train_codes
    if resolved.startswith("cuda"):
        torch.cuda.empty_cache()

    model.eval()
    gaps: list[np.ndarray] = []
    above: list[np.ndarray] = []
    with torch.no_grad():
        for start in range(0, rows, batch_size):
            stop = min(start + batch_size, rows)
            values = numeric_tensor[start:stop].to(resolved)
            codes = category_tensor[start:stop].to(resolved)
            predicted, logits = model(values, codes)
            numeric_gap = (predicted - values).pow(2)
            category_gap = []
            for position, head in enumerate(logits):
                probability = torch.softmax(head, dim=1)
                actual = codes[:, position]
                chosen = probability.gather(1, actual.unsqueeze(1)).squeeze(1)
                # How surprised the model was by the level that was really
                # there. One number per categorical, on the same footing as a
                # squared error, so one attribution list can carry both.
                category_gap.append(-torch.log(chosen.clamp_min(1e-6)))
            gap = torch.cat(
                [numeric_gap]
                + ([torch.stack(category_gap, dim=1)] if category_gap else []),
                dim=1,
            )
            gaps.append(gap.cpu().numpy())
            above.append((values > predicted).cpu().numpy())

    if log:
        trained = "" if train_rows == rows else f" (fitted on {train_rows:,})"
        log(f"  autoencoder  {rows:,} rows on {resolved}{trained}, {epochs} epochs, "
            f"loss {final_loss:.4f}")
    return np.vstack(gaps), np.vstack(above), resolved, float(final_loss), epochs


# --------------------------------------------------------------------------
# Attribution
# --------------------------------------------------------------------------


def _display_value(value):
    """The value behind an attribution, as a reviewer would write it down.

    Numeric columns are read from the model's own matrix, which is float
    throughout; a grade of 5 must still read as 5 rather than 5.0, so a value
    that is a whole number is rendered as one.
    """
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        return int(value)
    return value


def _attribution_json(
    matrix: FeatureMatrix,
    gap: np.ndarray,
    above: np.ndarray,
    row: int,
    top_n: int,
    min_share: float,
) -> str:
    """The `feature_attributions` array: which columns the model could not place.

    `contribution` is that column's share of the record's total gap, so the
    list reads as "most of what does not fit about this record is here", and
    `direction` says whether the value sat above or below what the rest of the
    record implied. A categorical has no side to sit on -- the model simply
    expected a different value -- so it is reported as `unexpected`
    (docs/EVIDENCE_CONTRACT.md).
    """
    total = float(gap[row].sum())
    if total <= 0:
        return "[]"
    order = np.argsort(-gap[row])
    names = matrix.names
    numeric_count = len(matrix.numeric_names)
    out = []
    for index in order[:top_n]:
        share = float(gap[row, index]) / total
        if share < min_share:
            continue
        name = names[index]
        value = _display_value(matrix.values[name][row])
        if index < numeric_count:
            direction = "increases" if bool(above[row, index]) else "reduces"
        else:
            direction = "unexpected"
        out.append(
            {
                "feature": name,
                "label_en": feature_label(name),
                "contribution": round(share, 4),
                "direction": direction,
                "value": value.item() if hasattr(value, "item") else value,
            }
        )
    return json.dumps(out, default=str)


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------


def fit(
    con: duckdb.DuckDBPyConnection,
    policy,
    *,
    log=None,
) -> MLScores:
    """Fit both models and register `ml_scores` for the rest of layer 3.

    The registered table is what C03 joins to for corroboration and what phase
    6 fuses under `layer_weights.ml_unsupervised`.
    """
    started = time.perf_counter()
    matrix = build_matrix(con, policy, log=log)
    if matrix.rows == 0:
        raise MLError("the layer-3 matrix is empty; build the feature store first")

    forest_raw, forest_seconds = fit_forest(matrix, policy.isolation_forest)
    autoencoder = policy.autoencoder
    autoencoder_started = time.perf_counter()
    gap, above, device, final_loss, epochs = train_autoencoder(
        matrix, autoencoder, log=log
    )
    autoencoder_seconds = round(time.perf_counter() - autoencoder_started, 3)

    reconstruction = gap.sum(axis=1)
    forest_percentile = _percentile(forest_raw)
    reconstruction_percentile = _percentile(reconstruction)
    # One score for the layer, because `policy/fusion.yaml` weights layer 3 as
    # one contributor. The mean of the two ranks rather than the max: two
    # models agreeing is the signal, and one model shouting alone is exactly
    # what the corroboration bonus in phase 6 exists to price.
    ml_score = (forest_percentile + reconstruction_percentile) / 2

    top_n = int(autoencoder["attribution_top_n"])
    min_share = float(autoencoder["attribution_min_share"])
    # An attribution is a paragraph of evidence, built one record at a time in
    # Python. Only the strangest records are ever read -- fusion ignores the
    # models below `layer_contribution.ml_unsupervised_min_score` and nothing
    # renders an explanation for a record nobody was shown -- so the list is
    # built for the top of the population and left empty below it. At 10k the
    # cap does not bind and every employee still carries one.
    limit = int(autoencoder.get("attribution_max_rows") or 0)
    if limit and matrix.rows > limit:
        explained = np.argpartition(-ml_score, limit - 1)[:limit]
    else:
        explained = np.arange(matrix.rows)
    payload = [""] * matrix.rows
    for row in explained:
        payload[int(row)] = _attribution_json(
            matrix, gap, above, int(row), top_n, min_share
        )
    result_attributed = len(explained)

    import pyarrow as pa

    table = pa.table(
        {
            "employee_id": pa.array(matrix.employees, pa.string()),
            "forest_raw": pa.array(np.round(forest_raw, 6), pa.float64()),
            "forest_score": pa.array(np.round(forest_percentile, 3), pa.float64()),
            "reconstruction_gap": pa.array(np.round(reconstruction, 6), pa.float64()),
            "reconstruction_score": pa.array(
                np.round(reconstruction_percentile, 3), pa.float64()
            ),
            "ml_score": pa.array(np.round(ml_score, 3), pa.float64()),
            "ml_attributions_json": pa.array(payload, pa.string()),
        }
    )
    con.register("ml_scores_arrow", table)
    con.execute(
        "CREATE OR REPLACE TEMP TABLE ml_scores AS SELECT * FROM ml_scores_arrow"
    )
    con.unregister("ml_scores_arrow")

    _device, cuda_available = resolve_device(autoencoder.get("device", "auto"))
    result = MLScores(
        rows=matrix.rows,
        features=matrix.features,
        numeric_features=len(matrix.numeric_names),
        categorical_features=len(matrix.categorical_names),
        device=device,
        cuda_available=cuda_available,
        contamination=float(policy.isolation_forest["contamination"]),
        epochs=epochs,
        final_loss=round(final_loss, 6),
        forest_seconds=forest_seconds,
        autoencoder_seconds=autoencoder_seconds,
        seconds=round(time.perf_counter() - started, 3),
        attributed=int(result_attributed),
        table=table,
    )
    if log:
        log(
            f"  models    isolation forest {forest_seconds:.2f}s, autoencoder "
            f"{autoencoder_seconds:.2f}s on {device}"
        )
    return result
