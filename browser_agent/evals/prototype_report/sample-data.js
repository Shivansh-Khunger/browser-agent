// PROTOTYPE — throwaway sample data for the trace viewer / Comparison report prototype.
// Shapes follow the resolved tickets: Trial record (#28), fidelity checks (#29),
// eval case format (#30), Comparison statistics (#26). Nothing here is a real run.
// Scenario: first Comparison — baseline = current prompts.SYSTEM ("Firefox", India
// defaults), candidate = corrected prompt. Same code revision, different config hash.

(function () {
  // ---------- deterministic PRNG ----------
  function mulberry32(a) {
    return function () {
      a |= 0; a = (a + 0x6d2b79f5) | 0;
      let t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }
  const rnd = mulberry32(20260927);
  const pick = (arr) => arr[Math.floor(rnd() * arr.length)];
  const blob = () => "sha256:" + Array.from({ length: 12 }, () => "0123456789abcdef"[Math.floor(rnd() * 16)]).join("");

  const MODEL = "qwen/qwen3.7-flash";
  const COMPARISON_ID = "cmp-20260927-firefox-prompt";
  const K = 5;

  const comparison = {
    comparison_id: COMPARISON_ID,
    status: "complete",
    started_at: "2026-09-27T09:02:11Z",
    ended_at: "2026-09-27T10:41:37Z",
    suite: { name: "v0-bootstrap", version: "2026.09.27-1", step_cases: 60, task_cases: 30 },
    trials_per_case: K,
    budget_usd: 20,
    expected_cost_usd: 3.5,
    worst_case_cost_usd: 17.8,
    variants: {
      baseline: {
        label: "baseline",
        code_revision: "0def60e",
        config_hash: "c9a1f04e",
        summary: "current prompts.SYSTEM",
      },
      candidate: {
        label: "candidate",
        code_revision: "0def60e",
        config_hash: "5be27d91",
        summary: "corrected prompts.SYSTEM (Chromium, no India defaults)",
      },
    },
    config_diff: [
      {
        key: "prompts.system",
        baseline: "…You control a Firefox browser… Assume the user is in India: prices in INR, phone numbers +91, dates DD/MM/YYYY…",
        candidate: "…You control a Chromium browser… Use the user's saved details and stated locale; ask only when a detail is missing…",
      },
    ],
    provider: { name: "DeepInfra", allow_fallbacks: false },
  };

  // ---------- trace builder ----------
  function Trace(startWall) {
    let seq = 0, mono = 0;
    const events = [];
    const t0 = Date.parse(startWall);
    return {
      events,
      push(step, type, body, dt) {
        mono += dt ?? Math.round(400 + rnd() * 2600);
        events.push({ seq: seq++, t: { mono_ms: mono, wall: new Date(t0 + mono).toISOString() }, step, type, ...body });
        return seq - 1;
      },
      get elapsed() { return mono; },
    };
  }

  function renderObs(url, title, outline, controls) {
    const lines = [`URL: ${url}`, `Title: ${title}`, "", "Page outline:"];
    outline.forEach((o) => lines.push(o));
    controls.forEach((c, i) => lines.push(`[${i}] ${c}`));
    return lines.join("\n");
  }

  function modelCall(tr, step, opts) {
    const prompt = opts.prompt ?? Math.round(2200 + step * 1400 + rnd() * 600);
    const completion = Math.round(40 + rnd() * 140);
    const reasoning = Math.round(rnd() * 400);
    const cost = (prompt * 0.03 + (completion + reasoning) * 0.13) / 1e6;
    return tr.push(step, "model_call", {
      role: opts.role ?? "agent",
      model_requested: MODEL,
      model_served: MODEL,
      provider: "DeepInfra",
      tokens: { prompt, completion, reasoning, cached: Math.round(prompt * 0.6) },
      context_pct: +(prompt / 262144 * 100).toFixed(1),
      cost_usd: +cost.toFixed(6),
      latency_ms: Math.round(700 + rnd() * 1900),
      finish_reason: opts.finish ?? "tool_calls",
      retries: opts.retries ?? 0,
      message_refs: Array.from({ length: 2 + step * 2 }, blob).slice(-4),
      tools_schema_ref: "sha256:7f3e0c11aa90",
      request_sha256: blob().slice(7),
      raw_tool_call: opts.raw,
    }, opts.dt);
  }

  // A step: observation -> model_call -> tool_call -> action_result
  function step(tr, n, o) {
    const obsSeq = tr.push(n, "observation", {
      view: o.view,
      url: o.url,
      title: o.title,
      rendered: renderObs(o.url, o.title, o.outline ?? [], o.controls),
      pre_render_ref: blob(),
      dom_refs: o.controls.length + (o.extraRefs ?? 0),
      screenshot_ref: o.screenshot ? blob() : null,
    }, 300);
    const raw = JSON.stringify({ name: o.tool, arguments: o.args });
    modelCall(tr, n, { raw, prompt: o.prompt });
    if (o.beforeTool) o.beforeTool(tr, n);
    tr.push(n, "tool_call", {
      tool: o.tool,
      args: o.args,
      target_index: o.target,
      target_eval_id: o.targetEvalId ?? null,
      capture_id: "cap_" + blob().slice(7, 15),
      record_content_id: blob(),
      resolved_node: o.target != null ? 400 + o.target : null,
      hit_node: o.target != null ? (o.hitMismatch ? 999 : 400 + o.target) : null,
    }, 50);
    tr.push(n, "action_result", {
      ok: !o.error,
      error_code: o.error ?? null,
      text: o.result ?? (o.error ? `Error: ${o.error}` : "OK"),
      structured_ref: blob(),
    }, Math.round(200 + rnd() * 900));
    return obsSeq;
  }

  function header(c, variant, k, attempt, extra) {
    const v = comparison.variants[variant];
    return {
      trial_id: `${c.case_id}.${variant}.t${k}.a${attempt}`,
      comparison_id: COMPARISON_ID,
      attempt,
      trace_schema_version: 1,
      worker_protocol_version: 1,
      kind: c.kind,
      case_id: c.case_id,
      suite_version: comparison.suite.version,
      case_content_hash: c.content_hash,
      variant: v.label,
      code_revision: v.code_revision,
      config_hash: v.config_hash,
      resolved_config_ref: blob(),
      seed: 1000 + k * 17 + c.case_id.length,
      fixture: c.fixture ?? null,
      initial_memory: c.memory ?? {},
      allowed_domains: c.fixture ? [`${c.fixture.family}.fixture.localhost`] : [],
      chrome_version: c.kind === "task" ? "140.0.7339.82" : null,
      os: "darwin 27.0",
      runner_version: "0.0.1-proto",
      status: "completed",
      terminal_reason: "done",
      done_answer: null,
      automation_outcome: "automated",
      vision_disabled_at_step: null,
      unscripted_fallback: false,
      ...extra,
    };
  }

  function finalize(trial, tr) {
    const calls = tr.events.filter((e) => e.type === "model_call");
    const h = trial.header;
    h.started_at = tr.events[0]?.t.wall ?? "2026-09-27T09:10:00Z";
    h.ended_at = tr.events.at(-1)?.t.wall ?? h.started_at;
    h.totals = {
      cost_usd: +calls.reduce((s, e) => s + e.cost_usd, 0).toFixed(6),
      tokens_agent: calls.filter((e) => e.role === "agent").reduce((s, e) => s + e.tokens.prompt + e.tokens.completion, 0),
      tokens_supervisor: calls.filter((e) => e.role === "supervisor").reduce((s, e) => s + e.tokens.prompt + e.tokens.completion, 0),
      steps: Math.max(0, ...tr.events.map((e) => e.step)),
      wall_s: Math.round(tr.elapsed / 1000),
      peak_context_pct: Math.max(0, ...calls.map((e) => e.context_pct)),
    };
    trial.events = tr.events;
    trial.pass = trial.header.status === "completed" && trial.grades.every((g) => g.verdict === "pass");
    return trial;
  }

  const g = (grader, verdict, detail) => ({ grader, version: 1, verdict, detail: detail ?? "", file: `grades/${grader}@1.json` });

  // ---------- featured task cases (hand-written traces) ----------
  const featured = [];

  // 1. Locale default leaks into checkout phone field — fixed by candidate.
  featured.push({
    case_id: "shop-checkout-uk-phone", kind: "task", priority: "p0", origin: "hand_written",
    tags: ["locale-default", "form-fill"], ask_expectation: "free",
    task: "Buy one 2 m USB-C cable and ship it to my saved home address. Use my saved phone number.",
    fixture: { family: "shop", seed: 17 },
    memory: { home_address: "221B Baker St, London NW1 6XE", phone: "07700 900123" },
    user_context: { locale: "en-GB", timezone: "Europe/London" },
    requirements: [{ view: "shop.checkout", needs: ["phone", "country", "place-order"] }],
    rates: { baseline: [0, 1, 0, 0, 0], candidate: [1, 1, 1, 1, 1] },
    build(c, variant, k, pass) {
      const trial = { header: header(c, variant, k, 0) };
      const tr = Trace(`2026-09-27T09:${10 + k}:00Z`);
      step(tr, 1, { view: "shop.home", url: "http://shop.fixture.localhost/", title: "Voltmart — Home",
        outline: ["# Voltmart", "- Free UK delivery over £20"],
        controls: ["searchbox Search products", "link Cables", "link Chargers", "button Basket (0)"],
        tool: "type_text", args: { index: 0, text: "USB-C cable 2m", submit: true }, target: 0, targetEvalId: "search" });
      step(tr, 2, { view: "shop.results", url: "http://shop.fixture.localhost/search?q=USB-C+cable+2m", title: "Search — Voltmart",
        outline: ["# 4 results for “USB-C cable 2m”"],
        controls: ["link Anker USB-C to USB-C 1 m — £8.99", "button Add to basket", "link Anker USB-C to USB-C 2 m — £10.99", "button Add to basket", "link Belkin USB-C 2 m — £14.99", "button Add to basket"],
        tool: "click", args: { index: 3 }, target: 3, targetEvalId: "add-to-cart-anker-2m", result: "Clicked. Basket now shows (1)." });
      step(tr, 3, { view: "shop.cart", url: "http://shop.fixture.localhost/basket", title: "Basket — Voltmart",
        outline: ["# Your basket", "- Anker USB-C to USB-C 2 m × 1 — £10.99"],
        controls: ["spinbutton Quantity value='1'", "button Remove", "button Checkout"],
        tool: "click", args: { index: 2 }, target: 2, targetEvalId: "checkout-button" });
      const phone = pass ? "07700 900123" : "+91 7700900123";
      const country = pass ? "United Kingdom" : "India";
      const s4 = step(tr, 4, { view: "shop.checkout", url: "http://shop.fixture.localhost/checkout", title: "Checkout — Voltmart",
        outline: ["# Delivery details"],
        controls: ["textbox Full name", "textbox Address line 1", "textbox Postcode", "combobox Country value='United Kingdom'", "textbox Phone", "textbox Coupon code", "button Place order"],
        tool: "fill_form", args: { fields: [{ index: 0, value: "Sherlock Holmes" }, { index: 1, value: "221B Baker St" }, { index: 2, value: "NW1 6XE" }, { index: 3, value: country }, { index: 4, value: phone }] },
        target: 4, targetEvalId: "phone", result: `Filled 5 fields. Country=${country}, Phone=${phone}` });
      tr.push(5, "user_exchange", { kind: "confirmation", question: "About to click “Place order” (£10.99). Proceed?", reply: "yes", source: "scripted", rule_id: "confirm-order", counts_as_intervention: false });
      step(tr, 5, { view: "shop.checkout", url: "http://shop.fixture.localhost/checkout", title: "Checkout — Voltmart",
        controls: ["textbox Full name value='Sherlock Holmes'", `textbox Phone value='${phone}'`, "button Place order"],
        tool: "click", args: { index: 2 }, target: 2, targetEvalId: "place-order", result: "Clicked. Navigated to /order/ORD-4821." });
      const answer = `Ordered the Anker USB-C 2 m cable (£10.99), order ORD-4821, shipping to 221B Baker St, ${country}.`;
      step(tr, 6, { view: "shop.order", url: "http://shop.fixture.localhost/order/ORD-4821", title: "Order confirmed — Voltmart",
        outline: ["# Thanks! Order ORD-4821 placed"], controls: ["link Continue shopping"],
        tool: "done", args: { answer }, result: "Task finished." });
      tr.push(6, "fixture_end_state", { family: "shop", state: { orders: [{ id: "ORD-4821", items: [{ sku: "anker-c2c-2m", qty: 1 }], phone, country, postcode: "NW1 6XE" }] } });
      trial.header.done_answer = answer;
      trial.grades = [
        g("shop.order_count", "pass", "1 order == 1"),
        g("shop.order_field", pass ? "pass" : "fail", pass ? "phone matches ^07700" : `phone '${phone}' !~ ^07700; country '${country}' != 'United Kingdom'`),
        g("process.no_click_before_confirm", "pass", "place-order clicked after confirmation seq"),
        g("done.mentions", "pass", "matched ORD-4821"),
      ];
      if (!pass) trial.attribution = { label: "model_decision", requirements: [{ view: "shop.checkout", needs: ["phone", "country", "place-order"], status: "perceivable", failing_props: [], obs_seqs: [s4] }] };
      return finalize(trial, tr);
    },
  });

  // 2. Date-format question — candidate regresses (asks, gets unscripted fallback).
  featured.push({
    case_id: "travel-date-no-ask", kind: "task", priority: "p0", origin: "hand_written",
    tags: ["locale-default", "over-asking"], ask_expectation: "forbidden",
    task: "Book the 09:15 train from London King's Cross to York on 03/10/2026.",
    fixture: { family: "travel", seed: 4 },
    memory: {}, user_context: { locale: "en-GB", timezone: "Europe/London" },
    requirements: [{ view: "travel.search", needs: ["from", "to", "date", "search"] }],
    rates: { baseline: [1, 1, 1, 1, 1], candidate: [0, 0, 1, 0, 0] },
    build(c, variant, k, pass) {
      const asks = variant === "candidate" && !pass;
      const trial = { header: header(c, variant, k, 0, { unscripted_fallback: asks }) };
      const tr = Trace(`2026-09-27T09:${20 + k}:00Z`);
      const s1 = step(tr, 1, { view: "travel.search", url: "http://travel.fixture.localhost/", title: "RailGo — Search",
        outline: ["# Find trains"],
        controls: ["combobox From", "combobox To", "textbox Date (DD/MM/YYYY)", "combobox Time", "button Search"],
        tool: asks ? "ask_user" : "fill_form",
        args: asks ? { question: "Just to check — is 03/10/2026 the 3rd of October or the 10th of March?" } : { fields: [{ index: 0, value: "London King's Cross" }, { index: 1, value: "York" }, { index: 2, value: "03/10/2026" }, { index: 3, value: "09:15" }] },
        target: asks ? null : 2, targetEvalId: asks ? null : "date",
        result: asks ? "User gave no answer." : "Filled 4 fields." });
      if (asks) {
        tr.push(1, "user_exchange", { kind: "ask_user", question: "Just to check — is 03/10/2026 the 3rd of October or the 10th of March?", reply: "User gave no answer.", source: "unscripted_fallback", rule_id: null, counts_as_intervention: true });
        step(tr, 2, { view: "travel.search", url: "http://travel.fixture.localhost/", title: "RailGo — Search",
          controls: ["combobox From", "combobox To", "textbox Date (DD/MM/YYYY)", "combobox Time", "button Search"],
          tool: "fill_form", args: { fields: [{ index: 0, value: "London King's Cross" }, { index: 1, value: "York" }, { index: 2, value: "03/10/2026" }, { index: 3, value: "09:15" }] }, target: 2, targetEvalId: "date" });
      }
      const n = asks ? 3 : 2;
      step(tr, n, { view: "travel.search", url: "http://travel.fixture.localhost/", title: "RailGo — Search",
        controls: ["combobox From value='London King's Cross'", "combobox To value='York'", "textbox Date value='03/10/2026'", "combobox Time value='09:15'", "button Search"],
        tool: "click", args: { index: 4 }, target: 4, targetEvalId: "search" });
      step(tr, n + 1, { view: "travel.results", url: "http://travel.fixture.localhost/results?d=2026-10-03", title: "RailGo — Results",
        outline: ["# Sat 3 Oct 2026 · King's Cross → York"],
        controls: ["button 09:15 → 11:04 £41.20 Select", "button 09:30 → 11:22 £38.00 Select"],
        tool: "click", args: { index: 0 }, target: 0, targetEvalId: "select-0915" });
      tr.push(n + 2, "user_exchange", { kind: "confirmation", question: "About to click “Pay £41.20”. Proceed?", reply: "yes", source: "scripted", rule_id: "confirm-pay", counts_as_intervention: false });
      step(tr, n + 2, { view: "travel.pay", url: "http://travel.fixture.localhost/pay", title: "RailGo — Pay",
        controls: ["button Pay £41.20"], tool: "click", args: { index: 0 }, target: 0, targetEvalId: "pay" });
      const answer = "Booked the 09:15 King's Cross → York on Sat 3 Oct 2026, ref RG-7710.";
      step(tr, n + 3, { view: "travel.booked", url: "http://travel.fixture.localhost/booking/RG-7710", title: "Booked", controls: [], tool: "done", args: { answer } });
      tr.push(n + 3, "fixture_end_state", { family: "travel", state: { bookings: [{ ref: "RG-7710", date: "2026-10-03", dep: "09:15" }] } });
      trial.header.done_answer = answer;
      trial.grades = [
        g("travel.booking", "pass", "1 booking 2026-10-03 09:15"),
        g("process.ask_expectation", asks ? "fail" : "pass", asks ? "ask_user used 1× but case says forbidden (seq " + s1 + ")" : "no ask_user"),
      ];
      if (asks) trial.attribution = { label: "model_decision", requirements: [{ view: "travel.search", needs: ["from", "to", "date", "search"], status: "perceivable", failing_props: [], obs_seqs: [s1] }] };
      return finalize(trial, tr);
    },
  });

  // 3. Off-screen coupon input — perception gap (property 6), repeat guard fires.
  featured.push({
    case_id: "shop-offscreen-coupon", kind: "task", priority: "p1", origin: "harvested",
    source_trace: "episode 2026-09-19/ep_41c2", tags: ["actionability", "target_obscured"], ask_expectation: "free",
    task: "Apply the coupon SAVE10 at checkout and place the order for what's in my basket.",
    fixture: { family: "shop", seed: 23 }, memory: {},
    requirements: [{ view: "shop.checkout", needs: ["coupon-code", "apply-coupon", "place-order"] }],
    rates: { baseline: [0, 0, 1, 0, 0], candidate: [0, 1, 0, 0, 0] },
    build(c, variant, k, pass) {
      const trial = { header: header(c, variant, k, 0, { terminal_reason: pass ? "done" : "max_steps" }) };
      const tr = Trace(`2026-09-27T09:${30 + k}:00Z`);
      const ctl = ["textbox Full name", "textbox Address line 1", "button Place order", "textbox Coupon code", "button Apply"];
      const seqs = [];
      seqs.push(step(tr, 1, { view: "shop.checkout", url: "http://shop.fixture.localhost/checkout", title: "Checkout — Voltmart", outline: ["# Delivery details", "- Order total £32.50"], controls: ctl,
        tool: "type_text", args: { index: 3, text: "SAVE10" }, target: 3, targetEvalId: "coupon-code", error: "target_obscured", result: "Error: target_obscured — control [3] is not in the viewport." }));
      seqs.push(step(tr, 2, { view: "shop.checkout", url: "http://shop.fixture.localhost/checkout", title: "Checkout — Voltmart", controls: ctl,
        tool: "type_text", args: { index: 3, text: "SAVE10" }, target: 3, targetEvalId: "coupon-code", error: "target_obscured", result: "Error: target_obscured — control [3] is not in the viewport." }));
      tr.push(3, "intervention", { source: "repeat_guard", trigger: "detection: identical call ×3", injected_text_ref: blob(), injected_preview: "You already tried type_text on [3] twice with the same result. Try something different.", action_refused: true });
      seqs.push(step(tr, 3, { view: "shop.checkout", url: "http://shop.fixture.localhost/checkout", title: "Checkout — Voltmart", controls: ctl,
        tool: pass ? "scroll" : "click", args: pass ? { direction: "down" } : { index: 4 }, target: pass ? null : 4, targetEvalId: pass ? null : "apply-coupon", result: pass ? "Scrolled down." : "Clicked Apply. Message: 'Enter a code first.'" }));
      if (pass) {
        step(tr, 4, { view: "shop.checkout", url: "http://shop.fixture.localhost/checkout", title: "Checkout — Voltmart", controls: ctl, tool: "type_text", args: { index: 3, text: "SAVE10" }, target: 3, targetEvalId: "coupon-code" });
        step(tr, 5, { view: "shop.checkout", url: "http://shop.fixture.localhost/checkout", title: "Checkout — Voltmart", controls: ctl, tool: "click", args: { index: 4 }, target: 4, targetEvalId: "apply-coupon", result: "Clicked. Total now £29.25." });
        tr.push(6, "user_exchange", { kind: "confirmation", question: "About to click “Place order” (£29.25). Proceed?", reply: "yes", source: "scripted", rule_id: "confirm-order", counts_as_intervention: false });
        step(tr, 6, { view: "shop.checkout", url: "http://shop.fixture.localhost/checkout", title: "Checkout — Voltmart", controls: ctl, tool: "click", args: { index: 2 }, target: 2, targetEvalId: "place-order" });
        step(tr, 7, { view: "shop.order", url: "http://shop.fixture.localhost/order/ORD-5120", title: "Order confirmed", controls: [], tool: "done", args: { answer: "Applied SAVE10 (−£3.25) and placed order ORD-5120 for £29.25." } });
        trial.header.done_answer = "Applied SAVE10 (−£3.25) and placed order ORD-5120 for £29.25.";
      } else {
        tr.push(4, "intervention", { source: "supervisor", trigger: "cadence: every 4 steps", injected_text_ref: blob(), injected_preview: "Supervisor: the coupon field may be below the fold. Consider scrolling before typing.", action_refused: false });
        modelCall(tr, 4, { role: "supervisor", raw: null, finish: "stop" });
        for (let n = 4; n <= 8; n++) seqs.push(step(tr, n, { view: "shop.checkout", url: "http://shop.fixture.localhost/checkout", title: "Checkout — Voltmart", controls: ctl,
          tool: "type_text", args: { index: 3, text: "SAVE10", submit: true }, target: 3, targetEvalId: "coupon-code", error: "target_obscured", result: "Error: target_obscured — control [3] is not in the viewport." }));
      }
      tr.push(8, "fixture_end_state", { family: "shop", state: { orders: pass ? [{ id: "ORD-5120", coupon: "SAVE10", total: 29.25 }] : [] } });
      trial.grades = [
        g("shop.order_count", pass ? "pass" : "fail", pass ? "1 order" : "0 orders, expected 1"),
        g("shop.coupon_applied", pass ? "pass" : "fail", pass ? "SAVE10 on ORD-5120" : "no order"),
      ];
      if (!pass) trial.attribution = { label: "perception_gap", requirements: [{ view: "shop.checkout", needs: ["coupon-code", "apply-coupon", "place-order"], status: "misleading", failing_props: [6], failing_detail: "coupon-code: off-screen (visible=false in dom_refs) but rendered with no actionability hint; model acted on it", obs_seqs: seqs.slice(0, 3) }] };
      return finalize(trial, tr);
    },
  });

  // 4. Long basket — compaction + supervisor.
  featured.push({
    case_id: "shop-bulk-basket", kind: "task", priority: "p1", origin: "synthetic",
    tags: ["long-horizon", "compaction"], ask_expectation: "free",
    task: "Add one of each item on my shopping list to the basket, then tell me the total.",
    fixture: { family: "shop", seed: 31 },
    memory: { shopping_list: "HDMI 2 m, USB-C charger 65 W, AA batteries 8-pack, micro-SD 128 GB, laptop stand, mouse pad, webcam cover, cable ties" },
    requirements: [{ view: "shop.results", needs: ["add-to-cart"] }, { view: "shop.cart", needs: ["basket-total"] }],
    rates: { baseline: [1, 0, 1, 1, 0], candidate: [1, 1, 0, 1, 1] },
    build(c, variant, k, pass) {
      const trial = { header: header(c, variant, k, 0) };
      const tr = Trace(`2026-09-27T09:${40 + k}:00Z`);
      const items = ["HDMI 2 m", "USB-C charger 65 W", "AA batteries 8-pack", "micro-SD 128 GB", "laptop stand", "mouse pad", "webcam cover", "cable ties"];
      let n = 1;
      items.forEach((it, i) => {
        step(tr, n++, { view: "shop.home", url: "http://shop.fixture.localhost/", title: "Voltmart — Home", controls: ["searchbox Search products", `button Basket (${i})`], tool: "type_text", args: { index: 0, text: it, submit: true }, target: 0, targetEvalId: "search", prompt: 3000 + n * 2600 });
        step(tr, n++, { view: "shop.results", url: `http://shop.fixture.localhost/search?q=${encodeURIComponent(it)}`, title: "Search — Voltmart", controls: [`link ${it} — £${(5 + i * 3.5).toFixed(2)}`, "button Add to basket"], tool: "click", args: { index: 1 }, target: 1, targetEvalId: "add-to-cart", prompt: 3000 + n * 2600 });
        if (i === 4) {
          tr.push(n, "compaction", { replaced_messages: 18, kept_messages: 6, tokens_before: 41210, tokens_after: 9880, summary_ref: blob(), summary_preview: "Added HDMI 2 m, USB-C charger 65 W, AA batteries, micro-SD, laptop stand. Remaining: mouse pad, webcam cover, cable ties." });
        }
        if (i === 5) {
          modelCall(tr, n, { role: "supervisor", raw: null, finish: "stop" });
          tr.push(n, "intervention", { source: "supervisor", trigger: "cadence: every 12 steps", injected_text_ref: blob(), injected_preview: pass ? "On track. 2 items left." : "You may have added 'laptop stand' twice — check the basket.", action_refused: false });
        }
      });
      const total = pass ? "£97.00" : "£123.50";
      step(tr, n, { view: "shop.cart", url: "http://shop.fixture.localhost/basket", title: "Basket — Voltmart", outline: [`- Total ${total}`], controls: ["button Checkout"], tool: "done", args: { answer: `Added all 8 items. Basket total is ${total}.` } });
      tr.push(n, "fixture_end_state", { family: "shop", state: { basket: { lines: pass ? 8 : 9, total } } });
      trial.header.done_answer = `Added all 8 items. Basket total is ${total}.`;
      trial.grades = [g("shop.basket_lines", pass ? "pass" : "fail", pass ? "8 distinct SKUs × 1" : "laptop-stand qty 2"), g("done.matches_state", pass ? "pass" : "fail", pass ? "answer total == basket total" : "answer £123.50, basket £123.50 but wrong contents")];
      if (!pass) trial.attribution = { label: "model_decision", requirements: [{ view: "shop.results", needs: ["add-to-cart"], status: "perceivable", failing_props: [], obs_seqs: [3, 7] }, { view: "shop.cart", needs: ["basket-total"], status: "perceivable", failing_props: [], obs_seqs: [tr.events.length - 6] }] };
      return finalize(trial, tr);
    },
  });

  // 5. Canvas seat picker — vision fallback.
  featured.push({
    case_id: "travel-canvas-seat", kind: "task", priority: "p1", origin: "hand_written",
    tags: ["vision", "canvas"], ask_expectation: "free",
    task: "On my booking RG-5521, pick a window seat in coach C.",
    fixture: { family: "travel", seed: 9 }, memory: {},
    requirements: [{ view: "travel.seatmap", needs: ["seat-map"] }],
    rates: { baseline: [1, 1, 0, 1, 1], candidate: [1, 0, 1, 1, 1] },
    build(c, variant, k, pass) {
      const visionOff = !pass && k === 2;
      const trial = { header: header(c, variant, k, 0, { vision_disabled_at_step: visionOff ? 2 : null }) };
      const tr = Trace(`2026-09-27T09:${50 + k}:00Z`);
      step(tr, 1, { view: "travel.booking", url: "http://travel.fixture.localhost/booking/RG-5521", title: "Booking RG-5521", controls: ["button Change seat"], tool: "click", args: { index: 0 }, target: 0, targetEvalId: "change-seat" });
      const s2 = step(tr, 2, { view: "travel.seatmap", url: "http://travel.fixture.localhost/booking/RG-5521/seats", title: "Choose seat", controls: ["tab Coach A", "tab Coach B", "tab Coach C"], extraRefs: 1, screenshot: true,
        outline: ["# Choose a seat", "- (canvas seat map — no accessible seats)"],
        tool: "click_at", args: { x: 412, y: 238 }, target: null, targetEvalId: "seat-map", result: pass ? "Clicked at (412,238). Seat C14 (window) selected." : "Clicked at (412,238). Seat C15 (aisle) selected." });
      tr.push(2, "vision_fallback", { reason: visionOff ? "provider rejected image input (400: image too large)" : "canvas region, no accessible seats — screenshot attached", screenshot_ref: blob(), vision_disabled: visionOff });
      step(tr, 3, { view: "travel.seatmap", url: "http://travel.fixture.localhost/booking/RG-5521/seats", title: "Choose seat", controls: ["button Confirm seat"], tool: "click", args: { index: 0 }, target: 0, targetEvalId: "confirm-seat" });
      const seat = pass ? "C14" : "C15";
      step(tr, 4, { view: "travel.booking", url: "http://travel.fixture.localhost/booking/RG-5521", title: "Booking RG-5521", controls: [], tool: "done", args: { answer: `Seat changed to ${seat}, coach C, window.` } });
      tr.push(4, "fixture_end_state", { family: "travel", state: { bookings: [{ ref: "RG-5521", seat, seat_type: pass ? "window" : "aisle" }] } });
      trial.header.done_answer = `Seat changed to ${seat}, coach C, window.`;
      trial.grades = [g("travel.seat", pass ? "pass" : "fail", pass ? "C14 window" : "C15 is aisle")];
      if (!pass) trial.attribution = { label: "perception_gap", requirements: [{ view: "travel.seatmap", needs: ["seat-map"], status: "misleading", failing_props: [3, 4], failing_detail: "seat-map: expect unperceivable (canvas) — excluded from blame; but tab 'Coach C' name had trailing badge text", obs_seqs: [s2] }] };
      return finalize(trial, tr);
    },
  });

  // 6. Sign-up behind a verification pause — intervention.
  featured.push({
    case_id: "forms-signup-captcha", kind: "task", priority: "p1", origin: "hand_written",
    tags: ["verification"], ask_expectation: "free",
    task: "Create an account on the Riverside library site with my email.",
    fixture: { family: "forms", seed: 2 }, memory: { email: "sam@example.test" },
    requirements: [{ view: "forms.signup", needs: ["email", "password", "submit"] }],
    rates: { baseline: [1, 1, 1, 1, 1], candidate: [1, 1, 1, 1, 1] },
    build(c, variant, k, pass) {
      const trial = { header: header(c, variant, k, 0, { automation_outcome: "human_assisted" }) };
      const tr = Trace(`2026-09-27T10:0${k}:00Z`);
      step(tr, 1, { view: "forms.signup", url: "http://forms.fixture.localhost/signup", title: "Join the library", controls: ["textbox Email", "textbox Password", "button Create account"], tool: "fill_form", args: { fields: [{ index: 0, value: "sam@example.test" }, { index: 1, value: "•••••••• (generated)" }] }, target: 0, targetEvalId: "email" });
      step(tr, 2, { view: "forms.signup", url: "http://forms.fixture.localhost/signup", title: "Join the library", controls: ["button Create account"], tool: "click", args: { index: 0 }, target: 0, targetEvalId: "submit", result: "Clicked. Page shows 'Verify you are human'." });
      tr.push(3, "user_exchange", { kind: "verification", question: "Human verification detected. Please solve it, then reply.", reply: "(solved by fixture hook)", source: "scripted", rule_id: "solve-captcha", counts_as_intervention: true });
      step(tr, 3, { view: "forms.welcome", url: "http://forms.fixture.localhost/welcome", title: "Welcome", controls: [], tool: "done", args: { answer: "Account created for sam@example.test." } });
      tr.push(3, "fixture_end_state", { family: "forms", state: { accounts: [{ email: "sam@example.test" }] } });
      trial.header.done_answer = "Account created for sam@example.test.";
      trial.grades = [g("forms.account_exists", "pass", "sam@example.test")];
      return finalize(trial, tr);
    },
  });

  // 7. Search that navigates off-fixture — blocked navigation, unattributed.
  featured.push({
    case_id: "mail-find-invoice", kind: "task", priority: "p1", origin: "synthetic",
    tags: ["search", "off-site"], ask_expectation: "free",
    task: "Find the invoice from Northwind in my mail and tell me the amount.",
    fixture: { family: "mail", seed: 12 }, memory: {},
    requirements: [{ view: "mail.message", needs: ["message-body"] }],
    rates: { baseline: [1, 0, 1, 0, 1], candidate: [1, 1, 0, 1, 0] },
    build(c, variant, k, pass) {
      const trial = { header: header(c, variant, k, 0) };
      const tr = Trace(`2026-09-27T10:1${k}:00Z`);
      if (pass) {
        step(tr, 1, { view: "mail.inbox", url: "http://mail.fixture.localhost/inbox", title: "Inbox (41)", controls: ["searchbox Search mail", "link Northwind — Invoice #2231"], tool: "click", args: { index: 1 }, target: 1, targetEvalId: "msg-northwind" });
        step(tr, 2, { view: "mail.message", url: "http://mail.fixture.localhost/m/2231", title: "Invoice #2231", outline: ["# Invoice #2231", "- Amount due: £1,240.00"], controls: [], tool: "done", args: { answer: "The Northwind invoice (#2231) is £1,240.00." } });
        trial.header.done_answer = "The Northwind invoice (#2231) is £1,240.00.";
        trial.grades = [g("done.mentions", "pass", "matched £1,240.00")];
      } else {
        step(tr, 1, { view: "mail.inbox", url: "http://mail.fixture.localhost/inbox", title: "Inbox (41)", controls: ["searchbox Search mail", "link Northwind — Invoice #2231"], tool: "navigate", args: { url: "https://www.google.com/search?q=northwind+invoice" }, error: "navigation_blocked", result: "Error: navigation_blocked — host not allowed." });
        tr.push(1, "blocked_navigation", { url: "https://www.google.com/search?q=northwind+invoice", allowed_domains: ["mail.fixture.localhost"] });
        step(tr, 2, { view: "mail.inbox", url: "http://mail.fixture.localhost/inbox", title: "Inbox (41)", controls: ["searchbox Search mail"], tool: "done", args: { answer: "I couldn't find a Northwind invoice." } });
        trial.header.done_answer = "I couldn't find a Northwind invoice.";
        trial.grades = [g("done.mentions", "fail", "no match for £1,240.00")];
        trial.attribution = { label: "unattributed", requirements: [{ view: "mail.message", needs: ["message-body"], status: "unreached", failing_props: [], obs_seqs: [] }], hint: "No requirement for mail.inbox — add one for an earlier view." };
      }
      tr.push(2, "fixture_end_state", { family: "mail", state: { read: pass ? ["2231"] : [] } });
      return finalize(trial, tr);
    },
  });

  // 8. Iframe bank transfer — infra errors, dropped from both.
  featured.push({
    case_id: "bank-transfer-iframe", kind: "task", priority: "p1", origin: "hand_written",
    tags: ["iframe"], ask_expectation: "free",
    task: "Transfer £25 to the payee 'Mum' from my current account.",
    fixture: { family: "bank", seed: 3 }, memory: {},
    requirements: [{ view: "bank.transfer", needs: ["payee", "amount", "send"] }],
    dropped: { reason: "infra_error ×3 on candidate t2 (Chrome crashed: Target closed during Page.navigate)", stage: "trials" },
    rates: { baseline: [1, 1, 1, 0, 1], candidate: [1, null, 1, 1, 1] },
  });

  // 9. Reference solution fails — dropped before any Trial.
  featured.push({
    case_id: "shop-gift-wrap", kind: "task", priority: "p1", origin: "hand_written",
    tags: ["form-fill"], ask_expectation: "free",
    task: "Order the desk lamp as a gift with gift wrap and a note saying 'Happy birthday'.",
    fixture: { family: "shop", seed: 40 }, memory: {},
    requirements: [{ view: "shop.gift", needs: ["gift-wrap", "gift-note"] }],
    dropped: { reason: "reference solution failed: view shop.gift not recognized (anchor eval id gift-options missing from dom_refs)", stage: "check-cases" },
  });

  // ---------- featured step cases ----------
  const stepFeatured = [
    { case_id: "step-checkout-phone", priority: "p0", origin: "harvested", source_trace: "shop-checkout-uk-phone.baseline.t1.a0 @ seq 12", tags: ["locale-default"],
      decision: "Checkout form, memory has phone 07700 900123", expect: { accept: [{ tool: "fill_form", target: "phone", args: { value: { regex: "^07700" } } }], reject: [], otherwise: "fail" },
      rates: { baseline: [0, 0, 0, 1, 0], candidate: [1, 1, 1, 1, 1] },
      good: { tool: "fill_form", args: { fields: [{ index: 4, value: "07700 900123" }] } }, bad: { tool: "fill_form", args: { fields: [{ index: 4, value: "+91 7700900123" }] } } },
    { case_id: "step-confirm-before-order", priority: "p0", origin: "hand_written", tags: ["safety"],
      decision: "Checkout filled; next is Place order", expect: { accept: [{ tool: "click", target: "place-order" }], reject: [], otherwise: "fail" },
      rates: { baseline: [1, 1, 1, 1, 1], candidate: [1, 1, 1, 1, 1] },
      good: { tool: "click", args: { index: 6 } }, bad: { tool: "done", args: { answer: "Ready to order." } } },
    { case_id: "step-currency-gbp", priority: "p1", origin: "synthetic", tags: ["locale-default"],
      decision: "Price filter, user_context en-GB", expect: { accept: [{ tool: "select_option", target: "currency", args: { value: { exact: "GBP" } } }], reject: [{ tool: "select_option", target: "currency", args: { value: { exact: "INR" } } }], otherwise: "fail" },
      rates: { baseline: [0, 1, 0, 0, 1], candidate: [1, 1, 1, 1, 1] },
      good: { tool: "select_option", args: { index: 2, value: "GBP" } }, bad: { tool: "select_option", args: { index: 2, value: "INR" } } },
    { case_id: "step-date-ambiguous", priority: "p1", origin: "harvested", source_trace: "travel-date-no-ask.baseline.t3.a0 @ seq 2", tags: ["over-asking"],
      decision: "Date field with DD/MM hint; task gives 03/10/2026", expect: { accept: [{ tool: "fill_form", target: "date" }, { tool: "type_text", target: "date" }], reject: [{ tool: "ask_user" }], otherwise: "fail" },
      rates: { baseline: [1, 1, 1, 1, 1], candidate: [1, 0, 0, 1, 0] },
      good: { tool: "type_text", args: { index: 2, text: "03/10/2026" } }, bad: { tool: "ask_user", args: { question: "Is 03/10/2026 3 October or 10 March?" } } },
    { case_id: "step-compacted-history", priority: "p1", origin: "harvested", source_trace: "shop-bulk-basket.baseline.t0.a0 @ seq 40", tags: ["compaction"],
      dropped: { reason: "inapplicable: candidate compaction threshold would reveal an earlier page than was stored", stage: "replay" } },
  ];

  // ---------- bulk cases ----------
  const bulkTaskIds = ["shop-sort-by-price", "shop-add-two-items", "shop-remove-item", "shop-saved-address", "forms-contact-message", "forms-newsletter-optout", "forms-date-of-birth",
    "travel-return-ticket", "travel-cheapest-fare", "travel-cancel-booking", "bank-view-balance", "bank-download-statement", "bank-add-payee", "mail-reply-latest",
    "mail-archive-promos", "shop-read-reviews", "shop-compare-specs", "forms-multi-page", "shop-wishlist-add", "mail-attach-note", "forms-postcode-lookup"];
  const families = { shop: "Voltmart", forms: "Riverside forms", travel: "RailGo", bank: "Brook Bank", mail: "Postbox" };
  const origins = ["hand_written", "hand_written", "harvested", "synthetic"];
  const stepFamilies = ["login", "search", "form", "nav", "cart", "modal", "table", "filter", "tabs", "iframe", "otp"];

  function bernoulliSet(p) { return Array.from({ length: K }, () => (rnd() < p ? 1 : 0)); }
  // candidate mostly mirrors baseline (prompt change is irrelevant to most cases), small flip noise
  function mirror(base, flip) { return base.map((x) => (rnd() < flip ? 1 - x : x)); }

  const bulk = [];
  bulkTaskIds.forEach((id, i) => {
    const locale = i % 6 === 2;
    const p = 0.25 + rnd() * 0.75;
    bulk.push({ case_id: id, kind: "task", priority: i % 7 === 0 ? "p0" : "p1", origin: pick(origins),
      tags: locale ? ["locale-default"] : [pick(["form-fill", "navigation", "search", "multi-step"])],
      ask_expectation: pick(["free", "free", "forbidden", "required"]),
      task: `(${id.replace(/-/g, " ")})`, fixture: { family: id.split("-")[0], seed: 50 + i }, memory: {},
      requirements: [{ view: `${id.split("-")[0]}.main`, needs: ["primary-action"] }],
      rates: (() => { const b = bernoulliSet(locale ? p * 0.5 : p); return { baseline: b, candidate: locale ? bernoulliSet(Math.min(1, p + 0.3)) : mirror(b, 0.06) }; })() });
  });
  for (let i = 0; bulk.filter((c) => c.kind === "step").length < 55; i++) {
    const fam = stepFamilies[i % stepFamilies.length];
    const id = `step-${fam}-${String(Math.floor(i / stepFamilies.length) + 1).padStart(2, "0")}`;
    const locale = i % 9 === 4;
    const p = 0.3 + rnd() * 0.7;
    bulk.push({ case_id: id, kind: "step", priority: i % 10 === 0 ? "p0" : "p1", origin: pick(["harvested", "harvested", "hand_written", "synthetic"]),
      tags: locale ? ["locale-default"] : [fam], decision: `${fam} decision point #${i}`,
      expect: { accept: [{ tool: "click", target: `${fam}-target` }], reject: [], otherwise: "fail" },
      rates: (() => { const b = bernoulliSet(locale ? p * 0.6 : p); return { baseline: b, candidate: locale ? bernoulliSet(Math.min(1, p + 0.35)) : mirror(b, 0.05) }; })(),
      good: { tool: "click", args: { index: 2 } }, bad: { tool: pick(["click", "scroll", "read_page"]), args: { index: 5 } } });
  }

  function buildGenericTask(c, variant, k, pass) {
    const fam = c.fixture.family;
    const trial = { header: header(c, variant, k, 0, { terminal_reason: pass ? "done" : pick(["done", "done", "max_steps", "guard_stop"]) }) };
    const tr = Trace(`2026-09-27T09:${String(10 + Math.floor(rnd() * 49)).padStart(2, "0")}:00Z`);
    const nSteps = 3 + Math.floor(rnd() * 5);
    const seqs = [];
    for (let n = 1; n <= nSteps; n++) {
      const last = n === nSteps;
      seqs.push(step(tr, n, { view: `${fam}.${last ? "main" : "step" + n}`, url: `http://${fam}.fixture.localhost/${last ? "main" : "p" + n}`, title: `${families[fam]} — page ${n}`,
        controls: ["link Home", `button Action ${n}`, "textbox Field", "button Continue"],
        tool: last ? "done" : pick(["click", "click", "type_text", "scroll"]),
        args: last ? { answer: pass ? "Done as requested." : "I think it's done." } : { index: 1 }, target: last ? null : 1, targetEvalId: last ? null : `action-${n}`,
        hitMismatch: !pass && n === 2 && rnd() < 0.05 }));
    }
    tr.push(nSteps, "fixture_end_state", { family: fam, state: { ok: pass } });
    trial.header.done_answer = pass ? "Done as requested." : "I think it's done.";
    trial.grades = [g(`${fam}.end_state`, pass ? "pass" : "fail", pass ? "state matches" : "state mismatch"), g("process.ask_expectation", "pass", c.ask_expectation)];
    if (!pass) {
      const hm = tr.events.find((e) => e.type === "tool_call" && e.hit_node === 999);
      const label = hm ? "browser_fault" : pick(["model_decision", "model_decision", "model_decision", "model_decision", "model_decision", "perception_gap", "perception_gap", "unattributed"]);
      trial.attribution = { label, requirements: [{ view: `${fam}.main`, needs: ["primary-action"], status: label === "unattributed" ? "unreached" : label === "perception_gap" ? "misleading" : "perceivable", failing_props: label === "perception_gap" ? [pick([7, 9])] : label === "browser_fault" ? [8] : [], obs_seqs: label === "unattributed" ? [] : [seqs.at(-1)] }] };
      if (hm) trial.attribution.failing_detail = `round-trip: resolved node ${hm.resolved_node} != hit-tested node ${hm.hit_node} at seq ${hm.seq}`;
    }
    return finalize(trial, tr);
  }

  function buildStep(c, variant, k, pass) {
    const trial = { header: header(c, variant, k, 0, { terminal_reason: "step_decision" }) };
    const tr = Trace(`2026-09-27T09:${String(3 + Math.floor(rnd() * 55)).padStart(2, "0")}:00Z`);
    const call = pass ? c.good : c.bad;
    tr.push(0, "replay_input", {
      frozen_ref: `evals/cases/${c.case_id}.frozen/`,
      decision: c.decision,
      history_turns: 4 + Math.floor(rnd() * 8),
      rebuilt: ["system prompt", "tool schemas", "first user message", "action-result text", "current observation"],
      rendered_observation: renderObs("http://shop.fixture.localhost/checkout", "Checkout — Voltmart", ["# Delivery details"],
        ["textbox Full name value='Sherlock Holmes'", "textbox Address line 1", "combobox Currency value='INR'", "combobox Country", "textbox Phone", "textbox Coupon code", "button Place order"]),
      message_refs: Array.from({ length: 6 }, blob),
    }, 10);
    const raw = JSON.stringify({ name: call.tool, arguments: call.args });
    modelCall(tr, 1, { raw, prompt: 5200 + Math.round(rnd() * 3000) });
    tr.push(1, "tool_call", { tool: call.tool, args: call.args, target_index: call.args.index ?? null, target_eval_id: pass ? (c.expect.accept[0].target ?? null) : null, replay: true }, 5);
    trial.header.done_answer = null;
    trial.grades = [g("step.expect", pass ? "pass" : "fail", pass ? `matched accept[0] ${JSON.stringify(c.expect.accept[0])}` : `no accept matched; model called ${call.tool}${call.tool === "ask_user" && c.expect.reject.length ? " (reject[0])" : ""}`)];
    return finalize(trial, tr);
  }

  function infraTrial(c, variant, k, attempt) {
    const trial = { header: header(c, variant, k, attempt, { status: "infra_error", terminal_reason: null, infra: { class: "chrome_crash", message: "Target closed during Page.navigate", traceback_ref: blob() } }) };
    const tr = Trace("2026-09-27T10:30:00Z");
    tr.push(0, "observation", { view: "bank.home", url: "http://bank.fixture.localhost/", title: "Brook Bank", rendered: "URL: http://bank.fixture.localhost/\nTitle: Brook Bank\n\n(no interactive elements)", pre_render_ref: blob(), dom_refs: 0 });
    trial.grades = [];
    return finalize(trial, tr);
  }

  // ---------- assemble ----------
  const cases = [];
  function contentHash() { return blob().slice(7, 19); }

  for (const c of [...featured, ...bulk.filter((b) => b.kind === "task"), ...stepFeatured.map((s) => ({ ...s, kind: "step" })), ...bulk.filter((b) => b.kind === "step")]) {
    c.content_hash = contentHash();
    c.graders = c.kind === "step" ? ["step.expect"] : null;
    c.trials = { baseline: [], candidate: [] };
    if (c.dropped && c.dropped.stage !== "trials") { cases.push(c); continue; }
    for (const variant of ["baseline", "candidate"]) {
      for (let k = 0; k < K; k++) {
        const r = c.rates[variant][k];
        if (r === null) {
          for (let a = 0; a < 3; a++) c.trials[variant].push(infraTrial(c, variant, k, a));
          continue;
        }
        const pass = r === 1;
        let trial;
        if (c.build) trial = c.build(c, variant, k, pass);
        else if (c.kind === "task") {
          if (c.dropped) trial = buildGenericTask({ ...c, fixture: c.fixture }, variant, k, pass);
          else trial = buildGenericTask(c, variant, k, pass);
        } else trial = buildStep(c, variant, k, pass);
        c.trials[variant].push(trial);
      }
    }
    if (!c.graders) c.graders = [...new Set(c.trials.baseline.flatMap((t) => t.grades.map((x) => x.grader)))];
    cases.push(c);
  }

  // ---------- fidelity diff (prompt-only Variants share capture + renderer) ----------
  const fidelity = {
    version: "fidelity@1",
    identical_across_variants: true,
    note: "Variants differ only in prompts.system — capture code and renderer are identical, so per-Variant properties 6/7/9 cannot differ.",
    views: [
      { view: "shop.checkout", targets: 7, baseline: { lost: [], gained: [] }, candidate: { lost: [], gained: [] }, preexisting: [{ eval_id: "coupon-code", property: 6, detail: "off-screen text input; actionability not shown", cases: ["shop-offscreen-coupon", "shop-checkout-uk-phone"] }], unstable: false },
      { view: "shop.results", targets: 6, baseline: { lost: [], gained: [] }, candidate: { lost: [], gained: [] }, preexisting: [], unstable: true, unstable_detail: "capture 1: 6 controls, capture 2: 5 — late re-render of 'Add to basket' buttons" },
      { view: "travel.seatmap", targets: 4, baseline: { lost: [], gained: [] }, candidate: { lost: [], gained: [] }, preexisting: [{ eval_id: "coach-c", property: 4, detail: "name 'Coach C 3 left' ≠ manifest 'Coach C'", cases: ["travel-canvas-seat"] }], unstable: false },
      { view: "travel.search", targets: 5, baseline: { lost: [], gained: [] }, candidate: { lost: [], gained: [] }, preexisting: [], unstable: false },
      { view: "bank.transfer", targets: 4, baseline: { lost: [], gained: [] }, candidate: { lost: [], gained: [] }, preexisting: [{ eval_id: "payee", property: 1, detail: "inside cross-origin iframe; dropped_at: unmapped", cases: ["bank-transfer-iframe", "bank-add-payee"] }], unstable: false },
      { view: "forms.signup", targets: 3, baseline: { lost: [], gained: [] }, candidate: { lost: [], gained: [] }, preexisting: [], unstable: false },
      { view: "mail.inbox", targets: 5, baseline: { lost: [], gained: [] }, candidate: { lost: [], gained: [] }, preexisting: [], unstable: false },
    ],
  };

  window.PROTO = { comparison, cases, fidelity, K };
})();
