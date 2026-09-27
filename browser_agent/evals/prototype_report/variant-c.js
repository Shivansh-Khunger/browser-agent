// PROTOTYPE — Variant C: "Matrix + drawer".
// Every case is one row of Trial cells (5 baseline | 5 candidate) so flip patterns show at a
// glance; the fidelity diff is a view × property grid. Clicking a cell opens the Trace in a
// bottom drawer as a dense one-row-per-step table. Primary affordance: spotting patterns.

(function () {
  const { esc, usd, pct, pp, stats, flags, spent, eventSummary, note, setState, findTrial, caseOf } = window.LIB;
  const { comparison: C, cases, fidelity, K } = window.PROTO;

  let sel = null;
  let sort = "delta";
  let expanded = new Set();

  const LETTER = { model_decision: "M", perception_gap: "P", browser_fault: "B", unattributed: "U" };

  function cellFor(trials, k) {
    const ts = trials.filter((t) => t.header.trial_id.split(".")[2] === "t" + k);
    const t = ts.at(-1);
    if (!t) return `<td class="c-cell none"></td>`;
    const infra = ts.filter((x) => x.header.status === "infra_error").length;
    const cls = t.header.status !== "completed" ? "infra" : t.pass ? "pass" : "fail";
    const mark = t.header.status !== "completed" ? "×" + infra : !t.pass ? (LETTER[t.attribution?.label] ?? "") : "";
    const extra = (t.header.unscripted_fallback ? "?" : "") + (t.header.vision_disabled_at_step != null ? "↓" : "");
    return `<td class="c-cell ${cls} ${sel === t.header.trial_id ? "sel" : ""}" data-trial="${t.header.trial_id}" title="${t.header.trial_id}">${mark}${extra}</td>`;
  }

  function rows(kind) {
    let list = cases.filter((c) => c.kind === kind);
    if (sort === "delta") list.sort((a, b) => (a.analyzed ? 0 : 1) - (b.analyzed ? 0 : 1) || (b.delta ?? 0) - (a.delta ?? 0));
    if (sort === "baseline") list.sort((a, b) => (a.b ?? 9) - (b.b ?? 9));
    if (sort === "origin") list.sort((a, b) => a.origin.localeCompare(b.origin));
    return list.map((c) => {
      const bar = c.analyzed ? `<div class="c-bar"><div class="${c.delta >= 0 ? "up" : "down"}" style="width:${Math.abs(c.delta) * 50}%;${c.delta >= 0 ? "left:50%" : "right:50%"}"></div></div>` : "";
      return `<tr class="${c.analyzed ? "" : "dropped"}">
        <td class="c-id" title="${esc(c.task ?? c.decision ?? "")}">${c.priority === "p0" ? '<b class="p0">P0</b> ' : ""}${c.case_id}</td>
        <td class="c-orig">${{ harvested: "H", hand_written: "W", synthetic: "S" }[c.origin]}</td>
        ${c.analyzed || c.trials.baseline.length ? [0, 1, 2, 3, 4].map((k) => cellFor(c.trials.baseline, k)).join("") + '<td class="c-gap"></td>' + [0, 1, 2, 3, 4].map((k) => cellFor(c.trials.candidate, k)).join("") : `<td colspan="11" class="muted small">dropped: ${esc(c.dropped.reason)}</td>`}
        <td class="c-num">${c.analyzed ? `${c.b}→${c.c}` : "—"}</td>
        <td class="c-bartd">${bar}</td>
        <td>${c.flag ? `<span class="chip ${c.flag}">${c.flag}</span>` : c.dropped ? `<span class="chip">dropped</span>` : ""}</td></tr>`;
    }).join("");
  }

  function kindBlock(kind) {
    const s = stats[kind];
    return `<div class="c-kind">
      <div class="c-khead"><b>${kind} evals</b> <span class="c-v ${s.verdict.startsWith("better") ? "good" : s.verdict === "worse" ? "bad" : "neutral"}">${esc(s.verdict)}</span>
        ${pp(s.delta)} <span class="muted">CI [${pp(s.ci[0])}, ${pp(s.ci[1])}] · p=${s.p.toFixed(3)} · MDE ${pp(s.mde).slice(1)} · pass^1 ${pct(s.pass1.baseline)}→${pct(s.pass1.candidate)} · pass^5 ${pct(s.pass5.baseline)}→${pct(s.pass5.candidate)}</span></div>
      <table class="c-matrix"><thead><tr><th>case</th><th title="origin">o</th><th colspan="5">baseline t0–t4</th><th></th><th colspan="5">candidate t0–t4</th><th>c/${K}</th><th>Δ</th><th></th></tr></thead><tbody>${rows(kind)}</tbody></table></div>`;
  }

  function fidelityGrid() {
    const props = [1, 2, 3, 4, 5, 6, 7, 8, 9];
    return `<div class="c-fid"><div class="c-fhead"><b>Fidelity diff</b> <span class="muted small">${fidelity.version} · ${fidelity.identical_across_variants ? "identical across Variants" : "differs"}</span></div>
      <table><thead><tr><th>view</th>${props.map((p) => `<th title="${p <= 5 || p === 8 ? "browser layer" : "per Variant"}">${p}</th>`).join("")}</tr></thead><tbody>
      ${fidelity.views.map((v) => `<tr class="${v.unstable ? "unstable" : ""}" title="${esc(v.unstable_detail ?? "")}"><td>${v.view}${v.unstable ? " ⚠" : ""}</td>${props.map((p) => {
        const g = v.preexisting.find((x) => x.property === p);
        return `<td class="${g ? "gap" : "okc"}" title="${g ? esc(g.eval_id + ": " + g.detail) : ""}">${g ? "●" : ""}</td>`;
      }).join("")}</tr>`).join("")}</tbody></table>
      <div class="muted small">● pre-existing gap (both Variants) · ⚠ unstable view · lost/gained would show ▼/▲</div></div>`;
  }

  function legend() {
    return `<div class="c-legend small"><span class="c-cell pass sw"></span>pass <span class="c-cell fail sw"></span>fail (M model_decision · P perception_gap · B browser_fault · U unattributed) <span class="c-cell infra sw"></span>infra ×attempts · ? unscripted · ↓ vision lost · origin H/W/S = harvested/hand-written/synthetic</div>`;
  }

  function drawer(t) {
    if (!t) return "";
    const h = t.header;
    const by = {};
    t.events.forEach((e) => (by[e.step] ??= []).push(e));
    const c = caseOf(h.case_id);
    const [cid, v, k] = h.trial_id.split(".");
    const other = c.trials[v === "baseline" ? "candidate" : "baseline"].filter((x) => x.header.trial_id.split(".")[2] === k).at(-1);
    const rowsHtml = Object.entries(by).map(([n, evs]) => {
      const obs = evs.find((e) => e.type === "observation" || e.type === "replay_input");
      const mc = evs.filter((e) => e.type === "model_call");
      const call = evs.find((e) => e.type === "tool_call");
      const res = evs.find((e) => e.type === "action_result");
      const others = evs.filter((e) => !["observation", "replay_input", "model_call", "tool_call", "action_result"].includes(e.type));
      const key = h.trial_id + ":" + n;
      const open = expanded.has(key);
      return `<tr class="c-srow ${res && !res.ok ? "err" : ""}" data-exp="${key}">
          <td>${n}</td>
          <td>${obs ? esc(obs.view ?? obs.decision) : ""}</td>
          <td>${call ? `<b>${call.tool}</b> <code>${esc(JSON.stringify(call.args)).slice(0, 90)}</code>` : ""}</td>
          <td>${res ? esc((res.ok ? "" : "✗ " + res.error_code + " ") + res.text).slice(0, 80) : ""}</td>
          <td>${others.map((e) => `<span class="chip ev-${e.type}" title="${esc(eventSummary(e))}">${e.type}</span>`).join(" ")}</td>
          <td class="c-num">${mc.map((m) => m.tokens.prompt.toLocaleString()).join("+")}</td>
          <td class="c-num">${mc.length ? Math.max(...mc.map((m) => m.context_pct)) + "%" : ""}</td>
          <td class="c-num">${mc.length ? usd(mc.reduce((s, m) => s + m.cost_usd, 0)) : ""}</td></tr>
        ${open ? `<tr class="c-sexp"><td></td><td colspan="7">${evs.map((e) => `<div><span class="muted">#${e.seq} ${e.type} +${e.t.mono_ms}ms</span> ${e.type === "observation" ? `<pre>${esc(e.rendered)}</pre>` : e.type === "replay_input" ? `<pre>${esc(e.rendered_observation)}</pre>` : e.type === "model_call" ? `<pre>${esc(JSON.stringify(e, null, 1))}</pre>` : esc(eventSummary(e))}</div>`).join("")}</td></tr>` : ""}`;
    }).join("");
    return `<div class="c-drawer">
      <div class="c-dhead">
        <b>${h.trial_id}</b> <span class="chip ${t.pass ? "fixed" : "broken"}">${t.pass ? "pass" : h.status === "completed" ? "fail" : h.status}</span>
        ${h.terminal_reason ?? ""} · ${h.totals.steps} steps · ${usd(h.totals.cost_usd)} · peak ctx ${h.totals.peak_context_pct}% · seed ${h.seed}
        ${t.attribution ? `<span class="chip attr-${t.attribution.label}">${t.attribution.label}</span> <span class="small">${t.attribution.requirements.map((r) => `${r.view}: ${r.status}${r.failing_props.length ? " (props " + r.failing_props.join(",") + ")" : ""}${r.obs_seqs.length ? " @#" + r.obs_seqs.join(",#") : ""}`).join("; ")}</span>` : ""}
        ${other ? `<button data-trial="${other.header.trial_id}">⇄ paired ${other.header.variant} ${other.pass ? "✓" : "✗"}</button>` : ""}
        <button data-close>close ✕</button>
        <div class="small">${t.grades.map((g) => `<span class="${g.verdict === "pass" ? "good-t" : "bad-t"}">${g.grader}:${g.verdict}</span> <span class="muted">${esc(g.detail)}</span>`).join(" · ")}</div>
        <div class="small muted">task: ${esc(c.task ?? c.decision)} ${h.done_answer ? ` · done: “${esc(h.done_answer)}”` : ""}${h.infra ? ` · infra: ${h.infra.class} ${esc(h.infra.message)}` : ""}</div>
        <input class="c-note" data-note="${h.trial_id}" placeholder="open code…" value="${esc(note(h.trial_id).text)}" />
      </div>
      <div class="c-dbody"><table class="c-steps"><thead><tr><th>#</th><th>view</th><th>call</th><th>result</th><th>events</th><th>prompt tok</th><th>ctx</th><th>$</th></tr></thead><tbody>${rowsHtml}</tbody></table></div>
    </div>`;
  }

  function render(root) {
    const t = sel && findTrial(sel);
    root.innerHTML = `<div class="c-page ${t ? "with-drawer" : ""}">
      <div class="c-top">
        <div><b>${C.comparison_id}</b> <span class="muted">${C.variants.baseline.summary} → ${C.variants.candidate.summary} · ${usd(spent)} / ${usd(C.budget_usd)}</span></div>
        <div class="c-flags">${flags.brokenP0.map((c) => `<span class="chip broken">broken P0: ${c.case_id}</span>`).join(" ")} ${flags.unscripted.length ? `<span class="chip warn">?unscripted ×${flags.unscripted.length}</span>` : ""} ${flags.vision.length ? `<span class="chip warn">↓vision ×${flags.vision.length}</span>` : ""} <span class="chip">dropped ${flags.dropped.length}/${cases.length}</span> <span class="chip">unattributed ×${flags.unattributed.length}</span></div>
        <div>sort: ${["delta", "baseline", "origin"].map((s) => `<button class="${sort === s ? "on" : ""}" data-sort="${s}">${s}</button>`).join("")}</div>
      </div>
      <div class="c-main"><div class="c-left">${legend()}${kindBlock("task")}${kindBlock("step")}</div>${fidelityGrid()}</div>
      ${drawer(t)}
    </div>`;
    setState({ sort, selected: sel, expandedSteps: [...expanded] });
  }

  window.VARIANT_C = {
    key: "C", name: "Matrix + drawer",
    render(root) {
      sel = null; sort = "delta"; expanded = new Set();
      render(root);
      root.onclick = (e) => {
        const s = e.target.closest("[data-sort]"); if (s) { sort = s.dataset.sort; return render(root); }
        if (e.target.closest("[data-close]")) { sel = null; return render(root); }
        const tr = e.target.closest("[data-trial]"); if (tr) { sel = tr.dataset.trial; return render(root); }
        const x = e.target.closest("[data-exp]");
        if (x && !e.target.closest("input")) { expanded.has(x.dataset.exp) ? expanded.delete(x.dataset.exp) : expanded.add(x.dataset.exp); render(root); }
      };
      root.oninput = (e) => { if (e.target.dataset.note) { note(e.target.dataset.note).text = e.target.value; setState({}); } };
    },
  };
})();
