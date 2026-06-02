from __future__ import annotations

import unittest

import numpy as np

from replayos.analytics import _auc, _metrics


class AnalyticsMetricTests(unittest.TestCase):
    def test_auc_perfect_ordering(self) -> None:
        auc = _auc(np.array([0, 0, 1, 1]), np.array([0.1, 0.2, 0.8, 0.9]))
        self.assertEqual(auc, 1.0)

    def test_metrics_shape(self) -> None:
        metrics = _metrics(np.array([0, 1]), np.array([0.25, 0.75]))
        self.assertEqual(metrics["n"], 2)
        self.assertEqual(metrics["accuracy"], 1.0)
        self.assertLess(metrics["log_loss"], 0.7)


if __name__ == "__main__":
    unittest.main()

