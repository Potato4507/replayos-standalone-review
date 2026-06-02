# ReplayOS Model Evaluation

Run the current evaluation with:

```powershell
python scripts\run_pipeline.py
```

The pipeline stores all run details in `model_versions` and `experiment_runs`.

## Latest Local Run

Verified on April 11, 2026 against `D:\RocketLeagueFrames\rl_frames_60hz.duckdb`.

Warehouse counts:

- `replays`: 38,954
- `events`: 14,973
- `features_replay`: 93
- `features_team_match`: 186
- `features_player_match`: 550
- `players`: 422

Predictive run:

- Labeled replays: 92
- Holdout rows: 24
- Logistic regression accuracy: 1.0000
- Logistic regression log loss: 0.1355
- Logistic regression Brier score: 0.0255
- Logistic regression AUC: 1.0000
- Baseline accuracy: 0.6250
- Baseline log loss: 0.6626
- Baseline Brier score: 0.2349
- Baseline AUC: 0.4593

Style clustering run:

- Team-match rows: 186
- K: 3
- Inertia: 1825.9206
- One-cluster baseline inertia: 2418.0000
- Relative inertia reduction: 0.2449

## Predictive Model

Target: `blue_win_label`, inferred from goal events.

Features: numeric semantic frame features from `features_replay`; direct outcome columns are excluded.

Split logic: deterministic replay-id hash holdout, with an every-fifth replay fallback if the holdout lacks class balance.

Baseline: majority-probability model trained on the training split.

Tracked outputs:

- Accuracy
- Log loss
- Brier score
- AUC when both classes are present
- Calibration bins
- Feature coefficients and local contribution reason codes

## Descriptive Model

Target: team style cluster.

Features: long-form `features_team_match` rows, including possession, boost, pressure, aerial, starvation, overcommit, and goal-pressure signals.

Baseline: one-cluster k-means.

Tracked outputs:

- K
- Inertia
- Baseline inertia
- Relative inertia reduction
- Cluster centers
- Style labels

## Error Analysis

The first supervised model is intentionally modest because outcome labels are inferred and the current feature table covers a subset of the replay corpus. Calibration should be read as directional until more labeled replay features are available. The most important next validation step is to compare derived winners and team identities against trusted replay metadata.
