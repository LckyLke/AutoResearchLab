# Traveling salesman demo

The default AutoResearchLab demo problem: find the shortest closed tour
through 120 fixed cities on the unit square.

- `solver.py` — the solver (editable). Starts by visiting cities in input
  order, which criss-crosses the map (~62 length units).
- `eval.py` — the grader (protected). Validates the tour, enforces a 15 s
  compute budget, writes `metrics.json` with `tour_length`.

Why it's a good demo: there is a long, satisfying ladder of improvements —

| Approach                          | ~tour length |
| --------------------------------- | ------------ |
| input order (baseline)            | ~63.6        |
| nearest neighbour                 | ~12.3        |
| NN + 2-opt                        | ~8.5         |
| NN + 2-opt + Or-opt / annealing   | ~8           |
| theoretical optimum (BHH est.)    | ~7.8         |

Each rung requires a genuinely better idea, not just parameter tweaks —
perfect for watching an agent climb the champion ladder.
