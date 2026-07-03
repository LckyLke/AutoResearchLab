"""Traveling-salesman solver — AutoResearchLab agents may edit this file.

Contract (do not change):
    solve(cities: list[tuple[float, float]]) -> list[int]
Return an ordering of ALL city indices (a permutation of range(len(cities))).
The tour is closed: the last city connects back to the first.

The evaluation enforces a 15 second compute budget — favour good
heuristics over brute force.
"""


def solve(cities):
    # Deliberately naive starting point: visit the cities in input order.
    # (Input order is random, so this tour criss-crosses the whole map.)
    return list(range(len(cities)))
