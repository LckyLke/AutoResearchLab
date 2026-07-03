"""Evaluation for the TSP demo — protected; the agent cannot edit this.

Scores solver.solve() on a fixed, deterministic set of 120 cities:
  - the tour must visit every city exactly once (closed tour)
  - the solver must finish within 15 seconds
Metric: tour_length (minimize).
"""

import json
import math
import random
import time

import solver

N_CITIES = 120
TIME_BUDGET_SECONDS = 15.0


def make_cities():
    rng = random.Random(7)
    return [(rng.random(), rng.random()) for _ in range(N_CITIES)]


def tour_length(cities, tour):
    total = 0.0
    for i, a in enumerate(tour):
        x1, y1 = cities[a]
        x2, y2 = cities[tour[(i + 1) % len(tour)]]
        total += math.hypot(x2 - x1, y2 - y1)
    return total


def main():
    cities = make_cities()

    start = time.perf_counter()
    tour = solver.solve([tuple(c) for c in cities])
    elapsed = time.perf_counter() - start

    if elapsed > TIME_BUDGET_SECONDS:
        raise SystemExit(f"solver took {elapsed:.1f}s — budget is {TIME_BUDGET_SECONDS:.0f}s")
    try:
        tour = [int(i) for i in tour]
    except (TypeError, ValueError):
        raise SystemExit("solve() must return a list of city indices")
    if sorted(tour) != list(range(len(cities))):
        raise SystemExit("tour must be a permutation visiting every city exactly once")

    metrics = {
        "tour_length": round(tour_length(cities, tour), 4),
        "solver_seconds": round(elapsed, 3),
    }
    with open("metrics.json", "w") as f:
        json.dump(metrics, f)
    print(json.dumps(metrics))


if __name__ == "__main__":
    main()
