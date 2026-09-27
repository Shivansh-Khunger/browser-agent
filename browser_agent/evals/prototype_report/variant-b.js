// PROTOTYPE — Variant B: "Triage workbench".
// Three panes, full viewport. Left: a queue of failed Trials (not cases). Middle: the failed
// Trace step-aligned against its paired Trial (same case, same seed, other Variant). Right:
// open-coding panel. Primary affordance: code one failure after another with j/k.

(function () {
  const { esc, usd, pct, pp, stats, flags, spent, eventSummary, note, allCodes, setState, findTrial, caseOf } = window.LIB;
  const { comparison: C, cases, K } = window.PROTO;

  let sel = null;
  let q = { variant: "both", label: "any", uncoded: false };

  function queue() {
    const out = [];
    for (const c of cases.filter((x) => x.analyzed)) {
      for (const v of ["baseline", "candidate"]) {
        if (q.variant !== "both" && q.variant !== v) continue;
        for (const t of c.trials[v]) {
          if (t.header.status !== "completed" || t.pass) continue;
          if (q.label !== "any" && t.attribution?.label !== q.label) continue;
          const n = note(t.header.trial_id);
          if (q.uncoded && (n.text || n.codes.length)) continue;
          out.push({ c, t });
        }
      }
    }
    const rank = (c) => (c.flag === "broken" ? 0 : c.flag === "fixed" ? 1 : c.priority === "p0" ? 2 : 3);
    out.sort((a, b) => rank(a.c) - rank(b.c) || a.c.case_id.localeCompare(b.c.case_id));
    return out;
  }

  function pairedOf(t) {
    const [cid, v, k] = t.header.trial_id.split(".");
    const other = v === "baseline" ? "candidate" : "baseline";
    const c = caseOf(cid);
    const same = c.trials[other].filter((x) => x.header.trial_id.split(".")[2] === k);
    return same.at(-1);
  }

  function topbar() {
    const vs = (s) => `<span class="b-v ${s.verdict.startsWith("better") ? "good" : s.verdict === "worse" ? "bad" : "neutral"}">${s.kind}: <b>${esc(s.verdict)}</b> ${pp(s.delta)} <span class="muted">[${pp(s.ci[0])}, ${pp(s.ci[1])}] p=${s.p.toFixed(3)}</span></span>`;
    return `<div class="b-top">
      <span><b>${C.comparison_id}</b> <span class="muted">${C.variants.baseline.config_hash} → ${C.variants.candidate.config_hash} · ${usd(spent)}/${usd(C.budget_usd)}</span></span>
      ${vs(stats.task)} ${vs(stats.step)}
      ${flags.brokenP0.map((c) => `<span class="chip broken">broken P0 ${c.case_id}</span>`).join("")}
      ${flags.unscripted.length ? `<span class="chip warn">?unscripted ×${flags.unscripted.length}</span>` : ""}
      ${flags.vision.length ? `<span class="chip warn">↓vision ×${flags.vision.length}</span>` : ""}
      <span class="chip">dropped ${flags.dropped.length}/${cases.length}</span>
      <span class="chip">fidelity: no change</span>
    </div>`;
  }

  function left(list) {
    let lastCase = null;
    const coded = list.filter(({ t }) => { const n = note(t.header.trial_id); return n.text || n.codes.length; }).length;
    const items = list.map(({ c, t }) => {
      const head = c.case_id !== lastCase ? `<div class="b-qcase">${c.flag ? `<span class="chip ${c.flag}">${c.flag}</span> ` : ""}<b>${c.case_id}</b> <span class="muted">${c.b}/${K}→${c.c}/${K} · ${c.kind} ${c.priority}</span></div>` : "";
      lastCase = c.case_id;
      const n = note(t.header.trial_id);
      return head + `<div class="b-qitem ${sel === t.header.trial_id ? "sel" : ""}" data-trial="${t.header.trial_id}">
        <span class="b-var ${t.header.variant}">${t.header.variant[0]}</span> ${t.header.trial_id.split(".").slice(2).join(".")}
        ${t.attribution ? `<span class="chip attr-${t.attribution.label}">${t.attribution.label}</span>` : ""}
        ${n.codes.map((x) => `<span class="chip code">${esc(x)}</span>`).join("")}${n.text ? " ✎" : ""}</div>`;
    }).join("");
    return `<div class="b-left">
      <div class="b-filters">
        <select data-q="variant">${["both", "baseline", "candidate"].map((x) => `<option ${q.variant === x ? "selected" : ""}>${x}</option>`).join("")}</select>
        <select data-q="label">${["any", "model_decision", "perception_gap", "browser_fault", "unattributed"].map((x) => `<option ${q.label === x ? "selected" : ""}>${x}</option>`).join("")}</select>
        <label><input type="checkbox" data-q="uncoded" ${q.uncoded ? "checked" : ""}> uncoded</label>
      </div>
      <div class="muted small">${list.length} failed Trials · ${coded} coded · j/k to move</div>
      <div class="b-queue">${items}</div></div>`;
  }

  function stepsOf(t) {
    const by = {};
    (t?.events ?? []).forEach((e) => (by[e.step] ??= []).push(e));
    return by;
  }

  function cell(evs, otherEvs) {
    if (!evs) return `<div class="b-cell empty">—</div>`;
    const call = evs.find((e) => e.type === "tool_call");
    const oCall = otherEvs?.find((e) => e.type === "tool_call");
    const differs = call && oCall && JSON.stringify([call.tool, call.args]) !== JSON.stringify([oCall.tool, oCall.args]);
    return `<div class="b-cell ${differs ? "differs" : ""}">${evs.map((e) => {
      if (e.type === "observation") return `<details class="b-obs"><summary>👁 #${e.seq} ${esc(e.view)} <span class="muted">${esc(e.title)}</span></summary><pre>${esc(e.rendered)}</pre></details>`;
      if (e.type === "replay_input") return `<details class="b-obs" open><summary>⏮ #${e.seq} replay: ${esc(e.decision)}</summary><pre>${esc(e.rendered_observation)}</pre></details>`;
      if (e.type === "model_call") return `<div class="b-mc muted small" title="${esc(e.raw_tool_call ?? "")}">🧠 #${e.seq} ${e.role} · ctx ${e.context_pct}% · ${usd(e.cost_usd)}</div>`;
      if (e.type === "tool_call") return `<div class="b-call">▶ #${e.seq} <b>${e.tool}</b> <code>${esc(JSON.stringify(e.args))}</code>${e.target_eval_id ? ` <span class="muted">→ ${e.target_eval_id}</span>` : ""}</div>`;
      if (e.type === "action_result") return `<div class="b-res ${e.ok ? "" : "err"}">↩ #${e.seq} ${esc(e.text)}</div>`;
      return `<div class="b-ev ev-${e.type}">◆ #${e.seq} <b>${e.type}</b> ${esc(eventSummary(e))}</div>`;
    }).join("")}</div>`;
  }

  function middle(t) {
    if (!t) return `<div class="b-mid"><p class="muted">Queue empty.</p></div>`;
    const p = pairedOf(t);
    const a = stepsOf(t), b = stepsOf(p);
    const nums = [...new Set([...Object.keys(a), ...Object.keys(b)].map(Number))].sort((x, y) => x - y);
    const c = caseOf(t.header.case_id);
    const hdr = (x, label) => x ? `<div class="b-colh ${x.pass ? "pass" : "fail"}">${label}: <b>${x.header.trial_id}</b> · ${x.pass ? "PASS" : "FAIL"} · ${x.header.terminal_reason ?? x.header.status} · ${x.header.totals.steps} steps · ${usd(x.header.totals.cost_usd)}<div class="muted small">done: ${esc(x.header.done_answer ?? "—")}</div></div>` : `<div class="b-colh">no paired Trial</div>`;
    return `<div class="b-mid">
      <div class="b-task"><b>${c.case_id}</b> — ${esc(c.task ?? c.decision ?? "")} <span class="muted">${c.memory && Object.keys(c.memory).length ? "memory: " + esc(JSON.stringify(c.memory)) : ""}</span></div>
      <div class="b-grid"><div class="b-stepn"></div>${hdr(t, "this")}${hdr(p, "paired (same seed)")}
      ${nums.map((n) => `<div class="b-stepn">${n}</div>${cell(a[n], b[n])}${cell(b[n], a[n])}`).join("")}</div></div>`;
  }

  function right(t) {
    if (!t) return `<div class="b-right"></div>`;
    const n = note(t.header.trial_id);
    const codes = allCodes();
    return `<div class="b-right">
      <h3>Grades</h3>${t.grades.map((g) => `<div><span class="chip ${g.verdict === "pass" ? "fixed" : "broken"}">${g.verdict}</span> ${g.grader} <div class="muted small">${esc(g.detail)}</div></div>`).join("")}
      <h3>Failure attribution</h3>${t.attribution ? `<span class="chip attr-${t.attribution.label}">${t.attribution.label}</span>${t.attribution.requirements.map((r) => `<div class="small">${r.view} → <b>${r.status}</b>${r.failing_props.length ? ` · props ${r.failing_props.join(",")}` : ""}<br><span class="muted">needs ${r.needs.join(", ")}${r.obs_seqs.length ? " · seen at #" + r.obs_seqs.join(", #") : ""}</span></div>`).join("")}${t.attribution.failing_detail ? `<div class="muted small">${esc(t.attribution.failing_detail)}</div>` : ""}${t.attribution.hint ? `<div class="muted small">${esc(t.attribution.hint)}</div>` : ""}` : '<span class="muted">—</span>'}
      <h3>Open coding</h3>
      <textarea data-note="${t.header.trial_id}" placeholder="First thing that went wrong, in plain words…">${esc(n.text)}</textarea>
      <div>${n.codes.map((x) => `<span class="chip code" data-rm="${esc(x)}" title="remove">${esc(x)} ×</span>`).join(" ")}</div>
      <input data-code placeholder="add a code, Enter" />
      ${codes.length ? `<div class="muted small">codes so far:</div><div>${codes.map(([x, k]) => `<span class="chip code" data-add="${esc(x)}">${esc(x)} ${k}</span>`).join(" ")}</div>` : ""}
      <h3>Trial</h3><pre class="small">${esc(JSON.stringify({ seed: t.header.seed, variant: `${t.header.variant} ${t.header.code_revision}/${t.header.config_hash}`, automation_outcome: t.header.automation_outcome, unscripted_fallback: t.header.unscripted_fallback, vision_disabled_at_step: t.header.vision_disabled_at_step, totals: t.header.totals }, null, 1))}</pre>
    </div>`;
  }

  function render(root) {
    const list = queue();
    if (!sel || !list.find((x) => x.t.header.trial_id === sel)) sel = list[0]?.t.header.trial_id ?? null;
    const t = sel && findTrial(sel);
    root.innerHTML = `<div class="b-page">${topbar()}<div class="b-body">${left(list)}${middle(t)}${right(t)}</div></div>`;
    root.querySelector(".b-qitem.sel")?.scrollIntoView({ block: "nearest" });
    setState({ queue: q, selected: sel, queueLength: list.length });
  }

  window.VARIANT_B = {
    key: "B", name: "Triage workbench",
    render(root) {
      sel = null; q = { variant: "both", label: "any", uncoded: false };
      render(root);
      root.onclick = (e) => {
        const it = e.target.closest("[data-trial]");
        if (it) { sel = it.dataset.trial; return render(root); }
        const add = e.target.closest("[data-add]");
        if (add && sel) { const n = note(sel); if (!n.codes.includes(add.dataset.add)) n.codes.push(add.dataset.add); return render(root); }
        const rm = e.target.closest("[data-rm]");
        if (rm && sel) { const n = note(sel); n.codes = n.codes.filter((x) => x !== rm.dataset.rm); return render(root); }
      };
      root.onchange = (e) => {
        const k = e.target.dataset.q;
        if (k) { q[k] = e.target.type === "checkbox" ? e.target.checked : e.target.value; render(root); }
      };
      root.oninput = (e) => { if (e.target.dataset.note) { note(e.target.dataset.note).text = e.target.value; setState({}); } };
      root.onkeydown = (e) => {
        if (e.target.dataset.code !== undefined && e.key === "Enter" && e.target.value.trim()) {
          const n = note(sel); const v = e.target.value.trim().toLowerCase().replace(/\s+/g, "-");
          if (!n.codes.includes(v)) n.codes.push(v);
          render(root); root.querySelector("[data-code]")?.focus();
        }
      };
      document.onkeydown = (e) => {
        if (!document.querySelector(".b-page") || e.target.closest("input, textarea, select")) return;
        if (e.key !== "j" && e.key !== "k") return;
        const list = queue(); const i = list.findIndex((x) => x.t.header.trial_id === sel);
        const j = Math.max(0, Math.min(list.length - 1, i + (e.key === "j" ? 1 : -1)));
        sel = list[j]?.t.header.trial_id; render(root);
      };
    },
  };
})();
