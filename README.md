# AutoResearchLab

> Inspired by [karpathy/autoresearch](https://github.com/karpathy/autoresearch) —
> the same "agent edits, eval judges, keep only improvements" loop, generalized
> to any project/metric/agent and wrapped in a web GUI.

An autonomous, eval-driven improvement loop with a web GUI. Point it at any
project folder, choose which files an AI agent may edit, provide an
evaluation command that produces a metric — then start the loop and walk
away. Every iteration the agent proposes a change; the evaluation is the
only judge; improvements become the new **champion**. All versions, diffs,
logs and the metric trend are kept and shown live.

```
┌────────────┐   prompt (+history)   ┌────────────┐   score    ┌───────────┐
│  champion  │ ────────────────────▶ │   agent    │ ─────────▶ │   eval    │
│  workspace │      edits files      │ (sandboxed)│            │ (metrics) │
└────────────┘ ◀──────────────────── └────────────┘            └─────┬─────┘
        ▲            keep only whitelisted changes                   │
        └──────────────── promote if metric improved ◀───────────────┘
```

## Quick start

```bash
pip install -r requirements.txt
python run.py                # → http://127.0.0.1:8321
```


To set up your own experiment:

1. **New experiment** → point at a folder (browse popup or path).
2. Tick the files the agent may edit (searchable list, glob patterns) —
   **everything else is hard-blocked**: changes outside the whitelist are
   detected and reverted automatically.
3. Enter the eval command (e.g. `python eval.py`), the metric key
   (e.g. `rmse`) and its direction. Optionally add a **holdout eval
   command** — an overfitting guard run only on new champions, whose score
   is shown to you but never to the agent.
4. Pick the Python environment the eval and agent should run in — a conda
   env (auto-listed), a virtualenv, or system Python. Verify it with one
   click; the agent is told which Python version and packages it provides.
5. Pick an agent — Claude Code CLI by default (Fable/Opus/Sonnet/Haiku
   dropdown); the Anthropic API, any OpenAI-compatible endpoint (Ollama,
   vLLM, LM Studio, …), or any custom CLI also work.
6. Set budgets: per-iteration agent time, max iterations, total runtime,
   and an optional **cost budget in $** (from the agent's own usage
   reporting).
7. Adjust the instructions (a default and an annotated template are built in).
8. **Start loop.** Human out of the loop from here: a live console streams
   the agent's actions (tool calls, thoughts, eval runs) with a phase
   stepper and per-iteration timer — or close the tab; the loop runs
   server-side until you stop it or a budget is hit.

At any point you can drop **knowledge** onto an experiment — papers (PDFs
are text-extracted, with OCR fallback for scans), notes, specs. The agent
gets an auto-generated index at `knowledge/INDEX.md` and reads documents on
demand; the folder is read-only to it.

## The evaluation contract

The eval command runs with the (isolated) workspace as its working
directory and must produce a JSON object containing your metric, either by
writing `metrics.json` or printing JSON as the last stdout line:

```json
{"rmse": 1.234, "r2": 0.97}
```

Extra keys are stored and displayed. The primary metric decides the
champion; ties and regressions are rejected. Eval scripts can never be
whitelisted as editable — the agent cannot grade itself.

**Tip — enforce a fixed compute budget inside your eval**: when the metric depends on how long something
runs (training, search, simulation), have the eval script cap the compute
(e.g. "train for exactly 5 minutes", "solver gets 15 seconds" like the
bundled TSP demo). Iterations stay comparable, and the agent optimizes
*what to do with the budget* instead of just using more of it.

## How the loop stays honest

- **Sandbox**: your original folder is never touched — experiments run on a
  snapshot. Before each agent run, non-editable files are made read-only;
  afterwards the whole tree is re-hashed and any illegal change (modify /
  delete / create outside the whitelist) is reverted and logged as a
  violation. Eval/holdout scripts, the metrics file, and the `knowledge/`
  folder can never be edited by the agent. Temporary files belong in
  `.scratch/` — sanctioned, never snapshotted, wiped each iteration.
- **Holdout eval** (optional): a hidden second eval scores every new
  champion on a split the agent never sees, drawn as its own line on the
  chart — divergence between the curves is overfitting made visible.
- **Agent notebook**: the agent maintains `AGENT_NOTES.md`, a persistent
  memory that survives rejected iterations — recorded failures are never
  retried. Viewable and editable from the dashboard to steer the search.
- **Cost tracking**: per-iteration and cumulative $ / tokens on the
  dashboard, with an optional hard cost budget as a stop condition.

## Results on disk

Everything is plain files under `experiments/<id>/` — see
`autoresearch/storage.py` for the layout: per-iteration `meta.json`,
`agent.log`, `changes.diff` and a file snapshot, plus `history.jsonl`, the
current champion, `notebook.md` (the agent's memory) and the knowledge
library. Any version (including the champion) can be downloaded from the
GUI as a full runnable workspace zip. `experiments/` is git-ignored — it is
data, not source.

## Agents

| Type            | What it runs                                              |
| --------------- | --------------------------------------------------------- |
| `claude_code`   | `claude -p <task>` headless (default)                      |
| `anthropic_api` | Anthropic Messages API with a built-in file-edit tool loop |
| `openai_compat` | any OpenAI-compatible endpoint with function calling       |
| `command`       | any CLI: `{prompt_file}` and `{workdir}` placeholders      |

## Development

```bash
pytest             # unit + integration tests (no model calls)
python run.py      # dev server
```

Set `AUTORESEARCH_DATA_DIR` to relocate where experiment results are stored.
