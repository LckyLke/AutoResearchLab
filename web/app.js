/* AutoResearchLab GUI. Vanilla JS, no build step. */
"use strict";

// ---------------------------------------------------------------- utilities

const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) { /* noop */ }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return res.json();
}

function toast(msg, ms = 3200) {
  const el = $("toast");
  el.textContent = msg;
  el.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add("hidden"), ms);
}

function fmtMetric(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  if (v === 0) return "0";
  const abs = Math.abs(v);
  if (abs >= 1000) return v.toLocaleString(undefined, { maximumFractionDigits: 1 });
  if (abs >= 1) return v.toPrecision(5).replace(/\.?0+$/, "");
  return v.toPrecision(4);
}

function fmtDuration(s) {
  if (s == null) return "–";
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${(s - m * 60).toFixed(0)}s`;
}

function fmtClock(seconds) {
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

function esc(text) {
  const div = document.createElement("div");
  div.textContent = text ?? "";
  return div.innerHTML;
}

function fmtCost(usage) {
  if (!usage) return "";
  if (usage.cost_usd != null) return `$${usage.cost_usd.toFixed(usage.cost_usd < 1 ? 3 : 2)}`;
  const tok = (usage.input_tokens || 0) + (usage.output_tokens || 0);
  return tok ? `${(tok / 1000).toFixed(0)}k tok` : "";
}

function totalCost(attempts) {
  let usd = 0, tokens = 0;
  for (const h of attempts) {
    const u = h.usage;
    if (!u) continue;
    if (u.cost_usd != null) usd += u.cost_usd;
    else tokens += (u.input_tokens || 0) + (u.output_tokens || 0);
  }
  const parts = [];
  if (usd > 0) parts.push(`$${usd.toFixed(2)}`);
  if (tokens > 0) parts.push(`${(tokens / 1000).toFixed(0)}k tok`);
  return parts.join(" + ");
}

// ---------------------------------------------------------------- state

const state = {
  experiments: [],
  currentId: null,
  detail: null,
  sse: null,
  templates: null,
  condaEnvs: null,
  ignoreDefaults: null,
  // live-run bookkeeping
  live: {
    phase: null,
    iteration: null,
    phaseNames: ["sync", "agent", "eval", "decide"],
    activity: [],           // recent activity lines
    iterStarted: null,      // Date.now() when current iteration began
    clockTimer: null,
    pollTimer: null,
  },
};

// ---------------------------------------------------------------- sidebar

async function refreshSidebar() {
  const data = await api("/api/experiments");
  state.experiments = data.experiments;
  $("data-dir").textContent = `results in ${data.data_dir}`;
  const list = $("exp-list");
  list.innerHTML = "";
  for (const exp of state.experiments) {
    const btn = document.createElement("button");
    btn.className = "exp-item" + (exp.id === state.currentId ? " active" : "");
    const status = exp.status || "idle";
    const dotCls = status === "running" ? "running" :
                   status === "finished" ? "finished" :
                   status === "error" ? "error" : "";
    btn.innerHTML = `<span class="exp-dot ${dotCls}"></span>
                     <span class="exp-name">${esc(exp.name)}</span>
                     <span class="exp-status">${esc(status)}</span>`;
    btn.onclick = () => openExperiment(exp.id);
    list.appendChild(btn);
  }
}

// ---------------------------------------------------------------- views

function showView(name) {
  for (const v of ["empty", "setup", "dash"]) {
    $(`view-${v}`).classList.toggle("hidden", v !== name);
  }
  if (name !== "dash") setDocTitle(null);
}

function setDocTitle(info) {
  document.title = info ? `${info} · AutoResearchLab` : "AutoResearchLab";
}

// ---------------------------------------------------------------- setup wizard

const FUN_NAMES = [
  "curious-curie", "dashing-darwin", "fearless-fermi", "humming-hopper",
  "keen-kepler", "lively-lovelace", "mighty-maxwell", "nimble-noether",
  "plucky-planck", "swift-shannon", "turbo-turing", "witty-wiener",
  "quantum-quokka", "cosmic-capybara", "atomic-axolotl", "stellar-stoat",
  "gradient-gecko", "lambda-llama", "tensor-tapir", "boolean-badger",
  "recursive-raven", "stochastic-swan", "greedy-gopher", "annealed-antelope",
  "eigen-elephant", "fourier-fox", "hamming-hedgehog", "markov-marmot",
  "newton-narwhal", "pareto-parrot", "simplex-seal", "entropy-echidna",
];

function randomName() {
  return FUN_NAMES[Math.floor(Math.random() * FUN_NAMES.length)];
}

const CLAUDE_MODELS = [
  ["claude-fable-5", "Fable 5 — most capable"],
  ["claude-opus-4-8", "Opus 4.8 — powerful all-rounder"],
  ["claude-sonnet-5", "Sonnet 5 — fast & smart"],
  ["claude-haiku-4-5", "Haiku 4.5 — fastest"],
];

function renderModelField() {
  const type = $("f-agent-type").value;
  const selField = $("model-select-field");
  const customField = $("model-custom-field");
  const sel = $("f-model-select");

  if (type === "command") {           // custom CLIs pick their own model
    selField.classList.add("hidden");
    customField.classList.add("hidden");
    return;
  }
  if (type === "openai_compat") {     // free-form: whatever the endpoint serves
    selField.classList.add("hidden");
    customField.classList.remove("hidden");
    $("model-custom-label").textContent = "Model name (as served by the endpoint)";
    $("f-model").placeholder = "llama3.1";
    return;
  }
  // claude_code / anthropic_api: curated dropdown + custom escape hatch
  selField.classList.remove("hidden");
  const defaultLabel = type === "claude_code"
    ? "Default (your Claude Code setting)" : "Default (Opus 4.8)";
  sel.innerHTML = "";
  for (const [value, label] of [["", defaultLabel], ...CLAUDE_MODELS, ["custom", "Custom model ID…"]]) {
    const opt = document.createElement("option");
    opt.value = value;
    opt.textContent = label;
    sel.appendChild(opt);
  }
  $("model-custom-label").textContent = "Custom model ID";
  $("f-model").placeholder = "claude-…";
  customField.classList.toggle("hidden", sel.value !== "custom");
}

function selectedModel() {
  const type = $("f-agent-type").value;
  if (type === "command") return null;
  if (type === "openai_compat") return $("f-model").value.trim() || null;
  const v = $("f-model-select").value;
  if (v === "custom") return $("f-model").value.trim() || null;
  return v || null;
}

const agentExtraTemplates = {
  claude_code: `
    <label class="field"><span>Claude binary</span>
      <input type="text" id="f-claude-binary" value="claude" /></label>
    <label class="field"><span>Allowed tools (space-separated)</span>
      <input type="text" id="f-allowed-tools" value="Read Edit Write MultiEdit Glob Grep Bash" /></label>
    <label class="check-field">
      <input type="checkbox" id="f-skip-perms" />
      <span><code>--dangerously-skip-permissions</code> (faster, less safe)</span></label>
    <p class="hint">Runs <code>claude -p</code> headless in an isolated copy of your workspace.
       File whitelisting is enforced by AutoResearchLab regardless of the agent's tool permissions.</p>`,
  anthropic_api: `
    <label class="field"><span>API key environment variable</span>
      <input type="text" id="f-api-key-env" value="ANTHROPIC_API_KEY" /></label>
    <p class="hint">Direct Messages API with a built-in file-editing tool loop.
       Default model: <code>claude-opus-4-8</code>.</p>`,
  openai_compat: `
    <label class="field"><span>Base URL</span>
      <input type="text" id="f-base-url" value="http://localhost:11434/v1" /></label>
    <label class="field"><span>API key environment variable (optional)</span>
      <input type="text" id="f-api-key-env" value="OPENAI_API_KEY" /></label>
    <p class="hint">Works with Ollama, llama.cpp server, vLLM, LM Studio, OpenAI, …
       The endpoint must support function calling.</p>`,
  command: `
    <label class="field"><span>Command template</span>
      <input type="text" id="f-cmd-template" placeholder="aider --yes --message-file {prompt_file}" /></label>
    <p class="hint">Any CLI. Placeholders: <code>{prompt_file}</code> (file containing the task),
       <code>{workdir}</code>. Must exit 0 and edit files in place.</p>`,
};

function renderAgentExtra() {
  $("agent-extra").innerHTML = agentExtraTemplates[$("f-agent-type").value];
  renderModelField();
}

// -- folder picker --

const browse = { targetId: null, path: null, onSelect: null };

const FOLDER_SVG = `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor"
  stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
  <path d="M3 7V5.5A1.5 1.5 0 0 1 4.5 4h4l2 2.5h8A1.5 1.5 0 0 1 20 8v9.5a1.5 1.5 0 0 1-1.5 1.5h-14A1.5 1.5 0 0 1 3 17.5Z"/></svg>`;

async function openBrowser(targetId, onSelect) {
  browse.targetId = targetId;
  browse.onSelect = onSelect || null;
  $("browse-modal").classList.remove("hidden");
  await browseTo($(targetId).value.trim() || "~");
}

async function browseTo(path) {
  try {
    const d = await api("/api/browse", { method: "POST", body: JSON.stringify({ path }) });
    browse.path = d.path;
    $("browse-path").value = d.path;
    $("browse-current").textContent = d.path;
    const list = $("browse-list");
    list.innerHTML = "";
    if (d.path !== d.parent) {
      const up = document.createElement("button");
      up.className = "browse-item up";
      up.innerHTML = `${FOLDER_SVG}<span class="browse-name">.. <span class="muted">(up)</span></span>`;
      up.onclick = () => browseTo(d.parent);
      list.appendChild(up);
    }
    for (const dir of d.dirs) {
      const item = document.createElement("button");
      item.className = "browse-item";
      item.innerHTML = `${FOLDER_SVG}<span class="browse-name">${esc(dir)}</span>`;
      item.onclick = () => browseTo(`${d.path.replace(/\/+$/, "")}/${dir}`);
      list.appendChild(item);
    }
    if (!d.dirs.length) {
      const note = document.createElement("div");
      note.className = "browse-empty";
      note.textContent = "No subfolders here.";
      list.appendChild(note);
    }
  } catch (e) {
    toast(e.message);
  }
}

function closeBrowser() {
  $("browse-modal").classList.add("hidden");
}

// -- numeric input guards + custom steppers --
// Native spin arrows are hidden (tiny, ugly, always step by 1). Each field
// gets crisp −/+ buttons with a step size that matches what it measures,
// and the open-ended budgets treat "below minimum" as ∞ (empty).

const NUM_FIELDS = [
  { id: "f-max-iter",      step: 1,   min: 1,  infinity: true },
  { id: "f-max-runtime",   step: 300, min: 60, infinity: true },
  { id: "f-agent-timeout", step: 60,  min: 10, fallback: 1800 },
  { id: "f-eval-timeout",  step: 30,  min: 1,  fallback: 600 },
];

function attachNumericGuards() {
  const digitOnly = (ev) => {
    if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
    if (ev.key.length === 1 && !/[0-9]/.test(ev.key)) ev.preventDefault();
  };

  for (const spec of NUM_FIELDS) {
    const el = $(spec.id);

    const normalize = (v) => {
      if (!Number.isFinite(v) || v < spec.min) {
        return spec.infinity ? "" : spec.fallback;  // "" renders the ∞ placeholder
      }
      return v;
    };

    // wrap in a stepper control: [ input ][ − ][ + ]
    const wrap = document.createElement("div");
    wrap.className = "numctl";
    el.parentNode.insertBefore(wrap, el);
    wrap.appendChild(el);
    const mkBtn = (label, cls, delta) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = `num-btn ${cls}`;
      b.textContent = label;
      b.tabIndex = -1;
      b.title = `${delta > 0 ? "+" : "−"}${Math.abs(delta)}`;
      b.onclick = () => {
        const cur = parseInt(el.value, 10);
        let next;
        if (!Number.isFinite(cur)) {
          // stepping up from ∞ starts at the minimum; stepping down stays ∞
          next = delta > 0 ? spec.min : NaN;
        } else {
          next = cur + delta;
        }
        el.value = normalize(next);
        el.dispatchEvent(new Event("change"));
      };
      return b;
    };
    wrap.appendChild(mkBtn("−", "minus", -spec.step));
    wrap.appendChild(mkBtn("＋", "plus", spec.step));

    el.addEventListener("keydown", digitOnly);
    el.addEventListener("blur", () => {
      el.value = normalize(parseInt(el.value, 10));
    });
  }

  // cost budget: decimal money field, below zero / junk → ∞ (empty)
  const cost = $("f-max-cost");
  cost.addEventListener("keydown", (ev) => {
    if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
    if (ev.key.length === 1 && !/[0-9.]/.test(ev.key)) ev.preventDefault();
  });
  cost.addEventListener("blur", () => {
    const v = parseFloat(cost.value);
    cost.value = v > 0 ? v : "";
  });
}

// -- environment step --

function selectedEnvType() {
  return document.querySelector("#env-seg .seg-btn.active")?.dataset.env || "conda";
}

function renderEnvFields() {
  const type = selectedEnvType();
  $("env-conda-field").classList.toggle("hidden", type !== "conda");
  $("env-venv-field").classList.toggle("hidden", type !== "venv");
  $("btn-check-env").disabled = false;
  $("env-check-result").textContent = "";
  $("env-check-result").className = "env-check";
}

async function loadCondaEnvs() {
  try {
    const data = await api("/api/env/conda");
    state.condaEnvs = data.envs;
    const sel = $("f-conda-env");
    sel.innerHTML = "";
    if (!data.envs.length) {
      sel.innerHTML = `<option value="">no conda found on this machine</option>`;
      return;
    }
    for (const e of data.envs) {
      const opt = document.createElement("option");
      opt.value = e.name;
      opt.textContent = `${e.name}  (${e.prefix})`;
      sel.appendChild(opt);
    }
  } catch (_) {
    $("f-conda-env").innerHTML = `<option value="">could not list conda envs</option>`;
  }
}

function collectEnvConfig() {
  const type = selectedEnvType();
  return {
    type,
    conda_env: type === "conda" ? $("f-conda-env").value : "",
    venv_path: type === "venv" ? $("f-venv-path").value.trim() : "",
    inform_agent: $("f-inform-agent").checked,
  };
}

async function checkEnv() {
  const out = $("env-check-result");
  out.className = "env-check";
  out.textContent = "checking…";
  $("btn-check-env").disabled = true;
  try {
    const r = await api("/api/env/check", { method: "POST", body: JSON.stringify(collectEnvConfig()) });
    out.className = "env-check ok";
    out.textContent = `✓ Python ${r.python_version} · ${r.package_count} packages`;
  } catch (e) {
    out.className = "env-check err";
    out.textContent = `✗ ${e.message}`;
  } finally {
    $("btn-check-env").disabled = false;
  }
}

function extraIgnorePatterns() {
  return $("f-ignore").value.split(",").map((s) => s.trim()).filter(Boolean);
}

async function loadTree() {
  const workspace = $("f-workspace").value.trim();
  if (!workspace) { toast("Enter a workspace path first"); return; }
  const treeEl = $("file-tree");
  treeEl.innerHTML = `<p class="muted">Loading…</p>`;
  try {
    const data = await api("/api/tree", {
      method: "POST",
      body: JSON.stringify({ workspace, extra_ignore: extraIgnorePatterns() }),
    });
    state.ignoreDefaults = data.ignore_defaults;
    $("f-workspace").value = data.workspace;
    if (!data.files.length) {
      treeEl.innerHTML = `<p class="muted">No files found in this folder.</p>`;
      return;
    }
    treeEl.innerHTML = "";
    for (const f of data.files) {
      const label = document.createElement("label");
      label.innerHTML = `<input type="checkbox" value="${esc(f)}" /> <span>${esc(f)}</span>`;
      treeEl.appendChild(label);
    }
    $("tree-toolbar").classList.remove("hidden");
    $("f-tree-search").value = "";
    applyTreeFilter();
  } catch (e) {
    treeEl.innerHTML = `<p class="error">${esc(e.message)}</p>`;
    $("tree-toolbar").classList.add("hidden");
  }
}

// -- file tree search / bulk select (checkbox state survives filtering) --

function treeLabels() {
  return [...$("file-tree").querySelectorAll("label")];
}

function applyTreeFilter() {
  const q = $("f-tree-search").value.trim().toLowerCase();
  let shown = 0;
  for (const label of treeLabels()) {
    const path = label.querySelector("input").value.toLowerCase();
    const visible = !q || path.includes(q);
    label.style.display = visible ? "" : "none";
    if (visible) shown++;
  }
  updateTreeCount(shown);
}

function updateTreeCount(shown = null) {
  const labels = treeLabels();
  if (!labels.length) { $("tree-count").textContent = ""; return; }
  if (shown === null) {
    shown = labels.filter((l) => l.style.display !== "none").length;
  }
  const selected = labels.filter((l) => l.querySelector("input").checked).length;
  const filtered = shown !== labels.length ? `${shown}/${labels.length} shown · ` : `${labels.length} files · `;
  $("tree-count").textContent = `${filtered}${selected} selected`;
}

function setVisibleChecked(checked) {
  for (const label of treeLabels()) {
    if (label.style.display !== "none") {
      label.querySelector("input").checked = checked;
    }
  }
  updateTreeCount();
}

function collectConfig() {
  const editable = [...$("file-tree").querySelectorAll("input:checked")].map((c) => c.value);
  for (const g of $("f-globs").value.split(",")) {
    if (g.trim()) editable.push(g.trim());
  }
  const type = $("f-agent-type").value;
  const agent = { type, model: selectedModel() };
  if (type === "claude_code") {
    agent.claude_binary = $("f-claude-binary").value.trim() || "claude";
    agent.allowed_tools = $("f-allowed-tools").value.trim().split(/\s+/).filter(Boolean);
    agent.skip_permissions = $("f-skip-perms").checked;
  } else if (type === "anthropic_api") {
    agent.api_key_env = $("f-api-key-env").value.trim() || "ANTHROPIC_API_KEY";
  } else if (type === "openai_compat") {
    agent.api_base_url = $("f-base-url").value.trim() || "http://localhost:11434/v1";
    agent.api_key_env = $("f-api-key-env").value.trim() || "OPENAI_API_KEY";
  } else if (type === "command") {
    agent.command_template = $("f-cmd-template").value;
  }
  const cfg = {
    name: $("f-name").value.trim(),
    workspace: $("f-workspace").value.trim(),
    editable_files: editable,
    eval: {
      command: $("f-eval-cmd").value.trim(),
      metric: $("f-metric").value.trim(),
      direction: $("f-direction").value,
      timeout_seconds: parseInt($("f-eval-timeout").value, 10) || 600,
      holdout_command: $("f-holdout-cmd").value.trim(),
    },
    agent,
    environment: collectEnvConfig(),
    budgets: {
      agent_timeout_seconds: parseInt($("f-agent-timeout").value, 10) || 1800,
      max_iterations: $("f-max-iter").value ? parseInt($("f-max-iter").value, 10) : null,
      max_runtime_seconds: $("f-max-runtime").value ? parseInt($("f-max-runtime").value, 10) : null,
      max_cost_usd: parseFloat($("f-max-cost").value) > 0 ? parseFloat($("f-max-cost").value) : null,
    },
  };
  const extraIgnore = extraIgnorePatterns();
  if (extraIgnore.length && state.ignoreDefaults) {
    cfg.ignore_patterns = [...state.ignoreDefaults, ...extraIgnore];
  }
  return cfg;
}

async function createExperiment() {
  $("setup-error").textContent = "";
  $("btn-create").disabled = true;
  try {
    const body = { config: collectConfig(), instructions: $("f-instructions").value };
    const { id } = await api("/api/experiments", { method: "POST", body: JSON.stringify(body) });
    toast("Experiment created — snapshot taken");
    await refreshSidebar();
    openExperiment(id);
  } catch (e) {
    $("setup-error").textContent = e.message;
  } finally {
    $("btn-create").disabled = false;
  }
}

async function openSetup() {
  state.currentId = null;
  closeSSE();
  stopLiveTimers();
  showView("setup");
  await refreshSidebar();
  if (!state.templates) state.templates = await api("/api/templates");
  if (!$("f-instructions").value) $("f-instructions").value = state.templates.default;
  $("f-name").value = randomName();
  renderAgentExtra();
  renderEnvFields();
  if (state.condaEnvs === null) loadCondaEnvs();
}

// ---------------------------------------------------------------- dashboard

async function openExperiment(id) {
  if (state.currentId !== id) resetLive();
  state.currentId = id;
  showView("dash");
  await Promise.all([refreshSidebar(), refreshDetail()]);
  connectSSE(id);
}

async function refreshDetail() {
  if (!state.currentId) return;
  state.detail = await api(`/api/experiments/${state.currentId}`);
  renderDashboard();
}

function championEntry() {
  const hist = state.detail?.history || [];
  let best = null;
  for (const h of hist) if (h.is_champion) best = h;
  return best;
}

function envLabel(cfg) {
  const e = cfg.environment || { type: "system" };
  if (e.type === "conda") return { name: e.conda_env || "conda", kind: "conda" };
  if (e.type === "venv") {
    const parts = (e.venv_path || "venv").replace(/\/+$/, "").split("/");
    return { name: parts[parts.length - 1] || "venv", kind: "venv" };
  }
  return { name: "system", kind: "system python" };
}

function renderDashboard() {
  const d = state.detail;
  if (!d) return;
  const cfg = d.config;
  const hist = d.history || [];
  const attempts = hist.filter((h) => h.iteration > 0);
  const baseline = hist.find((h) => h.iteration === 0);
  const champ = championEntry();
  const running = d.loop?.running;

  $("dash-name").textContent = cfg.name;
  const pill = $("dash-status");
  const status = running ? "running" : (d.status || "idle");
  pill.textContent = status;
  pill.className = "status-pill " +
    (running ? "running" : status === "error" ? "error" : status === "finished" ? "finished" : "");
  $("dash-meta").textContent =
    `${cfg.eval.direction} ${cfg.eval.metric} · ${cfg.agent.type}` +
    (cfg.agent.model ? ` (${cfg.agent.model})` : "") + ` · ${cfg.eval.command}`;

  $("runbar").classList.toggle("on", !!running);
  $("btn-start").classList.toggle("hidden", !!running);
  $("btn-stop").classList.toggle("hidden", !running);
  $("activity-card").classList.toggle("hidden", !running);
  if (running) {
    setDocTitle(state.live.iteration ? `▶ iter ${state.live.iteration}` : "▶ running");
    startLiveTimers();
    renderActivity();
  } else {
    setDocTitle(cfg.name);
    stopLiveTimers();
  }

  // stats
  $("stat-metric-label").textContent = `${cfg.eval.metric} · champion`;
  $("stat-champion").textContent = fmtMetric(champ?.primary);
  {
    let foot = champ
      ? (champ.iteration === 0 ? "baseline is still champion" : `from iteration ${champ.iteration}`)
      : "no successful eval yet";
    if (champ?.holdout?.ok) {
      foot += ` · holdout ${fmtMetric(champ.holdout.primary)}`;
      // overfitting hint: champion improved on the visible eval but its
      // holdout is worse than the previous champion's holdout
      const champs = hist.filter((h) => h.is_champion && h.holdout?.ok);
      if (champs.length >= 2) {
        const prev = champs[champs.length - 2].holdout.primary;
        const cur = champs[champs.length - 1].holdout.primary;
        const worse = cfg.eval.direction === "maximize" ? cur < prev : cur > prev;
        if (worse) foot += " ⚠ holdout regressed — possible overfitting";
      }
    } else if (champ?.holdout && !champ.holdout.ok) {
      foot += " · holdout failed";
    }
    $("stat-champion-foot").textContent = foot;
  }
  $("stat-baseline").textContent = fmtMetric(baseline?.primary);
  const foot = $("stat-improvement");
  if (champ && baseline && baseline.primary != null && champ.primary != null && champ.iteration !== 0) {
    const delta = champ.primary - baseline.primary;
    const pct = baseline.primary !== 0 ? ` (${(Math.abs(delta / baseline.primary) * 100).toFixed(1)}%)` : "";
    const better = cfg.eval.direction === "maximize" ? delta > 0 : delta < 0;
    foot.textContent = `${delta > 0 ? "+" : ""}${fmtMetric(delta)}${pct} vs baseline`;
    foot.className = "stat-foot" + (better ? " good" : "");
  } else {
    foot.innerHTML = "&nbsp;";
    foot.className = "stat-foot";
  }
  $("stat-iters").textContent = attempts.length;
  const durations = attempts.map((h) => h.duration_seconds).filter((x) => x != null);
  const avg = durations.length ? durations.reduce((a, b) => a + b, 0) / durations.length : null;
  const spend = totalCost(attempts);
  $("stat-champions").textContent =
    `${attempts.filter((h) => h.is_champion).length} improvements` +
    (avg ? ` · ~${fmtDuration(avg)}/iter` : "") +
    (spend ? ` · ${spend}` : "");
  const env = envLabel(cfg);
  $("stat-env").textContent = env.name;
  $("stat-env-foot").textContent = env.kind;

  $("chart-title").textContent = `${cfg.eval.metric} per iteration`;
  $("th-metric").textContent = cfg.eval.metric;

  renderChart(hist, cfg);
  renderTable(attempts, cfg);
  renderKnowledge(d.knowledge || []);
}

// -- knowledge library -----------------------------------------------------

function renderKnowledge(docs) {
  const list = $("knowledge-list");
  if (!docs.length) {
    list.innerHTML = `<div class="knowledge-empty">No documents yet. Add papers, notes or
      specs — the agent gets an index and reads them on demand
      (<code>knowledge/INDEX.md</code> in its workspace).</div>`;
    return;
  }
  list.innerHTML = "";
  for (const doc of docs) {
    const item = document.createElement("div");
    item.className = "k-item";
    const kb = (doc.size / 1024).toFixed(0);
    const fmtChars = (c) => (c >= 1000 ? `${(c / 1000).toFixed(0)}k chars` : `${c} chars`);
    const extra = doc.extracted
      ? `extracted to text (${fmtChars(doc.chars || 0)})`
      : doc.note || (doc.chars != null ? fmtChars(doc.chars) : "");
    item.innerHTML = `
      <span class="k-kind ${doc.kind}">${doc.kind === "pdf" ? "PDF" : "TXT"}</span>
      <span class="k-name">${esc(doc.name)}</span>
      <span class="k-meta">${kb} KB${extra ? " · " + esc(extra) : ""}</span>
      <button class="k-del" title="Remove">✕</button>`;
    item.querySelector(".k-del").onclick = async () => {
      if (!confirm(`Remove ${doc.name} from the knowledge library?`)) return;
      try {
        await api(`/api/experiments/${state.currentId}/knowledge/${encodeURIComponent(doc.name)}`,
                  { method: "DELETE" });
        refreshDetail();
      } catch (e) { toast(e.message); }
    };
    list.appendChild(item);
  }
}

async function uploadKnowledgeFiles(files) {
  let ok = 0;
  for (const file of files) {
    const form = new FormData();
    form.append("file", file);
    const res = await fetch(`/api/experiments/${state.currentId}/knowledge`,
                            { method: "POST", body: form });
    if (res.ok) { ok++; }
    else {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch (_) { /* noop */ }
      toast(`${file.name}: ${detail}`, 5000);
    }
  }
  if (ok) toast(`Added ${ok} document${ok > 1 ? "s" : ""} — available from the next iteration`);
  refreshDetail();
}

// -- live activity ----------------------------------------------------------

function resetLive() {
  state.live.activity = [];
  state.live.phase = null;
  state.live.iteration = null;
  state.live.iterStarted = null;
}

function startLiveTimers() {
  if (!state.live.clockTimer) {
    state.live.clockTimer = setInterval(() => {
      if (state.live.iterStarted) {
        $("activity-clock").textContent = fmtClock((Date.now() - state.live.iterStarted) / 1000);
      }
    }, 1000);
  }
  if (!state.live.pollTimer) {
    // safety net in case an SSE event is missed
    state.live.pollTimer = setInterval(() => refreshDetail().catch(() => {}), 15000);
  }
}

function stopLiveTimers() {
  clearInterval(state.live.clockTimer);
  clearInterval(state.live.pollTimer);
  state.live.clockTimer = state.live.pollTimer = null;
}

function renderActivity() {
  const { phase, iteration, activity } = state.live;
  // stepper
  const order = state.live.phaseNames;
  const activeIdx = order.indexOf(phase);
  for (const el of document.querySelectorAll("#stepper .step")) {
    const idx = order.indexOf(el.dataset.step);
    el.className = "step" + (idx === activeIdx ? " active" : idx < activeIdx ? " done" : "");
  }
  $("activity-iter").textContent =
    phase === "baseline" || phase === "env"
      ? (phase === "env" ? "preparing environment" : "scoring baseline")
      : iteration ? `iteration ${iteration}` : "starting…";
  // log tail
  const log = $("activity-log");
  const lines = activity.slice(-40);
  log.innerHTML = lines.map((l, i) =>
    `<span class="cl${i < lines.length - 1 ? " dim" : ""}">${esc(l)}</span>`
  ).join("") + `<span class="cl"><span class="cursor"></span></span>`;
  log.scrollTop = log.scrollHeight;
}

function pushActivity(line) {
  const a = state.live.activity;
  if (a.length && a[a.length - 1] === line) return;
  a.push(line);
  if (a.length > 200) a.splice(0, a.length - 200);
  renderActivity();
}

// -- table ------------------------------------------------------------------

function renderTable(attempts, cfg) {
  const tbody = $("iter-tbody");
  tbody.innerHTML = "";
  for (const h of [...attempts].reverse()) {
    const tr = document.createElement("tr");
    let badge;
    if (!h.eval_ok) badge = `<span class="badge badge-failed">eval failed</span>`;
    else if (h.is_champion) badge = `<span class="badge badge-champion">★ champion</span>`;
    else badge = `<span class="badge badge-worse">no improvement</span>`;
    if (h.violations) badge += ` <span class="badge badge-violation">${h.violations} blocked edit${h.violations > 1 ? "s" : ""}</span>`;

    tr.innerHTML = `
      <td class="num">${h.iteration}</td>
      <td class="num">${fmtMetric(h.primary)}${h.holdout?.ok ? `<div class="holdout-sub" title="holdout">${fmtMetric(h.holdout.primary)}</div>` : ""}</td>
      <td class="num">${deltaVsChampionBefore(h, cfg)}</td>
      <td>${badge}</td>
      <td class="num">${fmtDuration(h.duration_seconds)}</td>
      <td class="num">${fmtCost(h.usage) || "–"}</td>
      <td class="summary-cell"><div class="clamp">${esc(h.summary || h.agent_error || h.eval_error || "")}</div></td>
      <td class="row-open">›</td>`;
    tr.onclick = () => openDrawer(h.iteration);
    tbody.appendChild(tr);
  }
}

function deltaVsChampionBefore(h, cfg) {
  if (h.primary == null) return "–";
  let champ = null;
  for (const e of state.detail.history) {
    if (e.iteration >= h.iteration) break;
    if (e.is_champion && e.primary != null) champ = e.primary;
  }
  if (champ == null) return "–";
  const delta = h.primary - champ;
  const better = cfg.eval.direction === "maximize" ? delta > 0 : delta < 0;
  const cls = better ? "delta-good" : "delta-bad";
  return `<span class="${cls}">${delta > 0 ? "+" : ""}${fmtMetric(delta)}</span>`;
}

// ---------------------------------------------------------------- chart

const chartState = { points: [], geometry: null, metric: "" };

function renderChart(history, cfg) {
  const svg = $("chart");
  const wrap = $("chart-wrap");
  renderChart._champLabelY = null;
  const width = wrap.clientWidth || 800;
  const height = 290;
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.innerHTML = "";

  const css = getComputedStyle(document.documentElement);
  const C = {
    attempt: css.getPropertyValue("--series-attempt").trim(),
    champ: css.getPropertyValue("--series-champ").trim(),
    holdout: css.getPropertyValue("--series-holdout").trim(),
    grid: css.getPropertyValue("--grid").trim(),
    baseline: css.getPropertyValue("--baseline").trim(),
    muted: css.getPropertyValue("--muted").trim(),
    surface: css.getPropertyValue("--surface-1").trim(),
  };

  const valued = history.filter((h) => h.primary != null);
  const holdoutPts = history
    .filter((h) => h.holdout && h.holdout.ok && h.holdout.primary != null)
    .map((h) => ({ i: h.iteration, v: h.holdout.primary }));
  $("legend-holdout").classList.toggle("hidden", !holdoutPts.length);
  const M = { top: 18, right: 84, bottom: 30, left: 58 };
  const iw = width - M.left - M.right;
  const ih = height - M.top - M.bottom;

  const ns = "http://www.w3.org/2000/svg";
  const mk = (tag, attrs, parent = svg) => {
    const el = document.createElementNS(ns, tag);
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
    parent.appendChild(el);
    return el;
  };

  if (!valued.length) {
    const t = mk("text", { x: width / 2, y: height / 2, "text-anchor": "middle",
                           fill: C.muted, "font-size": 13 });
    t.textContent = history.length ? "No successful evaluation yet" : "Start the loop to see the metric trend";
    chartState.geometry = null;
    return;
  }

  const maxIter = Math.max(1, ...history.map((h) => h.iteration));
  const xOf = (i) => M.left + (i / maxIter) * iw;

  const domainValues = [...valued.map((h) => h.primary), ...holdoutPts.map((p) => p.v)];
  let lo = Math.min(...domainValues);
  let hi = Math.max(...domainValues);
  if (lo === hi) { lo -= Math.abs(lo) * 0.1 || 1; hi += Math.abs(hi) * 0.1 || 1; }
  const pad = (hi - lo) * 0.08;
  lo -= pad; hi += pad;
  const yOf = (v) => M.top + ih - ((v - lo) / (hi - lo)) * ih;

  const ticks = 5;
  for (let i = 0; i <= ticks; i++) {
    const v = lo + ((hi - lo) * i) / ticks;
    const y = yOf(v);
    mk("line", { x1: M.left, x2: width - M.right, y1: y, y2: y,
                 stroke: C.grid, "stroke-width": 1 });
    const label = mk("text", { x: M.left - 8, y: y + 4, "text-anchor": "end",
                               fill: C.muted, "font-size": 10.5 });
    label.textContent = fmtMetric(v);
  }
  mk("line", { x1: M.left, x2: width - M.right, y1: M.top + ih, y2: M.top + ih,
               stroke: C.baseline, "stroke-width": 1 });
  const xStep = Math.max(1, Math.ceil(maxIter / 12));
  for (let i = 0; i <= maxIter; i += xStep) {
    const label = mk("text", { x: xOf(i), y: M.top + ih + 18, "text-anchor": "middle",
                               fill: C.muted, "font-size": 10.5 });
    label.textContent = i;
  }
  const xTitle = mk("text", { x: M.left + iw / 2, y: height - 2, "text-anchor": "middle",
                              fill: C.muted, "font-size": 10.5 });
  xTitle.textContent = "iteration";

  // champion trajectory: step line of best-so-far
  const champPts = [];
  let best = null;
  for (let i = 0; i <= maxIter; i++) {
    const h = history.find((e) => e.iteration === i);
    if (h && h.is_champion && h.primary != null) best = h.primary;
    if (best != null) champPts.push({ i, v: best });
  }
  if (champPts.length) {
    let dAttr = "";
    champPts.forEach((p, idx) => {
      const x = xOf(p.i), y = yOf(p.v);
      if (idx === 0) dAttr += `M ${x} ${y}`;
      else {
        const prev = champPts[idx - 1];
        dAttr += ` L ${x} ${yOf(prev.v)} L ${x} ${y}`;
      }
    });
    mk("path", { d: dAttr, fill: "none", stroke: C.champ, "stroke-width": 2 });
    // direct label at the end (relief rule for the sub-3:1 light aqua);
    // two lines so it fits inside the reserved right margin
    const last = champPts[champPts.length - 1];
    const lx = xOf(last.i) + 8;
    const ly = Math.min(Math.max(yOf(last.v), M.top + 14), M.top + ih - 6);
    const lbl1 = mk("text", { x: lx, y: ly - 6, fill: C.champ,
                              "font-size": 10.5, "font-weight": 600 });
    lbl1.textContent = "champion";
    const lbl2 = mk("text", { x: lx, y: ly + 8, fill: C.champ,
                              "font-size": 10.5, "font-weight": 600 });
    lbl2.textContent = fmtMetric(last.v);
    renderChart._champLabelY = ly;
  }

  // holdout trajectory: dashed step line, square markers (champions only)
  if (holdoutPts.length) {
    let dAttr = "";
    holdoutPts.forEach((p, idx) => {
      const x = xOf(p.i), y = yOf(p.v);
      if (idx === 0) dAttr += `M ${x} ${y}`;
      else dAttr += ` L ${x} ${yOf(holdoutPts[idx - 1].v)} L ${x} ${y}`;
    });
    const lastH = holdoutPts[holdoutPts.length - 1];
    if (lastH.i < maxIter) dAttr += ` L ${xOf(maxIter)} ${yOf(lastH.v)}`;
    mk("path", { d: dAttr, fill: "none", stroke: C.holdout, "stroke-width": 1.8,
                 "stroke-dasharray": "5 4" });
    for (const p of holdoutPts) {
      mk("rect", { x: xOf(p.i) - 3.5, y: yOf(p.v) - 3.5, width: 7, height: 7,
                   rx: 1.5, fill: C.holdout, stroke: C.surface, "stroke-width": 1.5 });
    }
    // direct label (relief rule for sub-3:1 light yellow), dodging the champion label
    const lx = xOf(maxIter) + 8;
    let ly = Math.min(Math.max(yOf(lastH.v), M.top + 14), M.top + ih - 6);
    const champY = renderChart._champLabelY;
    if (champY != null && Math.abs(ly - champY) < 30) {
      let candidate = champY + 30;                 // prefer below the champion label
      if (candidate > M.top + ih - 6) candidate = champY - 30;  // flip at the bottom edge
      ly = Math.min(Math.max(candidate, M.top + 14), M.top + ih - 6);
    }
    const hl1 = mk("text", { x: lx, y: ly - 6, fill: C.holdout,
                             "font-size": 10.5, "font-weight": 600 });
    hl1.textContent = "holdout";
    const hl2 = mk("text", { x: lx, y: ly + 8, fill: C.holdout,
                             "font-size": 10.5, "font-weight": 600 });
    hl2.textContent = fmtMetric(lastH.v);
  }

  // attempts line + markers
  const pts = valued.map((h) => ({ h, x: xOf(h.iteration), y: yOf(h.primary) }));
  if (pts.length > 1) {
    const dAttr = pts.map((p, i) => `${i === 0 ? "M" : "L"} ${p.x} ${p.y}`).join(" ");
    mk("path", { d: dAttr, fill: "none", stroke: C.attempt, "stroke-width": 2,
                 "stroke-linejoin": "round", opacity: 0.85 });
  }
  for (const p of pts) {
    mk("circle", { cx: p.x, cy: p.y, r: p.h.is_champion ? 5 : 4,
                   fill: p.h.is_champion ? C.champ : C.attempt,
                   stroke: C.surface, "stroke-width": 2 });
  }
  // failed evals: hollow markers pinned to the axis
  const failed = history.filter((h) => h.iteration > 0 && h.primary == null);
  for (const h of failed) {
    mk("circle", { cx: xOf(h.iteration), cy: M.top + ih, r: 4, fill: "none",
                   stroke: C.muted, "stroke-width": 1.5 });
  }

  chartState.points = [...pts, ...failed.map((h) => ({ h, x: xOf(h.iteration), y: M.top + ih }))]
    .sort((a, b) => a.x - b.x);
  chartState.geometry = { M, iw, ih, width, height };
  chartState.metric = cfg.eval.metric;
  attachChartHover(svg, C);
}

function attachChartHover(svg, C) {
  const wrap = $("chart-wrap");
  const tip = $("chart-tooltip");
  const ns = "http://www.w3.org/2000/svg";
  let cross = null;

  svg.onmousemove = (ev) => {
    if (!chartState.geometry || !chartState.points.length) return;
    const rect = svg.getBoundingClientRect();
    const scaleX = chartState.geometry.width / rect.width;
    const mx = (ev.clientX - rect.left) * scaleX;
    let nearest = chartState.points[0];
    for (const p of chartState.points) {
      if (Math.abs(p.x - mx) < Math.abs(nearest.x - mx)) nearest = p;
    }
    if (!cross) {
      cross = document.createElementNS(ns, "line");
      cross.setAttribute("stroke", C.baseline);
      cross.setAttribute("stroke-dasharray", "3 3");
      svg.appendChild(cross);
    }
    const g = chartState.geometry;
    cross.setAttribute("x1", nearest.x); cross.setAttribute("x2", nearest.x);
    cross.setAttribute("y1", g.M.top); cross.setAttribute("y2", g.M.top + g.ih);

    const h = nearest.h;
    tip.innerHTML = `
      <div class="tt-title">Iteration ${h.iteration}${h.is_champion ? " · ★ champion" : ""}</div>
      <div class="tt-row">${esc(chartState.metric)}: <b>${fmtMetric(h.primary)}</b>${h.eval_ok === false ? " (eval failed)" : ""}</div>
      ${h.holdout?.ok ? `<div class="tt-row">holdout: <b>${fmtMetric(h.holdout.primary)}</b></div>` : ""}
      ${h.usage ? `<div class="tt-row">cost: ${esc(fmtCost(h.usage))}</div>` : ""}
      ${h.summary ? `<div class="tt-row">${esc(h.summary.slice(0, 140))}${h.summary.length > 140 ? "…" : ""}</div>` : ""}`;
    tip.classList.remove("hidden");
    const wrapRect = wrap.getBoundingClientRect();
    let left = (nearest.x / scaleX) + 14;
    if (left + tip.offsetWidth > wrapRect.width - 8) left = (nearest.x / scaleX) - tip.offsetWidth - 14;
    tip.style.left = `${Math.max(0, left)}px`;
    tip.style.top = `12px`;
  };
  svg.onmouseleave = () => {
    tip.classList.add("hidden");
    if (cross) { cross.remove(); cross = null; }
  };
}

// ---------------------------------------------------------------- drawer

const drawerTabs = ["diff", "summary", "log", "meta"];

async function openDrawer(n) {
  const id = state.currentId;
  const data = await api(`/api/experiments/${id}/iterations/${n}`);
  $("drawer-title").textContent = `Iteration ${n}` + (data.meta.is_champion ? " · ★ champion" : "");
  $("drawer-download").href = `/api/experiments/${id}/iterations/${n}/download`;
  $("pane-diff").innerHTML = colorDiff(data.diff || "(no changes to editable files)");
  $("pane-summary").textContent = data.summary || "(no summary)";
  $("pane-log").textContent = data.agent_log || "(empty)";
  $("pane-meta").textContent = JSON.stringify(data.meta, null, 2);
  selectTab("diff");
  $("drawer").classList.remove("hidden");
}

function colorDiff(diff) {
  return diff.split("\n").map((line) => {
    const e = esc(line);
    if (line.startsWith("+++") || line.startsWith("---")) return `<span class="file">${e}</span>`;
    if (line.startsWith("@@")) return `<span class="hunk">${e}</span>`;
    if (line.startsWith("+")) return `<span class="add">${e}</span>`;
    if (line.startsWith("-")) return `<span class="del">${e}</span>`;
    return e;
  }).join("\n");
}

function selectTab(name) {
  for (const t of document.querySelectorAll(".tab")) {
    t.classList.toggle("active", t.dataset.tab === name);
  }
  for (const p of drawerTabs) {
    $(`pane-${p}`).classList.toggle("hidden", p !== name);
  }
}

// ---------------------------------------------------------------- SSE

function closeSSE() {
  if (state.sse) { state.sse.close(); state.sse = null; }
}

function connectSSE(id) {
  closeSSE();
  const src = new EventSource(`/api/experiments/${id}/events`);
  state.sse = src;
  src.onmessage = (ev) => {
    let event;
    try { event = JSON.parse(ev.data); } catch (_) { return; }
    if (state.currentId !== id) return;
    const p = event.payload || {};
    switch (event.kind) {
      case "iteration":
        if (p.iteration > 0) {
          toast(p.is_champion
            ? `★ Iteration ${p.iteration}: new champion (${fmtMetric(p.primary)})`
            : `Iteration ${p.iteration}: ${p.eval_ok ? fmtMetric(p.primary) : "eval failed"}`);
          pushActivity(p.is_champion
            ? `★ new champion — ${fmtMetric(p.primary)}`
            : `iteration ${p.iteration} scored ${p.eval_ok ? fmtMetric(p.primary) : "eval failed"}`);
        }
        state.live.phase = "decide";
        refreshDetail().then(refreshSidebar);
        break;
      case "status":
      case "instructions_updated":
      case "knowledge_updated":
      case "notebook_updated":
        refreshDetail().then(refreshSidebar);
        break;
      case "error":
        toast(`Loop error: ${p.message}`, 6000);
        refreshDetail().then(refreshSidebar);
        break;
      case "iteration_started":
        state.live.iteration = p.iteration;
        state.live.iterStarted = Date.now();
        state.live.activity = [];
        state.live.phase = "sync";
        pushActivity(`— iteration ${p.iteration} —`);
        setDocTitle(`▶ iter ${p.iteration}`);
        renderActivity();
        break;
      case "phase":
        state.live.phase = p.phase;
        if (p.phase === "env") pushActivity("resolving python environment…");
        if (p.phase === "baseline") { state.live.iterStarted = Date.now(); pushActivity("scoring the untouched baseline…"); }
        if (p.phase === "agent") pushActivity("agent is thinking…");
        if (p.phase === "eval") pushActivity("running evaluation…");
        renderActivity();
        break;
      case "environment":
        pushActivity(`env ready: ${p.name} · Python ${p.python_version || "?"} · ${p.package_count} packages`);
        break;
      case "agent_activity":
        state.live.iteration = p.iteration;
        pushActivity(p.line);
        break;
      default:
        break;
    }
  };
}

// ---------------------------------------------------------------- wiring

function bind() {
  $("btn-new").onclick = openSetup;
  $("btn-new-2").onclick = openSetup;
  $("btn-cancel-setup").onclick = () => {
    if (state.experiments.length) openExperiment(state.experiments[0].id);
    else showView("empty");
  };
  $("btn-load-tree").onclick = loadTree;
  $("f-tree-search").addEventListener("input", applyTreeFilter);
  $("btn-select-visible").onclick = () => setVisibleChecked(true);
  $("btn-clear-visible").onclick = () => setVisibleChecked(false);
  $("file-tree").addEventListener("change", () => updateTreeCount());
  $("btn-upload-knowledge").onclick = () => $("knowledge-file").click();
  $("knowledge-file").onchange = (ev) => {
    if (ev.target.files.length) uploadKnowledgeFiles([...ev.target.files]);
    ev.target.value = "";
  };
  $("f-agent-type").onchange = renderAgentExtra;
  $("f-model-select").onchange = () =>
    $("model-custom-field").classList.toggle("hidden", $("f-model-select").value !== "custom");
  $("btn-create").onclick = createExperiment;
  $("btn-check-env").onclick = checkEnv;
  $("btn-dice").onclick = () => { $("f-name").value = randomName(); };

  // folder picker
  $("btn-browse-ws").onclick = () => openBrowser("f-workspace", loadTree);
  $("btn-browse-venv").onclick = () => openBrowser("f-venv-path");
  $("browse-close").onclick = closeBrowser;
  $("browse-backdrop").onclick = closeBrowser;
  $("browse-go").onclick = () => browseTo($("browse-path").value.trim() || "~");
  $("browse-path").addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") browseTo($("browse-path").value.trim() || "~");
  });
  $("browse-select").onclick = () => {
    if (browse.targetId && browse.path) {
      $(browse.targetId).value = browse.path;
      closeBrowser();
      if (browse.onSelect) browse.onSelect();
    }
  };

  attachNumericGuards();
  for (const b of document.querySelectorAll("#env-seg .seg-btn")) {
    b.onclick = () => {
      document.querySelectorAll("#env-seg .seg-btn").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      renderEnvFields();
    };
  }
  $("btn-load-default").onclick = async () => {
    if (!state.templates) state.templates = await api("/api/templates");
    $("f-instructions").value = state.templates.default;
  };
  $("btn-load-template").onclick = async () => {
    if (!state.templates) state.templates = await api("/api/templates");
    $("f-instructions").value = state.templates.template;
  };

  $("btn-start").onclick = async () => {
    try {
      await api(`/api/experiments/${state.currentId}/start`, { method: "POST" });
      resetLive();
      toast("Loop started — you can close this tab; it keeps running");
      refreshDetail();
    } catch (e) { toast(e.message); }
  };
  $("btn-stop").onclick = async () => {
    try {
      await api(`/api/experiments/${state.currentId}/stop`, { method: "POST" });
      pushActivity("stop requested — finishing the current iteration…");
      toast("Stopping after the current iteration…");
    } catch (e) { toast(e.message); }
  };
  $("btn-delete").onclick = async () => {
    if (!confirm("Delete this experiment and all of its results?")) return;
    try {
      await api(`/api/experiments/${state.currentId}`, { method: "DELETE" });
      state.currentId = null;
      closeSSE();
      stopLiveTimers();
      await refreshSidebar();
      showView("empty");
    } catch (e) { toast(e.message); }
  };
  $("btn-dl-champion").onclick = () => {
    window.location.href = `/api/experiments/${state.currentId}/champion/download`;
  };

  // drawer
  $("drawer-close").onclick = () => $("drawer").classList.add("hidden");
  $("drawer-backdrop").onclick = () => $("drawer").classList.add("hidden");
  for (const t of document.querySelectorAll(".tab")) {
    t.onclick = () => selectTab(t.dataset.tab);
  }

  // notebook modal
  $("btn-view-notebook").onclick = () => {
    $("notebook-text").value = state.detail?.notebook ||
      "(empty — the agent writes to AGENT_NOTES.md during iterations)";
    $("notebook-modal").classList.remove("hidden");
  };
  $("notebook-close").onclick = () => $("notebook-modal").classList.add("hidden");
  $("notebook-backdrop").onclick = () => $("notebook-modal").classList.add("hidden");
  $("notebook-save").onclick = async () => {
    try {
      await api(`/api/experiments/${state.currentId}/notebook`, {
        method: "PUT",
        body: JSON.stringify({ notebook: $("notebook-text").value }),
      });
      toast("Notebook saved — the agent sees it next iteration");
      $("notebook-modal").classList.add("hidden");
      refreshDetail();
    } catch (e) { toast(e.message); }
  };

  // instructions modal
  $("btn-edit-instructions").onclick = () => {
    $("instr-text").value = state.detail?.instructions || "";
    $("instr-modal").classList.remove("hidden");
  };
  $("instr-close").onclick = () => $("instr-modal").classList.add("hidden");
  $("instr-backdrop").onclick = () => $("instr-modal").classList.add("hidden");
  $("instr-default").onclick = async () => {
    if (!state.templates) state.templates = await api("/api/templates");
    $("instr-text").value = state.templates.default;
  };
  $("instr-template").onclick = async () => {
    if (!state.templates) state.templates = await api("/api/templates");
    $("instr-text").value = state.templates.template;
  };
  $("instr-save").onclick = async () => {
    try {
      await api(`/api/experiments/${state.currentId}/instructions`, {
        method: "PUT",
        body: JSON.stringify({ instructions: $("instr-text").value }),
      });
      toast("Instructions saved — applies from the next iteration");
      $("instr-modal").classList.add("hidden");
      refreshDetail();
    } catch (e) { toast(e.message); }
  };

  window.addEventListener("resize", () => {
    if (state.detail && !$("view-dash").classList.contains("hidden")) {
      renderChart(state.detail.history || [], state.detail.config);
    }
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") {
      $("drawer").classList.add("hidden");
      $("instr-modal").classList.add("hidden");
      closeBrowser();
    }
  });
}

async function init() {
  bind();
  await refreshSidebar();
  if (state.experiments.length) openExperiment(state.experiments[0].id);
  else showView("empty");
}

init();
