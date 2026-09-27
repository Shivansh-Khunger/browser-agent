// PROTOTYPE — Variant A: "Report document".
// One long scrolling page, read top to bottom like a printed report. The trace opens
// inline under its case row as a vertical step timeline. Primary affordance: reading.

(function () {
  const { esc, usd, pct, pp, stats, flags, spent, byOrigin, eventSummary, note, setState } = window.LIB;
  const { comparison: C, cases, fidelity, K } = window.PROTO;

  let open = { case: null, trial: null };
  let filter = "flagged";
  let view = "table"; // D only: "table" | "matrix"
  let withMatrix = false;
  const LETTER = { model_decision: "M", perception_gap: "P", browser_fault: "B", unattributed: "U" };

  function verdictCard(s) {
    const cls = s.verdict.startsWith("better") ? "good" : s.verdict === "worse" ? "bad" : "neutral";
    return `<div class="a-verdict ${cls}">
      <div class="a-kind">${s.kind} evals · ${s.n} cases × ${K} Trials${s.dropped.length ? ` · ${s.dropped.length} dropped` : ""}</div>
      <div class="a-vword">${esc(s.verdict)}</div>
      <div>Δ pass^1 <b>${pp(s.delta)}</b> · 95% CI [${pp(s.ci[0])}, ${pp(s.ci[1])}] · p = ${s.p.toFixed(3)} · detectable ≈ ${pp(s.mde).slice(1)}</div>
      <div class="muted">pass^1 ${pct(s.pass1.baseline)} → ${pct(s.pass1.candidate)} · pass^5 ${pct(s.pass5.baseline)} → ${pct(s.pass5.candidate)} (secondary)</div>
      <div class="muted">${s.fixed.length} fixed · ${s.broken.length} broken${s.unreliable ? ' · <b class="bad-t">UNRELIABLE: >10% dropped</b>' : ""}</div>
    </div>`;
  }

  function flagList() {
    const items = [];
    flags.brokenP0.forEach((c) => items.push(`<li class="bad-t"><b>Broken P0:</b> <a href="#" data-case="${c.case_id}">${c.case_id}</a> (${c.b}/${K} → ${c.c}/${K})</li>`));
    if (flags.unscripted.length) items.push(`<li><b>?unscripted:</b> ${flags.unscripted.length} Trials hit a Simulated-user fallback (${[...new Set(flags.unscripted.map((t) => t.header.case_id))].join(", ")})</li>`);
    if (flags.vision.length) items.push(`<li><b>↓vision:</b> ${flags.vision.length} Trials lost vision mid-run (${[...new Set(flags.vision.map((t) => t.header.case_id))].join(", ")})</li>`);
    if (flags.dropped.length) items.push(`<li><b>Dropped from both Variants:</b> ${flags.dropped.length} of ${cases.length} cases (${pct(flags.dropped.length / cases.length, 1)}) — see Dropped section</li>`);
    if (flags.unattributed.length) items.push(`<li><b>Unattributed failures:</b> ${flags.unattributed.length} — add a requirement for an earlier view</li>`);
    return `<ul class="a-flags">${items.join("")}</ul>`;
  }

  function fidelitySection() {
    const rows = fidelity.views.map((v) => `<tr>
      <td>${v.view}${v.unstable ? ' <span class="chip warn" title="' + esc(v.unstable_detail) + '">unstable</span>' : ""}</td><td>${v.targets}</td>
      <td>${v.baseline.lost.length || "—"}</td><td>${v.candidate.lost.length || "—"} / ${v.candidate.gained.length || "—"}</td>
      <td>${v.preexisting.map((g) => `<div><code>${g.eval_id}</code> prop ${g.property}: ${esc(g.detail)} <span class="muted">(${g.cases.join(", ")})</span></div>`).join("") || "—"}</td></tr>`).join("");
    return `<h2>Fidelity diff <span class="muted small">${fidelity.version}</span></h2>
      <p class="muted">${esc(fidelity.note)}</p>
      <table class="a-table"><thead><tr><th>Fixture view</th><th>targets</th><th>baseline lost</th><th>candidate lost / gained</th><th>pre-existing gaps (baseline)</th></tr></thead><tbody>${rows}</tbody></table>`;
  }

  function attrChips(c) {
    return Object.entries(c.attributions).map(([k, n]) => `<span class="chip attr-${k}">${k} ×${n}</span>`).join(" ");
  }

  function filtered() {
    let list = cases.filter((c) => c.analyzed);
    if (filter === "flagged") list = list.filter((c) => c.flag || c.unscripted || c.visionFallback || c.priority === "p0");
    if (filter === "failing") list = list.filter((c) => c.b < K || c.c < K);
    if (["step", "task"].includes(filter)) list = list.filter((c) => c.kind === filter);
    return list.sort((a, b) => (b.flag === "broken") - (a.flag === "broken") || (b.flag === "fixed") - (a.flag === "fixed") || b.delta - a.delta);
  }

  // D: matrix rows — one cell per Trial, same inline detail as the table on click
  function matrixRows() {
    const cell = (trials, k) => {
      const ts = trials.filter((t) => t.header.trial_id.split(".")[2] === "t" + k);
      const t = ts.at(-1);
      if (!t) return `<td></td>`;
      const cls = t.header.status !== "completed" ? "infra" : t.pass ? "pass" : "fail";
      const mark = t.header.status !== "completed" ? "×" + ts.length : !t.pass ? (LETTER[t.attribution?.label] ?? "") : "";
      const extra = (t.header.unscripted_fallback ? "?" : "") + (t.header.vision_disabled_at_step != null ? "↓" : "");
      return `<td class="c-cell ${cls} ${open.trial === t.header.trial_id ? "sel" : ""}" data-trial="${t.header.trial_id}" title="${t.header.trial_id}${t.attribution ? " · " + t.attribution.label : ""}">${mark}${extra}</td>`;
    };
    return filtered().map((c) => {
      const bar = `<div class="c-bar"><div class="${c.delta >= 0 ? "up" : "down"}" style="width:${Math.abs(c.delta) * 50}%;${c.delta >= 0 ? "left:50%" : "right:50%"}"></div></div>`;
      const row = `<tr class="d-row ${open.case === c.case_id ? "open" : ""}">
        <td>${c.flag ? `<span class="chip ${c.flag}">${c.flag}</span>` : ""}</td>
        <td class="c-id a-case" data-case="${c.case_id}">${c.priority === "p0" ? '<b class="p0">P0</b> ' : ""}${c.case_id} <span class="muted small">${c.kind} · ${c.origin}</span></td>
        ${[0, 1, 2, 3, 4].map((k) => cell(c.trials.baseline, k)).join("")}<td class="c-gap"></td>${[0, 1, 2, 3, 4].map((k) => cell(c.trials.candidate, k)).join("")}
        <td class="num">${c.b}→${c.c}</td><td class="c-bartd">${bar}</td>
        <td class="num">${usd(c.cost.baseline)} / ${usd(c.cost.candidate)}</td></tr>`;
      return row + (open.case === c.case_id ? `<tr><td colspan="16">${caseDetail(c)}</td></tr>` : "");
    }).join("");
  }

  function perCase() {
    const toggle = withMatrix ? `<span class="d-toggle">${["table", "matrix"].map((v) => `<button class="${view === v ? "on" : ""}" data-view="${v}">${v}</button>`).join("")}</span>` : "";
    const filters = `<div class="a-filters">${["flagged", "failing", "task", "step", "all"].map((f) => `<button class="${filter === f ? "on" : ""}" data-filter="${f}">${f}</button>`).join("")}${toggle}</div>`;
    if (withMatrix && view === "matrix") {
      return filters + `<div class="c-legend small"><span class="c-cell pass sw"></span>pass <span class="c-cell fail sw"></span>fail (M model_decision · P perception_gap · B browser_fault · U unattributed) <span class="c-cell infra sw"></span>infra ×attempts · ? unscripted · ↓ vision lost · click a cell to read its Trace</div>
        <table class="c-matrix d-matrix"><thead><tr><th></th><th>case</th><th colspan="5">baseline t0–t4</th><th></th><th colspan="5">candidate t0–t4</th><th>c/${K}</th><th>Δ</th><th>cost b / c</th></tr></thead><tbody>${matrixRows()}</tbody></table>`;
    }
    return filters + `<table class="a-table"><thead><tr><th></th><th>case</th><th>baseline → candidate</th><th>attribution / flags</th><th>cost b / c</th><th>steps b / c</th></tr></thead><tbody>${caseRows()}</tbody></table>`;
  }

  function caseRows() {
    return filtered().map((c) => {
      const row = `<tr class="a-case ${open.case === c.case_id ? "open" : ""}" data-case="${c.case_id}">
        <td>${c.flag ? `<span class="chip ${c.flag}">${c.flag}</span>` : ""}</td>
        <td><b>${c.case_id}</b> <span class="muted">${c.kind} · ${c.priority} · ${c.origin}</span></td>
        <td class="num">${c.b}/${K} → ${c.c}/${K}</td>
        <td>${attrChips(c)} ${c.unscripted ? '<span class="chip warn">?unscripted</span>' : ""} ${c.visionFallback ? '<span class="chip warn">↓vision</span>' : ""}</td>
        <td class="num">${usd(c.cost.baseline)} / ${usd(c.cost.candidate)}</td>
        <td class="num">${c.steps.baseline.toFixed(1)} / ${c.steps.candidate.toFixed(1)}</td></tr>`;
      return row + (open.case === c.case_id ? `<tr><td colspan="6">${caseDetail(c)}</td></tr>` : "");
    }).join("");
  }

  function caseDetail(c) {
    const strip = (v) => c.trials[v].map((t) => `<button class="a-tr ${t.header.status !== "completed" ? "infra" : t.pass ? "pass" : "fail"} ${open.trial === t.header.trial_id ? "sel" : ""}" data-trial="${t.header.trial_id}">${t.header.trial_id.split(".").slice(-2).join(".")}</button>`).join("");
    const t = open.trial && [...c.trials.baseline, ...c.trials.candidate].find((x) => x.header.trial_id === open.trial);
    return `<div class="a-detail">
      <div class="muted">${esc(c.task ?? c.decision ?? "")}</div>
      <div class="a-strips"><div><span class="lbl">baseline</span>${strip("baseline")}</div><div><span class="lbl">candidate</span>${strip("candidate")}</div></div>
      ${t ? trace(t) : '<p class="muted">Pick a Trial to read its Trace.</p>'}
    </div>`;
  }

  function trace(t) {
    const h = t.header;
    const bySteps = {};
    t.events.forEach((e) => (bySteps[e.step] ??= []).push(e));
    const steps = Object.entries(bySteps).map(([n, evs]) => `<div class="a-step"><div class="a-stepn">step ${n}</div><div>${evs.map(eventBlock).join("")}</div></div>`).join("");
    const n = note(h.trial_id);
    return `<div class="a-trace">
      <div class="a-thead"><b>${h.trial_id}</b> · ${h.status}${h.terminal_reason ? " / " + h.terminal_reason : ""} · ${h.totals.steps} steps · ${usd(h.totals.cost_usd)} · peak ctx ${h.totals.peak_context_pct}% · ${h.totals.wall_s}s · seed ${h.seed}
        <div class="muted">${h.variant} (${h.code_revision}, ${h.config_hash}) · ${h.automation_outcome}${h.unscripted_fallback ? " · ?unscripted" : ""}${h.vision_disabled_at_step != null ? " · ↓vision at step " + h.vision_disabled_at_step : ""}${h.infra ? ` · infra: ${h.infra.class} — ${esc(h.infra.message)}` : ""}</div></div>
      <div class="a-grades">${t.grades.map((g) => `<span class="chip ${g.verdict === "pass" ? "fixed" : "broken"}">${g.grader}: ${g.verdict}</span> <span class="muted small">${esc(g.detail)}</span>`).join("<br>")}</div>
      ${t.attribution ? `<div class="a-attr"><span class="chip attr-${t.attribution.label}">${t.attribution.label}</span> ${t.attribution.requirements.map((r) => `${r.view} [${r.needs.join(", ")}]: <b>${r.status}</b>${r.failing_props.length ? " · props " + r.failing_props.join(",") : ""}${r.obs_seqs.length ? " · obs seq " + r.obs_seqs.join(", ") : ""}`).join("; ")}${t.attribution.failing_detail ? `<div class="muted">${esc(t.attribution.failing_detail)}</div>` : ""}${t.attribution.hint ? `<div class="muted">${esc(t.attribution.hint)}</div>` : ""}</div>` : ""}
      ${h.done_answer ? `<div class="a-answer">done: “${esc(h.done_answer)}”</div>` : ""}
      <div class="a-note"><textarea placeholder="Open-code this Trial: what went wrong, in your own words…" data-note="${h.trial_id}">${esc(n.text)}</textarea></div>
      ${steps}
    </div>`;
  }

  function eventBlock(e) {
    const body = e.type === "observation"
      ? `<details><summary>${esc(eventSummary(e))}</summary><pre>${esc(e.rendered)}</pre></details>`
      : e.type === "replay_input"
        ? `<details open><summary>${esc(eventSummary(e))}</summary><pre>${esc(e.rendered_observation)}</pre></details>`
        : e.type === "model_call"
          ? `<details><summary>${esc(eventSummary(e))}</summary><pre>${esc(JSON.stringify({ model_served: e.model_served, provider: e.provider, finish_reason: e.finish_reason, tokens: e.tokens, request_sha256: e.request_sha256, message_refs: e.message_refs, raw_tool_call: e.raw_tool_call }, null, 2))}</pre></details>`
          : esc(eventSummary(e));
    return `<div class="a-ev ev-${e.type}"><span class="a-evt">#${e.seq} ${e.type}</span> ${body}</div>`;
  }

  function originSplit() {
    return `<table class="a-table narrow"><thead><tr><th>origin</th><th>cases</th><th>baseline pass^1</th><th>candidate pass^1</th></tr></thead><tbody>${byOrigin().map((o) => `<tr><td>${o.origin}</td><td>${o.n}</td><td>${pct(o.b)}</td><td>${pct(o.c)}</td></tr>`).join("")}</tbody></table>`;
  }

  function dropped() {
    return `<table class="a-table"><thead><tr><th>case</th><th>stage</th><th>reason</th></tr></thead><tbody>${flags.dropped.map((c) => `<tr><td>${c.case_id} <span class="muted">${c.kind}</span></td><td>${c.dropped.stage}</td><td>${esc(c.dropped.reason)}</td></tr>`).join("")}</tbody></table>`;
  }

  function render(root) {
    root.innerHTML = `<div class="a-page">
      <h1>Comparison <code>${C.comparison_id}</code></h1>
      <div class="muted">${C.suite.name} @ ${C.suite.version} · ${C.status} · ${C.started_at} → ${C.ended_at} · spent ${usd(spent)} of ${usd(C.budget_usd)} budget · provider ${C.provider.name} (no fallbacks)</div>
      <div class="a-variants">
        <div><b>baseline</b> ${C.variants.baseline.code_revision} · ${C.variants.baseline.config_hash} — ${esc(C.variants.baseline.summary)}</div>
        <div><b>candidate</b> ${C.variants.candidate.code_revision} · ${C.variants.candidate.config_hash} — ${esc(C.variants.candidate.summary)}</div>
        <details><summary>config diff (${C.config_diff.length} key)</summary>${C.config_diff.map((d) => `<div><code>${d.key}</code><div class="del">− ${esc(d.baseline)}</div><div class="ins">+ ${esc(d.candidate)}</div></div>`).join("")}</details>
      </div>
      ${flagList()}
      <div class="a-verdicts">${verdictCard(stats.task)}${verdictCard(stats.step)}</div>
      ${fidelitySection()}
      <h2>Per-case results</h2>
      ${perCase()}
      <h2>By origin</h2>${originSplit()}
      <h2>Dropped cases</h2>${dropped()}
    </div>`;
    setState({ filter, ...(withMatrix ? { view } : {}), openCase: open.case, openTrial: open.trial });
  }

  function mount(root, matrix) {
      open = { case: null, trial: null }; filter = "flagged"; withMatrix = matrix; view = matrix ? "matrix" : "table";
      render(root);
      root.onclick = (e) => {
        const f = e.target.closest("[data-filter]");
        if (f) { filter = f.dataset.filter; return render(root); }
        const vw = e.target.closest("[data-view]");
        if (vw) { view = vw.dataset.view; return render(root); }
        const tr = e.target.closest("[data-trial]");
        if (tr) {
          const id = tr.dataset.trial;
          const inMatrix = tr.classList.contains("c-cell");
          if (inMatrix && open.trial === id) open = { case: null, trial: null };
          else { open.trial = open.trial === id && !inMatrix ? null : id; open.case = id.split(".")[0]; }
          return render(root);
        }
        const cs = e.target.closest("[data-case]");
        if (cs) {
          e.preventDefault();
          const id = cs.dataset.case;
          if (open.case === id && cs.tagName === "TR") { open = { case: null, trial: null }; }
          else { open.case = id; const c = cases.find((x) => x.case_id === id); const firstFail = [...c.trials.baseline, ...c.trials.candidate].find((t) => !t.pass); open.trial = firstFail?.header.trial_id ?? null; if (!c.analyzed) { filter = "all"; } }
          if (filter === "flagged" && !cases.find((x) => x.case_id === id).flag) filter = "all";
          render(root);
          document.querySelector(`tr[data-case="${id}"]`)?.scrollIntoView({ block: "start" });
        }
      };
      root.oninput = (e) => { if (e.target.dataset.note) { note(e.target.dataset.note).text = e.target.value; setState({}); } };
  }

  window.VARIANT_A = { key: "A", name: "Report document", render: (root) => mount(root, false) };
  // D = A with C's matrix as a toggleable view of the per-case section
  window.VARIANT_D = { key: "D", name: "Report + matrix view", render: (root) => mount(root, true) };
})();
