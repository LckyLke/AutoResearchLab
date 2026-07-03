"""Evaluation script — this file is protected; the agent cannot edit it.

Generates a deterministic synthetic dataset, scores predict.predict() by
RMSE on a held-out split, and writes metrics.json.
Metric: rmse (minimize).
"""

import json
import math
import random

import predict


def make_data():
    rng = random.Random(42)
    xs = [i / 10.0 for i in range(-60, 61)]
    ys = [0.8 * x**2 - 1.5 * x + 2.0 + math.sin(2.2 * x) + rng.gauss(0, 0.35) for x in xs]
    pairs = list(zip(xs, ys))
    rng.shuffle(pairs)
    split = int(len(pairs) * 0.7)
    return pairs[:split], pairs[split:]


def rmse(y_true, y_pred):
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(y_true, y_pred)) / len(y_true))


def main():
    train, test = make_data()
    xs_test = [x for x, _ in test]
    ys_test = [y for _, y in test]
    ys_hat = predict.predict(xs_test)
    if not isinstance(ys_hat, list) or len(ys_hat) != len(xs_test):
        raise SystemExit("predict() must return a list of the same length as its input")
    score = rmse(ys_test, [float(v) for v in ys_hat])
    metrics = {"rmse": round(score, 6), "n_test": len(xs_test)}
    with open("metrics.json", "w") as f:
        json.dump(metrics, f)
    print(json.dumps(metrics))


if __name__ == "__main__":
    main()
