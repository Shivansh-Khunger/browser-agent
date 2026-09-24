# Comparison statistics: sizing and decision rule

Research for [#26](https://github.com/shreverr/browser-agent/issues/26) (map
[#24](https://github.com/shreverr/browser-agent/issues/24)). Written 2026-09-24.
Vocabulary follows `CONTEXT.md` → Evaluation: Eval case, Eval suite, Step eval,
Task eval, Variant, Trial, Grader, Comparison.

## Decision

| | Step evals | Task evals |
|---|---|---|
| Eval cases | 60 | 30 |
| Trials per case per Variant (K) | 5 | 5 |
| Trials per Comparison (2 Variants) | 600 | 300 |
| Estimated cost on `qwen/qwen3.7-flash` | ≈ $0.30 | ≈ $3 typical, ≤ $5 with a 25-step cap |
| Smallest mean pass-rate change detected at 80% power | ≤ 10 pp (spread across cases) to ≈ 15 pp (mixed up/down per case) | ≈ 13 pp (spread across cases) to ≈ 20 pp (mixed) |

- **Unit of analysis is the Eval case, not the Trial.** For each case take the
  per-Variant pass rate `p̂ = c/K`, then the paired difference
  `dᵢ = p̂_candidate,ᵢ − p̂_baseline,ᵢ`. The headline number is `Δ = mean(dᵢ)`.
- **Test:** a two-sided paired sign-flip permutation test on the `dᵢ`: exact
  enumeration when N ≤ 20, otherwise 100k Monte Carlo draws. Report a 95% CI from
  a paired bootstrap over cases (10k resamples). At K = 1 this test is exactly the
  exact McNemar test. For K > 1 it is the case-clustered version.
- **Do not** pool Trials into one McNemar table. When a change helps some cases and
  hurts others, the pooled test gave false positives 12–35% of the time at K = 3–8,
  against a nominal 5% (simulation below).
- **Decision rule:** `better` if p < 0.05 and Δ > 0; `worse` if p < 0.05 and Δ < 0;
  otherwise `no detectable difference`, always printed with Δ, its CI and the
  detectable-effect size for this N × K. Any case flagged `broken` (below) turns
  `better` into `better, with regressions`, which needs a person to read the traces.
- **Sampling policy:** Trials use the Variant's real request parameters. Today
  that means the same kwargs as production `Agent._complete`, which sends no
  `temperature` or `seed`. Never force temperature 0 to "remove noise". Send a
  different recorded `seed` per Trial index, pin the provider with no fallbacks,
  and interleave baseline and candidate Trials in time.
- **Primary metric is pass^1 (the mean per-case pass rate).** Report pass^K per
  Variant as a secondary reliability number. Do not report pass@k in v0.

## Why these sizes

### Budget is not the constraint for the current model; wall-clock is

`AGENT_MODEL` in the local `.env` is `qwen/qwen3.7-flash`. The code default in
`browser_agent/config.py` is `anthropic/claude-opus-4.8`. OpenRouter's models API
(`/api/v1/models/qwen/qwen3.7-flash/endpoints`, fetched 2026-09-24) lists:

- one provider, Alibaba;
- $0.03 per M prompt tokens and $0.13 per M completion tokens below 32k prompt
  tokens;
- $0.10 / $0.40 per M from 32k to 256k prompt tokens;
- support for `temperature`, `top_p`, `seed` and `reasoning`.

Opus 4.8 is $5 per M input and $25 per M output.

Per-Trial estimate. Every one of these inputs is an assumption to replace with the
`usage.cost` that OpenRouter returns on every response
([usage accounting](https://openrouter.ai/docs/guides/guides/usage-accounting)).

- Fixed prompt: `prompts.SYSTEM` + `VISION_NOTE` (≈ 7.6k chars) plus `TOOLS`
  schemas (≈ 6.1k chars) ≈ 3.5k tokens at about 4 chars/token.
- Latest Observation: up to 150 controls and 250 context nodes (`ObservationLimits`).
  Assume ≈ 2.5k tokens on a Fixture site page.
- Screenshot: vision is on by default. `_compact_history` keeps only the newest
  image. Assume ≈ 1.2k tokens.
- History: compaction strips older page text, HTML and images, but keeps assistant
  turns and action results. Assume ≈ 0.5k tokens per earlier step.
- Output: the `max_tokens=4000` ceiling. Assume 1.5k on average, including reasoning.
- Only the system block carries `cache_control`, and only for Anthropic models, so
  no history caching is assumed.

| Trial | Prompt tokens (sum) | qwen3.7-flash | Opus 4.8 |
|---|---|---|---|
| Step eval (1 call, ≈ 10k in / 1.5k out) | 10k | ≈ $0.0005 | ≈ $0.09 |
| Task eval, 15 steps | ≈ 190k | ≈ $0.009 | ≈ $1.5 |
| Task eval, 25-step cap | ≈ 375k | ≈ $0.017 | ≈ $3 |
| Task eval, 50 steps (`MAX_STEPS` default) | ≈ 1.06M (late calls > 32k) | ≈ $0.05 | ≈ $7+ |

The default Comparison costs about $3.5 on qwen3.7-flash, and even the worst case
(every Task trial hits a 50-step cap) stays under $20. On Opus 4.8 the same $20
buys about 13 Task trials, which is not enough for any paired Comparison. The
budget in the map only works because the model under test is a flash-tier model.

The real limit is time. At an assumed 1–2 min per Task trial, 300 Task trials
run one after another take 5–10 h.

### Trials per case: K = 5

Miller splits the variance of a case-mean score into between-case variance and
within-case variance: `Var(μ̂) = (Var(x) + E[σᵢ²]/K) / n`. For binary Graders,
`σᵢ² = pᵢ(1 − pᵢ)`. "Once E[σᵢ²]/K ≪ Var(x), increasing K further will have
little effect". In his binary example, going from K = 1 to K = 2, 4 and 6 cuts
variance by 1/3, 1/2 and 5/9, with a ceiling of 2/3
([Miller 2024, §3.1](https://arxiv.org/html/2411.00640)). The power formula is
`n = (z_{α/2} + z_β)² (ω² + σ²_A/K_A + σ²_B/K_B) / δ²` (§5). Adding cases shrinks
the ω² (case-difference) term, which trials cannot touch.

Practice lines up with this:

- τ-bench runs "at least 3 trials per task" ([Yao et al. 2024](https://arxiv.org/html/2406.12045)).
- τ²-bench runs "each task … four times" ([Barres et al. 2025](https://arxiv.org/html/2506.07982)).
- Anthropic's agent-evals guide calls "20–50 simple tasks drawn from real failures"
  "a great start" and runs multiple Trials "because model outputs vary between
  runs" ([Anthropic, Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)).

### Power simulation

This is our own Monte Carlo simulation, not a published result. Setup:

- 400 replications per cell, α = 0.05 two-sided.
- Per-case baseline pass probabilities come from a mix of easy, flaky and hard cases:
  - Task evals: 35% Beta(9,1), 35% Beta(2,2), 30% Beta(1,9).
  - Step evals: 45% Beta(12,1), 25% Beta(2,2), 30% Beta(1,12). Step evals are
    assumed to be more decisive.
- Three effect shapes:
  - **diffuse:** a logit shift on every case;
  - **concentrated:** a few cases jump to 0.9 and the rest stay put;
  - **mixed:** half the cases get a fresh random pass probability, so the average
    effect is 0 (plus any added diffuse shift).

Power of the case-level test (false-positive rate in the Δ = 0 column):

| Kind | N × K | Δ=0 | 10 pp | 15 pp | 20 pp | shape |
|---|---|---|---|---|---|---|
| task | 30 × 3 | 0.04 | 0.47 | 0.80 | 0.96 | diffuse |
| task | 30 × 5 | 0.04 | 0.65 | 0.94 | 1.00 | diffuse |
| task | 30 × 8 | 0.05 | 0.84 | 1.00 | 1.00 | diffuse |
| task | 30 × 5 | 0.02* | 0.33 | 0.64 | 0.92 | concentrated |
| task | 30 × 5 | 0.07 | 0.23 | 0.49 | 0.80 | mixed |
| task | 20 × 5 | 0.04 | 0.46 | 0.79 | 0.97 | diffuse |
| task | 40 × 5 | 0.04 | 0.75 | 0.99 | 1.00 | diffuse |
| step | 60 × 5 | 0.05 | 0.98 | 1.00 | 1.00 | diffuse |
| step | 60 × 5 | 0.03* | 0.76 | 0.98 | 1.00 | concentrated |
| step | 60 × 5 | 0.04 | 0.45 | 0.78 | 0.97 | mixed |

\* Sign-flip permutation test. The t-test on `dᵢ` ran slightly above 5% (0.04–0.07)
at small N when many `dᵢ` are 0. That is why the permutation test is the default.

Under the mixed shape, pooled trial-level McNemar gave false positives of
0.11 / 0.19 / 0.25 at task 20 × {3, 5, 8}, and 0.12 / 0.21 / 0.30 at step 60 ×
{3, 5, 8}. The case-level test stayed at 0.04–0.08. The pooled test answers "did
these exact cases change?", not "will cases like these improve?". Miller makes the
same point with clustered standard errors, which "can be over three times as large
as naive standard errors"
([Anthropic summary](https://www.anthropic.com/research/statistical-approach-to-model-evals)).
McNemar itself is sound for a single run per item
([Dietterich 1998](https://direct.mit.edu/neco/article-abstract/10/7/1895/6224/Approximate-Statistical-Tests-for-Comparing);
exact binomial form in
[statsmodels](https://www.statsmodels.org/stable/generated/statsmodels.stats.contingency_tables.mcnemar.html)),
which is why the sign-flip test reduces to it at K = 1.

Takeaways:

- Going from K = 5 to K = 8 gains less than adding 10 cases.
- Below about 20 Task-eval cases, a Comparison can only detect large, known-direction
  changes, such as the first-target prompt fix ("Firefox", India defaults).
- Step evals are nearly free, so cases can grow past 60 as they are authored.
  Authoring effort limits them, not dollars.

## pass@k vs pass^k

- **pass@k**: the chance that at least one of k Trials succeeds. Chen et al.
  estimate it without bias from n ≥ k samples as `1 − C(n−c,k)/C(n,k)`, noting
  that `1 − (1 − p̂)^k` is biased ([Codex paper §2.1](https://arxiv.org/abs/2107.03374)).
- **pass^k**: the chance that all k Trials succeed, `E_case[C(c,k)/C(n,k)]`. τ-bench
  found that "even for the best-performing gpt-4o … agent which has a >60% average
  task success, pass^8 drops to <25%" ([τ-bench](https://arxiv.org/html/2406.12045)).

Our agent gets one attempt per user task. It has no retry-and-verify loop, so
pass@k would overstate what a user experiences. Report three things:

1. **pass^1 per Variant and its Δ.** This is the decision metric.
2. **pass^K per Variant**, the share of cases where all 5 Trials passed, as a
   reliability view. It is noisier and not a decision input in v0.
3. No pass@k.

## Reporting rule

Every Comparison report, for step evals and task evals separately:

1. **Aggregate.** For each Variant: pass^1 with a case-clustered SE, and pass^5.
   Then Δ pass^1, its 95% bootstrap CI, the sign-flip p, the verdict, and the
   detectable effect at 80% power for this N × K, taken from the table above or
   recomputed. Add cost and tokens per Variant from `usage.cost`, because a cheaper
   Variant at equal pass rate counts as better.
2. **Per case,** sorted by `c_candidate − c_baseline`:
   `case | baseline c/5 | candidate c/5 | flag`. The flags at K = 5 come from a
   per-case Fisher exact test at p < 0.05, which means a change of 4 or more of 5
   (for example 0→4, 1→5):
   - `fixed` / `broken`: a change of 4 or more of 5.
   - `drift`: a change of 2–3 of 5, which is noise-level.
   - `same`: anything smaller.
3. **Per Grader** (named failure mode): failure rate per Variant, descriptive only.
   If a decision rests on more than one Grader or Eval suite, apply Holm-Bonferroni
   across them.

Per-case flags point to traces worth reading. They are not the verdict. In our
null simulation (30 Task-eval cases, K = 5, identical Variants), about 4.8 cases
per Comparison showed `drift`, and 15% of Comparisons showed at least one `fixed`
or `broken` flag by chance. Reading a per-case table without the aggregate test
invites chasing noise.

Integrity rules:

- **Fixed N × K, no stopping early.** Do not stop because p dipped below 0.05. An
  inconclusive result stays inconclusive. Follow up with a new Comparison, and
  prefer more cases over more trials.
- **Errored is not failed.** A Trial whose infrastructure broke (Chrome crash,
  provider 5xx, Fixture site down) is retried up to 2 times. If it still errors, drop
  the whole case from both Variants to keep the pairs balanced, and report the
  number dropped.
- **Isolation.** Each Task trial starts in a fresh Episode from the same checkpoint
  against a freshly seeded Fixture site. Leftover shared state "can cause correlated
  failures due to infrastructure flakiness rather than agent performance"
  ([Anthropic agent-evals guide](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)).
- **Budget guard.** The runner adds up `usage.cost` and stops at $20. Because Trials
  are interleaved, stopping leaves complete pairs. Analyse only cases that have K
  Trials in both Variants.

## Temperature and seed policy

- **Sampling parameters belong to the Variant.** The default Variant sends exactly
  what production sends. `Agent._complete` sets no `temperature`, `top_p` or
  `seed`. OpenRouter "omits it upstream … so the provider applies its own default"
  ([parameters](https://openrouter.ai/docs/api/reference/parameters)). Store the
  effective parameters with each Trial. A temperature is a Variant axis only when
  a Comparison deliberately sweeps it.
- **Do not use temperature 0 to cut noise.** There are three reasons:
  1. It is not deterministic on shared inference. 1,000 completions of Qwen3-235B
     at T = 0 gave 80 distinct outputs, because batch size varies with server load
     ([He, Thinking Machines, 2025](https://thinkingmachines.ai/blog/defeating-nondeterminism-in-llm-inference/)).
  2. Qwen's own guidance for thinking mode is "DO NOT use greedy decoding, as it can
     lead to performance degradation and endless repetitions"
     ([Qwen3 model card](https://huggingface.co/Qwen/Qwen3-8B)).
  3. It would measure a Variant that production never runs.

  τ-bench and τ²-bench do use T = 0 for the agent. They still run 3–4 Trials, which
  shows that T = 0 does not remove the need for repeats.
- **Seed.** Send `seed = hash(suite, case, trial_index)`: different across a case's
  Trials, the same for baseline and candidate at the same index, and recorded.
  OpenRouter says "determinism is not guaranteed for some models", so the analysis
  never depends on seeds. They exist to help replay a Trial. A seed that is the
  same for every Trial of a case could collapse the K Trials into one if the
  provider were deterministic.
- **Provider pinning.** Send
  `provider: {order: [<served provider>], allow_fallbacks: false, require_parameters: true}`.
  By default, providers that don't support every parameter "will ignore unknown
  parameters", and requests are load-balanced across providers
  ([provider selection](https://openrouter.ai/docs/guides/routing/provider-selection)).
  qwen3.7-flash has a single provider (Alibaba) today. Pinning keeps both Variants on
  the same backend if another provider is added. Record the serving provider per Trial.
- **Interleave.** Run the baseline and candidate Trials for each case alternately in
  the same session window, so drift in provider load or silent model updates hits
  both Variants equally.

## Open questions surfaced

- **Task-trial concurrency.** Wall-clock (5–10 h for 300 Task trials in sequence)
  is the binding constraint, not dollars. The runner needs parallel Episodes and
  Fixture-site instances, or a smaller Task-eval default.
- **Reasoning settings.** It is unknown whether qwen3.7-flash reasons by default,
  and how many output tokens that adds. Should `reasoning` effort be pinned
  explicitly in the Variant?
- **Primary Grader.** Which Grader outcome is "pass" for pass^1 when an Eval case
  has several Graders: all Graders pass, or one designated primary Grader?
- **Eval step cap.** Should task evals cap `MAX_STEPS` below the production default
  of 50 (25 is assumed above)? The cap is itself model-facing behaviour, so it
  would have to be the same in both Variants.
- **Replacing the estimates.** The first real Comparison should replace the token
  estimates above with measured `usage.cost`.
