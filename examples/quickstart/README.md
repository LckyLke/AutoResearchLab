# Quickstart example

A tiny regression problem to try AutoResearchLab end-to-end in minutes.

- `predict.py` — the "model" (editable). Starts by predicting a constant.
- `eval.py` — the grader (protected). Scores RMSE on held-out synthetic data
  and writes `metrics.json`.

Setup in the GUI:

| Field          | Value                        |
| -------------- | ---------------------------- |
| Workspace      | this folder                  |
| Editable files | `predict.py`                 |
| Eval command   | `python eval.py`             |
| Metric         | `rmse`, minimize             |

The baseline scores around RMSE ≈ 12. A linear fit gets it to ~4, a
quadratic fit to ~1, and modelling the sine term approaches the noise
floor of ~0.35.
