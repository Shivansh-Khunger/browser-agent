// PROTOTYPE — shared helpers: aggregation, stats, formatting, variant switcher, state panel.
// Layout lives in each variant file; only data + chrome is shared here.

(function () {
  const { cases, K } = window.PROTO;

  // ---------- formatting ----------
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const usd = (x) => "$" + (x < 0.01 ? x.toFixed(4) : x.toFixed(2));
  const pct = (x, d = 0) => (x * 100).toFixed(d) + "%";
  const pp = (x) => (x >= 0 ? "+" : "−") + Math.abs(x * 100).toFixed(1) + " pp";

  // ---------- per-case aggregation ----------
  function passes(trials) {
    // one Trial per k: take the last attempt; infra_error attempts never count
    const byK = {};
    trials.forEach((t) => { byK[t.header.trial_id.split(".").at(-2)] = t; });
    return Object.values(byK).filter((t) => t.pass).length;
  }

  function flatTrials(c) { return [...c.trials.baseline, ...c.trials.candidate]; }

  cases.forEach((c) => {
    c.analyzed = !c.dropped;
    c.b = c.analyzed ? passes(c.trials.baseline) : null;
    c.c = c.analyzed ? passes(c.trials.candidate) : null;
    c.delta = c.analyzed ? (c.c - c.b) / K : null;
    c.flag = !c.analyzed ? null : c.c - c.b >= 4 ? "fixed" : c.b - c.c >= 4 ? "broken" : null;
    const all = flatTrials(c).filter((t) => t.header.status === "completed");
    c.attributions = {};
    all.filter((t) => !t.pass && t.attribution).forEach((t) => {
      c.attributions[t.attribution.label] = (c.attributions[t.attribution.label] ?? 0) + 1;
    });
    c.visionFallback = all.some((t) => t.header.vision_disabled_at_step != null);
    c.unscripted = all.some((t) => t.header.unscripted_fallback);
    c.cost = { baseline: sum(c.trials.baseline.map((t) => t.header.totals.cost_usd)), candidate: sum(c.trials.candidate.map((t) => t.header.totals.cost_usd)) };
    c.steps = { baseline: mean(c.trials.baseline.filter(ok).map((t) => t.header.totals.steps)), candidate: mean(c.trials.candidate.filter(ok).map((t) => t.header.totals.steps)) };
  });

  function ok(t) { return t.header.status === "completed"; }
  function sum(a) { return a.reduce((s, x) => s + x, 0); }
  function mean(a) { return a.length ? sum(a) / a.length : 0; }

  // ---------- paired stats: sign-flip permutation + bootstrap ----------
  function mulberry32(a) { return function () { a |= 0; a = (a + 0x6d2b79f5) | 0; let t = Math.imul(a ^ (a >>> 15), 1 | a); t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; }; }

  function analyse(kind) {
    const cs = cases.filter((c) => c.kind === kind);
    const an = cs.filter((c) => c.analyzed);
    const d = an.map((c) => c.delta);
    const obs = mean(d);
    const r = mulberry32(7);
    let extreme = 0; const P = 5000;
    for (let i = 0; i < P; i++) { const m = mean(d.map((x) => (r() < 0.5 ? -x : x))); if (Math.abs(m) >= Math.abs(obs) - 1e-12) extreme++; }
    const p = (extreme + 1) / (P + 1);
    const boots = [];
    for (let i = 0; i < 2000; i++) boots.push(mean(d.map(() => d[Math.floor(r() * d.length)])));
    boots.sort((a, b) => a - b);
    const sd = Math.sqrt(mean(d.map((x) => (x - obs) ** 2)));
    const mde = 2.8 * sd / Math.sqrt(d.length);
    const broken = an.filter((c) => c.flag === "broken");
    let verdict = p < 0.05 ? (obs > 0 ? "better" : "worse") : "no detectable difference";
    if (verdict === "better" && broken.length) verdict = "better, with regressions";
    const pass1 = { baseline: mean(an.map((c) => c.b / K)), candidate: mean(an.map((c) => c.c / K)) };
    const pass5 = { baseline: an.filter((c) => c.b === K).length / an.length, candidate: an.filter((c) => c.c === K).length / an.length };
    const dropped = cs.filter((c) => c.dropped);
    return {
      kind, n: an.length, total: cs.length, delta: obs, p, ci: [boots[50], boots[1949]], mde, verdict, pass1, pass5,
      fixed: an.filter((c) => c.flag === "fixed"), broken, dropped,
      unreliable: dropped.length / cs.length > 0.1,
    };
  }

  const stats = { step: analyse("step"), task: analyse("task") };

  // ---------- flags shown above the verdict ----------
  const allTrials = cases.flatMap(flatTrials);
  const flags = {
    brokenP0: cases.filter((c) => c.flag === "broken" && c.priority === "p0"),
    unscripted: allTrials.filter((t) => t.header.unscripted_fallback),
    vision: allTrials.filter((t) => t.header.vision_disabled_at_step != null),
    infraAttempts: allTrials.filter((t) => t.header.status === "infra_error"),
    dropped: cases.filter((c) => c.dropped),
    unattributed: allTrials.filter((t) => t.attribution?.label === "unattributed"),
  };
  const spent = sum(allTrials.map((t) => t.header.totals.cost_usd));

  function byOrigin() {
    const out = {};
    for (const c of cases.filter((x) => x.analyzed)) {
      const o = (out[c.origin] ??= { origin: c.origin, n: 0, b: 0, c: 0 });
      o.n++; o.b += c.b / K; o.c += c.c / K;
    }
    return Object.values(out).map((o) => ({ ...o, b: o.b / o.n, c: o.c / o.n }));
  }

  function findTrial(id) { return allTrials.find((t) => t.header.trial_id === id); }
  function caseOf(id) { return cases.find((c) => c.case_id === id); }

  // one-line event summary, used by every variant's trace renderer
  function eventSummary(e) {
    switch (e.type) {
      case "observation": return `${e.view ?? "?"} · ${e.url} · ${e.dom_refs} dom_refs${e.screenshot_ref ? " · 📷" : ""}`;
      case "model_call": return `${e.role} · ${e.tokens.prompt.toLocaleString()} in / ${e.tokens.completion} out · ctx ${e.context_pct}% · ${usd(e.cost_usd)} · ${e.latency_ms} ms${e.retries ? ` · ${e.retries} retries` : ""}`;
      case "tool_call": return `${e.tool}(${JSON.stringify(e.args)})${e.target_eval_id ? ` → ${e.target_eval_id}` : ""}${e.hit_node != null && e.hit_node !== e.resolved_node ? " ⚠ hit-test mismatch" : ""}`;
      case "action_result": return (e.ok ? "ok · " : `✗ ${e.error_code} · `) + e.text;
      case "user_exchange": return `${e.kind}: “${e.question}” → “${e.reply}” (${e.source}${e.rule_id ? ": " + e.rule_id : ""})${e.counts_as_intervention ? " · counts as intervention" : ""}`;
      case "intervention": return `${e.source} · ${e.trigger}${e.action_refused ? " · action refused" : ""} — “${e.injected_preview}”`;
      case "compaction": return `replaced ${e.replaced_messages} msgs · ${e.tokens_before.toLocaleString()} → ${e.tokens_after.toLocaleString()} tokens — “${e.summary_preview}”`;
      case "vision_fallback": return `${e.reason}${e.vision_disabled ? " · vision disabled for rest of Trial" : ""}`;
      case "blocked_navigation": return `${e.url} (allowed: ${e.allowed_domains.join(", ")})`;
      case "fixture_end_state": return `${e.family}: ${JSON.stringify(e.state)}`;
      case "replay_input": return `${e.decision} · ${e.history_turns} frozen turns · rebuilt: ${e.rebuilt.join(", ")}`;
      default: return JSON.stringify(e);
    }
  }

  // ---------- open-coding notes (in memory only) ----------
  const notes = {}; // trial_id -> { text, codes: [] }
  function note(id) { return (notes[id] ??= { text: "", codes: [] }); }
  function allCodes() {
    const n = {};
    Object.values(notes).forEach((x) => x.codes.forEach((c) => (n[c] = (n[c] ?? 0) + 1)));
    return Object.entries(n).sort((a, b) => b[1] - a[1]);
  }

  // ---------- state panel (rule: surface the state) ----------
  const uiState = {};
  function setState(patch) {
    Object.assign(uiState, patch);
    const el = document.getElementById("proto-state-body");
    if (el) el.textContent = JSON.stringify({ ...uiState, notes: Object.fromEntries(Object.entries(notes).filter(([, v]) => v.text || v.codes.length)) }, null, 2);
  }

  // ---------- variant switcher ----------
  function mountSwitcher(variants, render) {
    const bar = document.createElement("div");
    bar.id = "proto-switcher";
    bar.innerHTML = `<button data-d="-1" title="previous (←)">‹</button><span id="proto-label"></span><button data-d="1" title="next (→)">›</button><button id="proto-state-toggle" title="show UI state">state</button><button id="proto-theme" title="theme: auto follows the OS"></button>`;
    document.body.appendChild(bar);
    const panel = document.createElement("pre");
    panel.id = "proto-state";
    panel.innerHTML = `<b>PROTOTYPE UI state</b>\n<span id="proto-state-body"></span>`;
    panel.hidden = true;
    document.body.appendChild(panel);
    const current = () => new URLSearchParams(location.search).get("variant") ?? variants[0].key;
    function go(key) {
      const u = new URL(location.href); u.searchParams.set("variant", key); history.replaceState(null, "", u);
      const v = variants.find((x) => x.key === key) ?? variants[0];
      document.getElementById("proto-label").textContent = `${v.key} (${v.name})`;
      for (const k of Object.keys(uiState)) if (k !== "theme") delete uiState[k];
      setState({ variant: v.key });
      render(v);
    }
    function cycle(d) { const i = variants.findIndex((x) => x.key === current()); go(variants[(i + d + variants.length) % variants.length].key); }
    // theme: auto (OS) -> light -> dark; per-viewer convenience only
    const themes = ["auto", "light", "dark"];
    let theme = "auto";
    try { theme = localStorage.getItem("proto-theme") ?? "auto"; } catch {}
    function applyTheme() {
      if (theme === "auto") delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = theme;
      document.getElementById("proto-theme").textContent = "theme: " + theme;
      try { localStorage.setItem("proto-theme", theme); } catch {}
      setState({ theme });
    }
    applyTheme();
    bar.addEventListener("click", (e) => {
      const d = e.target.dataset.d; if (d) cycle(+d);
      if (e.target.id === "proto-theme") { theme = themes[(themes.indexOf(theme) + 1) % themes.length]; applyTheme(); }
      if (e.target.id === "proto-state-toggle") panel.hidden = !panel.hidden;
    });
    document.addEventListener("keydown", (e) => {
      if (e.target.closest("input, textarea, [contenteditable]")) return;
      if (e.key === "ArrowLeft") cycle(-1);
      if (e.key === "ArrowRight") cycle(1);
    });
    go(current());
  }

  window.LIB = { esc, usd, pct, pp, stats, flags, spent, byOrigin, findTrial, caseOf, eventSummary, note, allCodes, setState, mountSwitcher, allTrials, flatTrials };
})();
