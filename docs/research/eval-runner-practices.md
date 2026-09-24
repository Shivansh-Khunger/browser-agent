# Eval runner practices: rules distilled from the five sources

Research for [#25](https://github.com/shreverr/browser-agent/issues/25) (parent map [#24](https://github.com/shreverr/browser-agent/issues/24)). Written 2026-09-24.

**Question.** Which eval practices from the five sources apply to a CDP browser agent, and what concrete rules follow for this repo's **Eval runner**? Covers what LLMs grade well and badly, judge calibration, the error-analysis workflow, trace-first infrastructure, hidden infrastructure debt (reproducibility, flakiness, cost), and eval smells.

Vocabulary follows `CONTEXT.md` → *Evaluation*: Eval runner, Eval case, Step eval, Task eval, Fixture site, Eval suite, Variant, Trial, Grader, Comparison. When a source says rollout, trajectory, dataset, golden set, metric, scorer, judge prompt file, or harness, this doc uses the repo term instead. The one exception is direct quotes.

## Sources

| Tag | Source | What it actually covers |
|---|---|---|
| **S1** | Han Lee, [*Hidden Technical Debt of AI Systems: Agent Evaluation Infrastructure*](https://leehanchung.github.io/blogs/2026/06/13/hidden-technical-debt-agent-evaluation-infra/) (2026-06-13) | Control plane vs data plane, five evaluation surfaces (output, trace, memory, environment, mech-interp), minimal trace record, perturbation/ablation, paired comparison with CI + MDE, checkpoint/branch/replay, state isolation, "cargo cult evaluation". Read in full. |
| **S2** | [*How to Automate AI Evals (Correctly)*](https://www.youtube.com/watch?v=tqUDjc1HzO4), Hamel Husain channel, talk by Shreya Shankar (published 2026-07-03, 27 min) | **Not** a walkthrough of a basic runner, despite the ticket's label. It covers AI-assisted **error analysis**: the analyze→measure→improve lifecycle, three mistakes, and a live demo of a trace-review app that builds a failure-mode taxonomy. Read via the English ASR transcript (fetched with `youtube-transcript-api`) plus the video description. |
| **S3** | Hamel Husain, [*"It's Hard to Eval" Is a Product Smell*](https://hamel.dev/blog/posts/eval-smell/#example-2-the-pe-curriculum-builder) (2026-06-29) | One smell: outputs that are hard to verify. Example 2 (PE curriculum builder) says to anchor output to a vetted artifact and show a diff. The post has **no** material on judges, binary vs Likert, calibration, or open/axial coding. Read in full. |
| **S4** | Alexey Grigorev, [*How to Do Evals in 2026*](https://aishippingblog.com/p/how-to-do-evals-in-2026) (2026-08-14) | Staged workflow: log and hand-label 10–15 interactions, align a binary judge written as a `judge.md` rule file, QA-style equivalence partitions and boundary cases, synthetic inputs, real users, online judging, multiple judges later. Read in full. |
| **S5** | agentpatterns.ai, [*Eval Engineering* training module](https://agentpatterns.ai/training/foundations/eval-engineering/) | Evals measure distributions (unlike tests), pass@k vs pass^k, grade outcomes not paths, code > LLM > human grader order, calibration, 20–50 starting cases, eval-driven development, anti-gaming defences. A secondary digest; its load-bearing claims were traced to A. |
| **A** | Anthropic, [*Demystifying evals for AI agents*](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents) | The primary source S5 cites. Checked for pass@k/pass^k, outcome grading, grader validation (CORE-Bench), balanced cases, trial isolation, reference solutions, judge "Unknown" escape, per-dimension judges, and computer-use/browser guidance. |

Tags in brackets after each rule name its sources. **[inference]** marks a step I took that no source states outright.

---

## 1. What LLMs grade well vs badly

**R1. Code Graders first. An LLM judge only where code cannot decide.** [S5, A] The order is code, then LLM judge, then human label; "use the lightest method covering each case" [S5]. A lists model-based graders as "non-deterministic, more expensive than code, requires calibration with human graders". For this runner, almost every Task eval outcome is code-gradable against Fixture site server state. A's browser example is WebArena: "URL and page state checks… along with backend state verification for tasks that modify data". Examples here: was the order row written, which form fields were submitted, the final URL, whether `ask_user` preceded an irreversible action, whether a target handle came from the current observation, step count, cost.

**R2. LLMs are good at applying a criterion a human has already externalized. They are bad at finding that criterion.** [S2] Shankar says AI is "not very good at automating the early parts, the error analysis parts. These are very taste-specific", and that further along the lifecycle ("measure", "improve") it "can automate and do much more". Asked open-endedly to find issues, an agent "will miss all of the taste specific issues" and "fabricate what they think is the highest priority". So LLM judges on this runner only apply named failure modes; they never discover or rank them.

**R3. LLMs are bad at exhaustive recall and at sampling.** [S2] When back-applying a failure mode to earlier traces, the agent "is not always exhaustive… can flag instances… but can't flag all of them". LLM-chosen sampling/clustering "is very much a guess… you can't assume that the clustering or that the sample selection is perfect". Rule: prevalence counts from an LLM pass are lower bounds, and an LLM-selected review sample is not a representative sample.

**R4. Where an LLM judge is justified here.** [S1, S5, A; inference for the mapping] Use one only for failure modes with no code check. Examples: whether the `done` answer is supported by what the agent actually observed (S1's "empty tool result hallucination": "the answer is unsupported by the only evidence the agent actually observed"), whether an `ask_user` question was necessary and well formed, and whether a step's one-line note matches the action. Anything a Fixture site can answer (price, item, count) is a code Grader against seeded ground truth, not a judge.

**R5. Make outputs verifiable instead of building a smarter judge.** [S3] "Designing your product for ease of verification should come before building evals", and "the fastest way to make an output checkable is to show where each part came from". Example 2's move is to anchor to a trusted artifact and show the diff. That makes evals tractable "because there is less surface area to test". For this agent **[inference]**: the Fixture site is the trusted artifact, so Task eval Graders diff server state against the seeded expectation. A `done` answer that cites where each fact was observed (URL / observation) would make R4's faithfulness judge a mostly code check. That is a product change, raised under Open questions.

## 2. Graders and judge calibration

**R6. Every Grader is binary and bound to one named failure mode. Judges are isolated per failure mode.** [S4, A; matches map Notes] S4's judge is "the same as training a binary classifier that predicts your decisions". A says "grade each dimension with an isolated LLM-as-judge rather than using one to grade all dimensions". *Source disagreement:* S5 claims "a single comprehensive prompt with all rubric dimensions produces more consistent scores", citing Anthropic's multi-agent research post. A, the evals-specific primary source, says the opposite. Keep per-failure-mode isolation.

**R7. Judge rules live in a versioned, generic rule file. It is tested cold, from the file alone.** [S4] Loop until judge and human agree, then have the judge write `judge.md`. "Read this file carefully to see that the rules are generic and not specific to the examples". Then "start a new session… a subagent which reads judge.md and classifies the records". For this runner: each LLM-judge Grader has a checked-in rule file, versioned with the Eval suite. The calibration check runs the judge with only that file, the Trial trace excerpt, and no labeling-session context.

**R8. Calibrate against human labels, keep a held-out split, and recalibrate when the case mix changes.** [S4, S5, A] S5: "score a sample set with both the judge and human reviewers using the same rubric… recalibrate when new query types enter the distribution". S4 says to use a train-test split once there is enough data. For review, S4 found it easier to mark agree/disagree against the judge's verdict than to relabel good/bad. Rule: a judge Grader is marked *trusted* only after it is checked against human labels it was not tuned on. It returns to *untrusted* when an Eval suite version adds a new fixture family or failure mode it covers.

**R9. Give the judge an explicit "Unknown" exit. Unknown is not a pass.** [A] "Providing an instruction to return Unknown when it doesn't have enough information." **[inference]** For Comparisons, report Unknown separately, never folded into pass or fail. A rising Unknown rate means the trace excerpt given to the judge is missing evidence.

**R10. No source gives a numeric agreement threshold.** S4 settles for "the judge should be correct most of the time". S5 and A say "calibrate closely". None names TPR/TNR targets, minimum label counts, or a statistic. **[inference]** Measure agreement separately on human-pass and human-fail labels, since a judge that always says "pass" looks accurate on a mostly-pass suite. The threshold itself is a decision for the calibration ticket (see Open questions).

**R11. Validate code Graders too. Every Task eval case needs a reference solution.** [A, S5] "Create a reference solution: a known working output that passes all graders. This proves that the task is solvable and verifies graders are correctly configured" [A]. CORE-Bench went from 42% to 95% after grader and task-spec fixes [A, S5]. "A 0% pass rate across many trials… is most often a signal of a broken task, not an incapable agent" [A]. Rule: a scripted reference Trial (deterministic CDP actions, no model) must pass every Grader of a Task eval case before the case joins an Eval suite. A case at 0% under both Variants across many Trials gets a case review before anyone blames the agent.

**R12. Sample passing Trials for human review, not only failures.** [S4, A] "Also sample the good results to make sure they are actually good" [S4]. "You won't know if your graders are working well unless you read the transcripts and grades from many trials" [A].

## 3. Error-analysis workflow

**R13. Use the lifecycle analyze → measure → improve, iteratively.** [S2] Error analysis finds failure modes in Trial traces. Measurement counts each mode's prevalence. Improvement changes the product (prompt, model, and so on). "80% of issues… are often caused by 20% of failure modes", so measure prevalence and fix the top modes first. The video description adds: "A common mistake is writing a rubric up front before looking at any data." (Matches map Notes: error-analysis-first.)

**R14. The human writes free-form notes in place. The assistant builds the taxonomy, and the human owns it.** [S2] Shankar highlights the offending span and writes a note ("I don't like having colons…"). An agent watches the annotations and groups them into a failure-mode taxonomy shown in a progress view. She deliberately does *not* let the agent add taxonomy categories: "It's not adding to the taxonomy. That's really left to me… I don't love the experience of just trying to validate the agent's taste." *Terminology note:* the talk never says "open coding" or "axial coding". Free-form notes followed by an agent-grouped taxonomy is the same workflow under different names. The rule here: the human does the open coding, and the assistant proposes groupings plus back-applied instances that the human accepts or rejects.

**R15. Back-apply each new failure mode to traces already reviewed, and make several passes.** [S2] "When you go back and you look at traces again… you actually end up find new failure modes". Her example was noticing an overused word only by the third essay. She has the agent re-scan earlier traces for each new mode and suggest instances to accept or reject. The framing is "an outer loop of iterating over the data, and… an inner loop of… hypothesis iterating through different failure modes". Aim for breadth (cover all modes) and depth (several examples per mode).

**R16. Persist taxonomy, definitions, and labels as files. Never re-run a fresh "find the issues".** [S2] Asking an agent to "check out my traces… evaluate the app" has "no reuse… nothing that's being shared across these different runs". "What we really want… is to persist intermediates of the life cycle." For this runner: the failure-mode taxonomy, per-mode definitions, and human labels are checked-in data keyed to Trial ids, and they are what Grader definitions reference.

**R17. Build a purpose-made trace review view. Humans hand-label the seed set.** [S2, S4] S2's review app has a one-by-one trace view, a map/cluster view, and a progress view. S4 "vibe-code[s] a small labelling tool" and says of the first 10–15 labels: "Don't delegate this step to coding assistants." **[inference]** For a browser agent the review view must show, per step, the rendered observation the model saw, the screenshot (if vision was on), the tool call, the action result/error text, and the state delta. Otherwise reviewers grade the final answer only, which S1 warns against.

**R18. Seed small from real failures, then grow deliberately.** [S4, S5, A] S4 starts at 10–15 logged interactions. A says "20–50 simple tasks drawn from real failures is a great start… large effect size means small sample sizes suffice". This supports the map's small hand-seeded bootstrap.

**R19. Build cases with QA partitioning, and keep them balanced.** [S4, A] Take the equivalence partitions of the input space, 2–3 cases each, then boundary cases [S4]. "Test both the cases where a behavior should occur and where it shouldn't… one-sided evals create one-sided optimization" [A]. **[inference]** Fixture families for this agent map onto partitions already named in `prompts.SYSTEM`: blocking consent popup vs task-relevant dialog, disabled control that must be enabled first, native `select` vs custom dropdown, OTP digit boxes, off-viewport targets, validation errors, verification challenges. Every "should call `ask_user`" case needs a sibling "should decide alone" case, or the Variant that asks everything wins.

**R20. Set the accuracy bar from worst cases, and make those P0 cases whatever their frequency.** [S2, S5] Reason about "worst-case scenarios up front" to set per-application rigor [S2]. S5 says "Always create P0 evals for security/safety violations regardless of frequency." Here the worst cases are placing an order or paying without confirmation, inventing an address or payment detail, submitting credentials, and acting on a stale target handle. They get Task eval cases on day one, beside the prevalence-driven ones.

## 4. Trace-first infrastructure

**R21. A Trial's trace is a structured per-step record, not a console log.** [S1] "A usable trace is a structured record of every step: the tool called, the arguments passed, the observation returned, the latency and cost, and the state delta… Without that structure the process questions above are unanswerable." S1's minimal record, mapped to this repo:

| S1 field | Eval runner field |
|---|---|
| run id / task id | Trial id / Eval case id (+ Eval suite version) |
| step index | step |
| model and harness version | resolved model id + Variant id + git SHA |
| prompt/config hash | Variant content hash (prompts, tool schemas, renderer, guards) |
| tool name, arguments hash | tool call name + args |
| observation hash | hash of rendered observation text (+ screenshot ref) |
| latency, cost | per-step latency, input/output tokens, cost |
| permission boundary | confirmation / `ask_user` gating state (**[inference]**) |
| state delta pointer, checkpoint id | existing **State delta** / **Artifact** refs and **Checkpoint** id under `AGENT_STATE_ROOT` |
| verifier result, failure labels | Grader verdicts, human labels |

"Anything less becomes hard to replay, compare, or audit."

**R22. Persist the full model message history per Trial.** [S1, A] A defines the trace as "the complete record of a trial, including outputs, tool calls, reasoning, intermediate results". The map's known fact that "model message history is never persisted" blocks this directly. So do Step eval capture (frozen observation + history) and R17's review view. **This is a prerequisite, not an option.**

**R23. Grade process invariants, not a golden path.** [S1, S5, A] "Traces should not be scored primarily on whether they follow one golden path… The stronger target is process invariants… like 'no unsafe writes'" [S1]. Checking "a sequence of tool calls in the right order" makes "overly brittle tests, as agents regularly find valid approaches" [A]. Rule: trace-level Graders assert invariants. Examples: no click on a `(disabled)` control, no action on a handle from a superseded observation, no irreversible action without confirmation, no close/reopen loop, no answer fact absent from every observation. Outcome Graders assert end state.

**R24. Capture state per step so a failure can be attributed to a step.** [S1] "Outcome-level eval tells you the task succeeded or failed; the sequence of state deltas tells you which step caused it." The repo already records per-action **State deltas**, and the runner should link them into the trace rather than reinvent them.

**R25. Memory is an evaluation surface.** [S1] "Memory pollution changes agent behavior silently… One bad episode becomes a durable preference." S1 asks "What memory did the agent consult? What did it update? Was the update justified?", and whether the task can be replayed with memory "disabled, stale… clean… and polluted". This agent has `remember`/`forget` user memory (`browser_agent/memory.py`) injected into every task message. Rule: each Trial starts from a declared memory state, and `remember`/`forget` calls are recorded and gradable. The map Notes are silent on this (see Additions).

## 5. Hidden infrastructure debt

**R26. Trials never share mutable state.** [S1, A] "Experimentation branching must not share mutable state unless sharing is explicitly designed" [S1]. "Each trial should be isolated by starting from a clean environment… shared state between runs… can cause correlated failures due to infrastructure flakiness" [A]. S1 lists what browser agents need: "profiles, cookies, local storage, network recordings, and DOM snapshots". Rule: every Trial gets a fresh **Episode** (or one restored from a named **Checkpoint**), a freshly seeded Fixture site instance, and a declared user-memory state. Concurrent Trials never share one Fixture site database.

**R27. Store everything needed to re-run a Comparison months later.** [S1] "All of these runs, including state, configs, should be stored so we can re-run the experiment months after for the same results." S1's debt list reads like a warning for this repo: task lists in CSVs, judge prompts in a spreadsheet, "the model is whatever the default API of the week behind some model router", "an ever changing tools schema", and a dashboard number "nobody can trace back to the run that produced it". Rule: version and record the Eval suite, Fixture site seed/build, Variant content, Grader and judge rule files, the judge model, and the **resolved** model id. `AGENT_MODEL` may be an alias, so the recorded value is what the API reported.

**R28. The Eval runner drives the production agent code path.** [S1] Customers "report that the agent got worse, and the team cannot reproduce the regression because production ran with a different memory state, tool timeout, browser profile, and system prompt than the eval ever saw." Rule: the runner is a **Harness** calling the same `Agent`. Variant overrides go through a seam in that code, not a forked copy of the loop.

**R29. Separate agent failure from infrastructure and Grader failure.** [S1, A] A durable layer must "distinguish the agent failing the task from the scorer failing the agent" [S1]. A warns of correlated failures "due to infrastructure flakiness rather than agent performance". **[inference]** Each Trial ends with an explicit status: graded, infra error (Chrome crash, CDP timeout, Fixture site 5xx, provider error), or Grader error. Infra-error Trials are excluded from pass rates, counted, and shown in the Comparison report. They are not silently scored as agent failures.

**R30. Log cost per Trial. Size N from the effect you need to detect.** [S1, A, S5] S1's cost row covers "inference, tools, runtime, and human review". Large effects need few cases ("a 30% to 80% improvement from a prompt change") [S5, A]. **[inference]** With the map's ≈$20 per Comparison ceiling, a Comparison can reliably detect large, per-case flips only. Hence R32's MDE: small deltas under this budget are noise until proven otherwise. Step evals are the cheap instrument, since they skip the browser and most model turns, which is also S1's argument for branching from checkpoints ("re-running from the beginning is too expensive").

## 6. Comparison design

**R31. Change one thing per Comparison.** [S1] "Treat every change as a hypothesis… Change one variable, hold the rest fixed." Otherwise "a model change and a harness change land in the same number and you cannot tell which one moved it". Declarative Variant overrides satisfy this by construction. **[inference]** A git-ref Comparison can bundle several changes, so the report should show the diff between the two refs and warn when it touches more than one Variant axis.

**R32. Report a Comparison as a measurement: paired, per case, with uncertainty and a minimum detectable effect.** [S1] "Pair the runs and report the uncertainty with a confidence interval and a minimum detectable effect alongside every number." The useful question is "which component did we change that caused which tasks flipped and in which direction". "Simpson's paradox applies." Slice by failure mode and fixture family [S1]. (The map already settles paired and per-case; CI + MDE is the addition.)

**R33. Report both pass@k and pass^k per Eval case.** [S5, A] pass@k is "at least one correct solution in k attempts". pass^k is "all k trials succeed"; use pass^k "for agents where consistency is essential" [A]. **[inference]** This agent acts unattended on real sites, so pass^k is the headline for safety-relevant cases. A case whose flip count sits inside its own Trial-to-Trial variance is not a flip.

**R34. Run ablations and perturbations, not just baseline vs candidate.** [S1] Ablations "remove one component at a time", and S1 names "browser access" as one. If removing a component "barely moves the score, the… layer is theater". Perturbations hold the task fixed and vary the environment: "randomly fail a tool… inject tool errors, increase response latency, revoke a permission". Toggling supervisor and loop guards (map Notes) is exactly an ablation. The addition is perturbation cases at the CDP layer (see §8).

**R35. Watch for saturation.** [A] When an agent "passes all of the solvable tasks… large capability improvements appear as small increases in scores". **[inference]** A case that passes every Trial under both Variants adds cost and no Comparison signal. Flag such cases, and retire or harden them per Eval suite version.

## 7. Eval smells to avoid

| Smell | Source | Rule for this runner |
|---|---|---|
| "It's hard to eval": the output can't be checked without redoing the work | S3 | Make the output verifiable (R5). The Fixture site is the trusted comparison. |
| Asking a coding agent to "evaluate my traces" from scratch | S2 | Human open coding plus persisted taxonomy (R14, R16). |
| Writing a rubric before looking at data | S2 (description) | Error-analysis first (R13). |
| Reviewing each trace once | S2 | Multiple passes, back-application (R15). |
| One accuracy bar for everything | S2 | Worst-case P0 cases (R20). |
| Golden-path grading of tool sequences | S1, S5, A | Process invariants + outcome state (R23). |
| One-sided cases (only "should do X") | A, S5 | Paired should/shouldn't cases (R19). |
| Untrusted judge, or unvalidated code Grader | A, S5 | Calibration (R8) and reference solutions (R11). |
| **Cargo-cult evaluation**: "someone adds a few tasks to the golden set, the average ticks back up" | S1 | Only compare Trials on the identical Eval suite version. Never compare pass rates across suite versions. |
| A single aggregate number nobody can trace back to Trials | S1 | Every reported number links to its Trials (R21, R27). |
| Shared state across Trials | S1, A | Isolation (R26). |
| Eval runtime drifting from the product | S1 | Same code path (R28). |
| Encoding the agent's current (buggy) behavior as "correct" | S5, A | Expected behavior is written by a human from task intent, never copied from a Trial's output. |
| A 0% case blamed on the agent | A | Review the case first (R11). |

## 8. Does the LLM↔CDP layer need evals?

**Short answer: yes for its model-facing surface, no for its deterministic mechanics.** This matches the map Notes. The sources add two things.

- **Tests vs evals split.** S5 separates harness engineering ("catches mistakes during a single execution") from eval engineering ("measuring agent quality across sessions and over time"). It says evals exist because "agent evals measure a distribution". The deterministic CDP layer (target resolution, frame handling, tab adoption, checkpointing) has no distribution. Ordinary tests fit it, and `tests/test_nodriver_*.py` / `test_browser_contract.py` already cover it. Treating it as an eval would add noise and cost for no signal.
- **The model-facing surface is an evaluation surface.** S1: "A tool call can be syntactically valid but semantically useless", and the environment and trace surfaces must be captured because "for most builders… your extrinsic trace and state instrumentation have to be comprehensive". For a browser agent that surface is `render_state` (what the model sees), tool schemas, `render_action_result` and error text, truncation, and the screenshot. A Variant that changes any of these changes model behavior, so it belongs in Step evals and Comparisons (map Notes, confirmed).
- **Addition 1: tool-choice evals.** A says computer-use agents trade off "DOM-based interactions [that] execute quickly but consume many tokens" against slower, cheaper screenshot interactions, and evals should verify "the agent was selecting the right tool for each context". Here that means Step eval cases for index-click vs `click_at`, `get_html` escalation, `fill_form` vs repeated `type`, and vision on vs off as a Variant axis.
- **Addition 2: perturbation at the boundary.** S1's "randomly fail a tool… inject tool errors" applied at the CDP boundary gives cases where an action fails, times out, hits a stale target handle, or returns an empty `read_page`. Grade whether the model recovers from the error text or fabricates. This is the empty-tool-result hallucination case in browser form, and it evaluates the *error-text wording* as a Variant axis.
- **Observation fidelity checks** (map Notes) are code Graders in S1's sense: the Fixture site knows which controls and context exist, so a Grader can assert that `render_state` surfaced them. Their failures are renderer bugs to fix with tests. They need no judge.

## 9. Conflicts with and additions to the map (#24) Notes

**Conflicts / refinements**

1. **Step evals vs "grade outcomes, not paths".** The map says a Step eval judges "the model's next tool call". S1, S5, and A all warn against golden-path grading. *Refinement:* Step eval Graders must accept **any acceptable next call**, either a set of acceptable calls or invariant violations ("clicked a disabled control", "typed into a `combobox` with `options=[…]`"). An exact match to one golden call is not a Grader. Not a contradiction, but the case format must support it.
2. **Error-analysis-first vs eval-driven development.** S5/A: "Write evals before building features… [otherwise you] reverse-engineer success criteria from a live system". The map says failure modes come from real traces. *Resolution:* no real conflict. Traces decide *which* failure modes get cases, and a human writes each case's expected behavior from task intent (never copied from Trial output). The first Comparison target already follows this: expected direction known in advance.
3. **Synthetic inputs.** S4 recommends generating inputs synthetically for volume. The map scopes out LLM-simulated *users* and prefers a hand-seeded bootstrap. Synthetic *task text* over Fixture sites is a different thing and is not excluded. It needs a decision, not a silent default.
4. **Single judge vs isolated judges.** S5 conflicts with A. The map's "binary per named failure mode" sides with A. No change, recorded so it isn't re-litigated.

**Additions (the Notes don't state these)**

5. **Persist full model message history per Trial** (R22). This is a prerequisite for Step eval capture, review, and judges, and it reverses a current code fact.
6. **Comparison reports carry CI + MDE, pass@k and pass^k, and an infra-error count** (R29, R32, R33). Comparisons are only valid on an identical Eval suite version (the cargo-cult smell).
7. **Reference solution per Task eval case**, and a 0% case gets reviewed before the agent is blamed (R11).
8. **User memory is a controlled Trial input and a graded surface** (R25). The map lists Variant axes but not memory state.
9. **Trial isolation spec**: fresh Episode or named Checkpoint, freshly seeded Fixture site, declared memory, no shared fixture DB across concurrent Trials (R26).
10. **Record the resolved model id**, not just `AGENT_MODEL`, plus Variant content hash and git SHA (R27).
11. **A trace review view** for error analysis (R17). The map assumes error analysis but doesn't say where it happens.
12. **Balanced should/shouldn't `ask_user` cases** and worst-case P0 cases (R19, R20).
13. **CDP-boundary perturbation cases and tool-choice Step evals** (§8).
14. **Judge "Unknown" verdict** reported separately (R9).

## 10. Open questions (candidate tickets)

- **Judge trust threshold.** Which agreement statistic (separate agreement on human-pass and human-fail labels?), how many held-out human labels, and what threshold before a judge Grader is trusted? All sources are silent (R10). Fits the map's "Judge model choice" gap.
- **Trial status taxonomy and retry policy.** Which errors count as infra vs agent failure, and whether infra-error Trials are retried within the $20 budget (R29).
- **Memory in Trials.** Is user memory a Variant axis, an Eval case input, or both? How are `remember` writes graded (R25)?
- **Statistics for small paired binary samples.** Which CI method and MDE to report given the $20 ceiling and N Trials (R32).
- **Trace review tooling scope.** Is a review/labelling view part of v0, or do the first error-analysis rounds use raw trace files (R17)?
- **Verifiable `done` output.** Should `done` return structured answer fields with observation provenance, making answer-faithfulness code-gradable (R5)? That is a product change outside the runner.
- **Perturbation cases.** How the runner injects CDP-layer failures (fake action errors, stale handles, latency) without forking the browser layer (§8).
- **Saturation and case retirement.** A rule for when an always-passing case leaves an Eval suite version (R35).
- **Synthetic task generation.** Allowed for Fixture site task text or not (conflict 3)?

## Not covered by the sources

- None of the five sources addresses browser agents specifically. Only A (reached via S5) mentions computer-use/browser evaluation. The CDP mappings above are marked **[inference]** where they go beyond the text.
- "Open coding" and "axial coding" appear in none of the five sources. S2 describes the equivalent workflow without those names.
- S2 was reached through YouTube's auto-generated English captions, so short quotes from it may carry ASR transcription errors (e.g. "emails" for "evals").
