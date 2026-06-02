from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
import numpy as np

from .config import get_settings


OUTCOME_EXCLUDED_COLUMNS = {
    "replay_id",
    "lineage",
    "blue_goals",
    "orange_goals",
    "goal_diff_blue",
    "blue_win_label",
}

TEAM_FEATURE_COLUMNS = [
    "boost_early",
    "boost_mid",
    "boost_late",
    "boost_decay",
    "possession_rate",
    "attack_zone_rate",
    "clutch_possession_rate",
    "clutch_boost",
    "clutch_boost_advantage",
    "aerial_rate",
    "demos_total",
    "demo_timing",
    "touch_rate",
    "pressure_rate",
    "starvation_rate",
    "overcommit_rate",
    "goal_pressure_ratio",
]


def _now_id(prefix: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{prefix}_{stamp}"


def _stable_bucket(value: str) -> int:
    digest = hashlib.md5(value.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 100


def _sigmoid(values: np.ndarray) -> np.ndarray:
    values = np.clip(values, -35, 35)
    return 1.0 / (1.0 + np.exp(-values))


def _log_loss(y_true: np.ndarray, y_prob: np.ndarray) -> float:
    eps = 1e-7
    y_prob = np.clip(y_prob, eps, 1.0 - eps)
    return float(-(y_true * np.log(y_prob) + (1.0 - y_true) * np.log(1.0 - y_prob)).mean())


def _brier(y_true: np.ndarray, y_prob: np.ndarray) -> float:
    return float(np.square(y_prob - y_true).mean())


def _accuracy(y_true: np.ndarray, y_prob: np.ndarray) -> float:
    return float(((y_prob >= 0.5).astype(int) == y_true.astype(int)).mean())


def _auc(y_true: np.ndarray, y_prob: np.ndarray) -> float | None:
    positives = int(y_true.sum())
    negatives = int(len(y_true) - positives)
    if positives == 0 or negatives == 0:
        return None
    order = np.argsort(y_prob)
    ranks = np.empty_like(order, dtype=float)
    ranks[order] = np.arange(1, len(y_prob) + 1)
    rank_sum = ranks[y_true == 1].sum()
    return float((rank_sum - positives * (positives + 1) / 2.0) / (positives * negatives))


def _calibration(y_true: np.ndarray, y_prob: np.ndarray, bins: int = 5) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for idx in range(bins):
        lower = idx / bins
        upper = (idx + 1) / bins
        mask = (y_prob >= lower) & (y_prob <= upper if idx == bins - 1 else y_prob < upper)
        if not mask.any():
            continue
        output.append(
            {
                "bin": f"{lower:.1f}-{upper:.1f}",
                "count": int(mask.sum()),
                "avg_prediction": round(float(y_prob[mask].mean()), 4),
                "empirical_win_rate": round(float(y_true[mask].mean()), 4),
            }
        )
    return output


def _metrics(y_true: np.ndarray, y_prob: np.ndarray) -> dict[str, Any]:
    auc = _auc(y_true, y_prob)
    return {
        "n": int(len(y_true)),
        "accuracy": round(_accuracy(y_true, y_prob), 4),
        "log_loss": round(_log_loss(y_true, y_prob), 4),
        "brier": round(_brier(y_true, y_prob), 4),
        "auc": None if auc is None else round(auc, 4),
    }


def _numeric_replay_columns(con: duckdb.DuckDBPyConnection) -> list[str]:
    info = con.execute("PRAGMA table_info('features_replay')").fetchall()
    numeric = {"DOUBLE", "FLOAT", "REAL", "BIGINT", "INTEGER", "HUGEINT", "DECIMAL"}
    return [
        name
        for _, name, data_type, *_ in info
        if name not in OUTCOME_EXCLUDED_COLUMNS and any(kind in data_type.upper() for kind in numeric)
    ]


def _prepare_matrix(rows: list[tuple[Any, ...]]) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    replay_ids = np.array([row[0] for row in rows])
    labels = np.array([float(row[1]) for row in rows], dtype=float)
    raw = np.array([[float(value) if value is not None else np.nan for value in row[2:]] for row in rows], dtype=float)
    means = np.nanmean(raw, axis=0)
    means = np.where(np.isfinite(means), means, 0.0)
    filled = np.where(np.isnan(raw), means, raw)
    stds = filled.std(axis=0)
    stds = np.where(stds < 1e-9, 1.0, stds)
    x = (filled - means) / stds
    return replay_ids, labels, x, means, stds


def _fit_logistic(x: np.ndarray, y: np.ndarray, *, epochs: int = 1800, lr: float = 0.055) -> np.ndarray:
    x_aug = np.column_stack([np.ones(len(x)), x])
    weights = np.zeros(x_aug.shape[1], dtype=float)
    l2 = 0.01
    for _ in range(epochs):
        probs = _sigmoid(x_aug @ weights)
        gradient = (x_aug.T @ (probs - y)) / len(y)
        gradient[1:] += l2 * weights[1:]
        weights -= lr * gradient
    return weights


def _split_indices(replay_ids: np.ndarray, labels: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    test = np.array([_stable_bucket(str(replay_id)) >= 80 for replay_id in replay_ids])
    train = ~test
    if train.sum() < 4 or test.sum() < 2 or len(set(labels[test].astype(int))) < 2:
        order = np.argsort(replay_ids)
        test = np.zeros(len(replay_ids), dtype=bool)
        test[order[::5]] = True
        train = ~test
    if train.sum() == 0 or test.sum() == 0:
        test = np.zeros(len(replay_ids), dtype=bool)
        test[-1:] = True
        train = ~test
    return train, test


def train_win_prediction(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    feature_names = _numeric_replay_columns(con)
    if not feature_names:
        return {"status": "skipped", "reason": "No numeric replay features available"}

    rows = con.execute(
        f"""
        SELECT replay_id, blue_win_label, {", ".join(feature_names)}
        FROM features_replay
        WHERE blue_win_label IS NOT NULL
        ORDER BY replay_id
        """
    ).fetchall()
    if len(rows) < 6:
        return {"status": "skipped", "reason": "Need at least 6 labeled replays", "labeled_replays": len(rows)}

    replay_ids, labels, x, means, stds = _prepare_matrix(rows)
    train_idx, test_idx = _split_indices(replay_ids, labels)
    baseline_prob = float(labels[train_idx].mean())
    baseline_test_prob = np.full(test_idx.sum(), baseline_prob)
    baseline_metrics = _metrics(labels[test_idx], baseline_test_prob)

    weights = _fit_logistic(x[train_idx], labels[train_idx])
    probs = _sigmoid(np.column_stack([np.ones(len(x)), x]) @ weights)
    test_metrics = _metrics(labels[test_idx], probs[test_idx])
    calibration = _calibration(labels[test_idx], probs[test_idx])

    created_at = datetime.now(timezone.utc)
    dataset_version = con.execute("SELECT any_value(dataset_version) FROM replays").fetchone()[0]
    baseline_id = _now_id("baseline_blue_win")
    model_id = _now_id("win_logreg")

    con.execute(
        "INSERT INTO model_versions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            baseline_id,
            "Majority baseline",
            "baseline",
            "blue_win_label",
            dataset_version,
            json.dumps(feature_names),
            "deterministic replay_id md5 holdout; fallback every fifth replay for class balance",
            json.dumps(baseline_metrics, sort_keys=True),
            json.dumps(_calibration(labels[test_idx], baseline_test_prob), sort_keys=True),
            json.dumps({"constant_probability": baseline_prob}, sort_keys=True),
            created_at,
        ],
    )

    coefficients = {name: float(value) for name, value in zip(feature_names, weights[1:])}
    feature_importance = sorted(
        [
            {"feature": name, "coefficient": round(value, 5), "magnitude": round(abs(value), 5)}
            for name, value in coefficients.items()
        ],
        key=lambda item: item["magnitude"],
        reverse=True,
    )
    artifact = {
        "intercept": float(weights[0]),
        "coefficients": coefficients,
        "feature_importance": feature_importance,
        "feature_means": dict(zip(feature_names, means.tolist())),
        "feature_stds": dict(zip(feature_names, stds.tolist())),
    }
    con.execute(
        "INSERT INTO model_versions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            model_id,
            "Blue win logistic regression",
            "logistic_regression_numpy",
            "blue_win_label",
            dataset_version,
            json.dumps(feature_names),
            "deterministic replay_id md5 holdout; fallback every fifth replay for class balance",
            json.dumps(test_metrics, sort_keys=True),
            json.dumps(calibration, sort_keys=True),
            json.dumps(artifact, sort_keys=True),
            created_at,
        ],
    )
    con.execute(
        "INSERT INTO experiment_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            _now_id("exp_win_prediction"),
            model_id,
            "win_prediction",
            dataset_version,
            baseline_id,
            json.dumps({"model": test_metrics, "baseline": baseline_metrics}, sort_keys=True),
            "Outcome labels are inferred from goal events; tie replays are excluded from this supervised run.",
            created_at,
        ],
    )

    prediction_rows = []
    for idx, replay_id in enumerate(replay_ids):
        contributions = [
            {
                "feature": name,
                "contribution": round(float(x[idx, f_idx] * weights[f_idx + 1]), 5),
                "value_z": round(float(x[idx, f_idx]), 4),
            }
            for f_idx, name in enumerate(feature_names)
        ]
        probability = float(probs[idx])
        prediction_rows.append(
            [
                f"{model_id}:{replay_id}",
                str(replay_id),
                model_id,
                "replay",
                str(replay_id),
                "blue_win_probability",
                "blue_win" if probability >= 0.5 else "orange_win",
                probability,
                probability - 0.5,
                json.dumps(sorted(contributions, key=lambda item: abs(item["contribution"]), reverse=True)[:5], sort_keys=True),
                created_at,
            ]
        )
    con.executemany("INSERT INTO predictions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", prediction_rows)
    return {
        "status": "trained",
        "model_version_id": model_id,
        "baseline_model_version_id": baseline_id,
        "metrics": test_metrics,
        "baseline_metrics": baseline_metrics,
        "labeled_replays": int(len(rows)),
        "features": feature_names,
    }


def _kmeans(x: np.ndarray, k: int, *, iterations: int = 50) -> tuple[np.ndarray, np.ndarray, float]:
    if len(x) == 0:
        return np.array([]), np.empty((0, x.shape[1])), 0.0
    k = max(1, min(k, len(x)))
    initial_idx = np.linspace(0, len(x) - 1, k).round().astype(int)
    centers = x[initial_idx].copy()
    labels = np.zeros(len(x), dtype=int)
    for _ in range(iterations):
        distances = np.linalg.norm(x[:, None, :] - centers[None, :, :], axis=2)
        new_labels = distances.argmin(axis=1)
        new_centers = centers.copy()
        for cluster in range(k):
            if (new_labels == cluster).any():
                new_centers[cluster] = x[new_labels == cluster].mean(axis=0)
        if np.array_equal(new_labels, labels) and np.allclose(new_centers, centers):
            break
        labels = new_labels
        centers = new_centers
    inertia = float(np.square(x - centers[labels]).sum())
    return labels, centers, inertia


def _style_labels(centers: np.ndarray) -> dict[str, str]:
    labels: dict[str, str] = {}
    for idx, center in enumerate(centers):
        values = dict(zip(TEAM_FEATURE_COLUMNS, center.tolist()))
        if values.get("pressure_rate", 0) + values.get("attack_zone_rate", 0) > 0.8:
            label = "pressure-forward"
        elif values.get("clutch_boost_advantage", 0) + values.get("boost_late", 0) > 0.8:
            label = "boost-control"
        elif values.get("aerial_rate", 0) + values.get("touch_rate", 0) > 0.8:
            label = "aerial-tempo"
        elif values.get("starvation_rate", 0) + values.get("overcommit_rate", 0) > 0.8:
            label = "risk-heavy"
        else:
            label = f"balanced-style-{idx + 1}"
        labels[str(idx)] = label
    return labels


def train_style_clustering(con: duckdb.DuckDBPyConnection, *, k: int = 3) -> dict[str, Any]:
    rows = con.execute(
        f"""
        SELECT team_id, replay_id, {", ".join(TEAM_FEATURE_COLUMNS)}
        FROM features_team_match
        ORDER BY team_id
        """
    ).fetchall()
    if len(rows) < 3:
        return {"status": "skipped", "reason": "Need at least 3 team-match rows", "rows": len(rows)}

    team_ids = [row[0] for row in rows]
    replay_ids = [row[1] for row in rows]
    raw = np.array([[float(value) if value is not None else np.nan for value in row[2:]] for row in rows], dtype=float)
    means = np.nanmean(raw, axis=0)
    means = np.where(np.isfinite(means), means, 0.0)
    filled = np.where(np.isnan(raw), means, raw)
    stds = filled.std(axis=0)
    stds = np.where(stds < 1e-9, 1.0, stds)
    x = (filled - means) / stds

    labels, centers, inertia = _kmeans(x, k)
    _, baseline_centers, baseline_inertia = _kmeans(x, 1)
    created_at = datetime.now(timezone.utc)
    dataset_version = con.execute("SELECT any_value(dataset_version) FROM replays").fetchone()[0]
    baseline_id = _now_id("baseline_style_cluster")
    model_id = _now_id("style_kmeans")

    con.execute(
        "INSERT INTO model_versions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            baseline_id,
            "Single style baseline",
            "single_cluster_baseline",
            "team_style",
            dataset_version,
            json.dumps(TEAM_FEATURE_COLUMNS),
            "all team-match rows; no labels available",
            json.dumps({"k": 1, "inertia": round(baseline_inertia, 4)}, sort_keys=True),
            json.dumps([], sort_keys=True),
            json.dumps({"centers": baseline_centers.tolist()}, sort_keys=True),
            created_at,
        ],
    )
    metrics = {
        "k": int(centers.shape[0]),
        "inertia": round(inertia, 4),
        "baseline_inertia": round(baseline_inertia, 4),
        "relative_inertia_reduction": round(1.0 - (inertia / baseline_inertia), 4) if baseline_inertia else None,
        "rows": len(rows),
    }
    artifact = {
        "centers_z": centers.tolist(),
        "feature_means": dict(zip(TEAM_FEATURE_COLUMNS, means.tolist())),
        "feature_stds": dict(zip(TEAM_FEATURE_COLUMNS, stds.tolist())),
        "style_labels": _style_labels(centers),
    }
    con.execute(
        "INSERT INTO model_versions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            model_id,
            "Team style k-means",
            "kmeans_numpy",
            "team_style",
            dataset_version,
            json.dumps(TEAM_FEATURE_COLUMNS),
            "all team-match rows; no labels available",
            json.dumps(metrics, sort_keys=True),
            json.dumps([], sort_keys=True),
            json.dumps(artifact, sort_keys=True),
            created_at,
        ],
    )
    con.execute(
        "INSERT INTO experiment_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            _now_id("exp_style_clustering"),
            model_id,
            "style_clustering",
            dataset_version,
            baseline_id,
            json.dumps({"model": metrics, "baseline": {"inertia": baseline_inertia}}, sort_keys=True),
            "Descriptive clustering compares multi-style segmentation against a one-cluster baseline.",
            created_at,
        ],
    )

    style_names = artifact["style_labels"]
    prediction_rows = []
    for idx, team_id in enumerate(team_ids):
        cluster = int(labels[idx])
        distance = float(np.linalg.norm(x[idx] - centers[cluster]))
        prediction_rows.append(
            [
                f"{model_id}:{team_id}",
                replay_ids[idx],
                model_id,
                "team_match",
                team_id,
                "team_style_cluster",
                style_names.get(str(cluster), f"style_{cluster}"),
                None,
                -distance,
                json.dumps({"cluster": cluster, "distance": round(distance, 4)}, sort_keys=True),
                created_at,
            ]
        )
    con.executemany("INSERT INTO predictions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", prediction_rows)
    return {"status": "trained", "model_version_id": model_id, "baseline_model_version_id": baseline_id, "metrics": metrics}


def run_model_pipeline(serving_db: Path | None = None) -> dict[str, Any]:
    settings = get_settings()
    serving_db = Path(serving_db or settings.serving_db)
    con = duckdb.connect(str(serving_db))
    try:
        return {
            "win_prediction": train_win_prediction(con),
            "style_clustering": train_style_clustering(con),
        }
    finally:
        con.close()


def compare_teams(con: duckdb.DuckDBPyConnection, team_a_id: str, team_b_id: str) -> dict[str, Any]:
    rows = con.execute(
        f"""
        SELECT team_id, team_color, replay_id, {", ".join(TEAM_FEATURE_COLUMNS)}, goals_for, goals_against, win_result
        FROM features_team_match
        WHERE team_id IN (?, ?)
        ORDER BY team_id
        """,
        [team_a_id, team_b_id],
    ).fetchall()
    if len(rows) != 2:
        missing = sorted(set([team_a_id, team_b_id]) - {row[0] for row in rows})
        raise ValueError(f"Unknown team ids: {', '.join(missing)}")

    by_id = {row[0]: row for row in rows}
    a = by_id[team_a_id]
    b = by_id[team_b_id]
    a_features = dict(zip(TEAM_FEATURE_COLUMNS, [float(value or 0.0) for value in a[3:3 + len(TEAM_FEATURE_COLUMNS)]]))
    b_features = dict(zip(TEAM_FEATURE_COLUMNS, [float(value or 0.0) for value in b[3:3 + len(TEAM_FEATURE_COLUMNS)]]))
    weights = {
        "possession_rate": 1.25,
        "attack_zone_rate": 0.8,
        "clutch_boost_advantage": 0.035,
        "touch_rate": 0.9,
        "pressure_rate": 1.1,
        "aerial_rate": 0.45,
        "starvation_rate": -0.9,
        "overcommit_rate": -0.7,
        "goal_pressure_ratio": 0.08,
    }
    contributions = []
    score = 0.0
    for feature, weight in weights.items():
        contribution = (a_features.get(feature, 0.0) - b_features.get(feature, 0.0)) * weight
        score += contribution
        contributions.append(
            {
                "feature": feature,
                "team_a": round(a_features.get(feature, 0.0), 4),
                "team_b": round(b_features.get(feature, 0.0), 4),
                "contribution": round(contribution, 5),
            }
        )
    probability = 1.0 / (1.0 + math.exp(-max(min(score, 8.0), -8.0)))
    sorted_reasons = sorted(contributions, key=lambda item: abs(item["contribution"]), reverse=True)
    return {
        "team_a_id": team_a_id,
        "team_b_id": team_b_id,
        "team_a_replay_id": a[2],
        "team_b_replay_id": b[2],
        "team_a_win_probability": round(probability, 4),
        "team_b_win_probability": round(1.0 - probability, 4),
        "predicted_label": team_a_id if probability >= 0.5 else team_b_id,
        "score": round(score, 5),
        "reason_codes": sorted_reasons[:6],
        "assumption": "Comparison uses normalized side-team features because real organization identities are not present in the raw corpus.",
    }

