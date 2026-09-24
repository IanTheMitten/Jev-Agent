# Technical spec - Jev replacement of cost-heavy decision layers

> Status: ready for planning. Feed to /autopilot-plan once ready - it verifies this line and refuses a spec that isn't "ready for planning".

## Source

No PRD. Raw goal as stated by the user: *"I want to replace all the cost heavy decision layers with Jev."* The decision-layer inventory was produced in-session by auditing every `call_llm(task=...)` site in the repo. This spec covers Tier A and Tier B — nine sites:

- **Tier A** — four sites whose LLM output is already a fixed label or score: approval guard, cron urgency monitor, goal judge, kanban estimator.
- **Tier B** — five sites where a blind heuristic gates expensive downstream work: the background memory/skill review gate, three points in the Honcho memory path, and the curator review fork.

Tier C (per-turn model routing, MoA engage/skip) and the explicitly-ruled-out layers remain out of scope and stay in the session inventory.

### Tier B corrections made during the audit

The original Tier B inventory overstated two items. Both corrections are load-bearing for the design below.

- **The Honcho recall path is already well gated.** The claim that "every turn pays a rewrite call plus a dialectic call" was wrong on both halves. `rewrite_memory_query` fires *inside* `_run_dialectic_depth` (`plugins/memory/honcho/__init__.py:1153`), so it is already gated by everything that gates dialectic — and dialectic has five free gates ahead of it (`:892-933`): tools-mode short-circuit, trivial-prompt regex, context cadence, thread-liveness with stale recovery, and a cadence counter widened by an empty-streak backoff. The remaining opportunity is narrow and specific: the cadence gate is a *turn counter*, blind to whether the current turn would benefit.
- **The curator fork is off by default.** `curator.consolidate` defaults to false (`agent/curator.py:74`), and `run_curator_review` returns before spawning anything when it is off (`:1598`). The site is only a cost layer for users who opted in — though for those users the fork is a full `AIAgent` on the main model, so the saving where it applies is large.

The audit also found one site that was **not** in the original inventory: `_apply_reasoning_heuristic` (`plugins/memory/honcho/__init__.py:1042`) selects Honcho's reasoning level by raw character count (`+1 at >=120 chars, +2 at >=400`), and that level drives billed compute on every dialectic pass.

Ruled out on re-examination: `is_trivial_prompt` (`agent/memory_provider.py`, used at `:900`). It is a free regex that already catches the cheap wins; replacing it with a network call is a cost regression, not a saving.

### Constraint set

Every decision below traces to one of these:

1. **C1 — Jev emits no text.** `POST https://api.typesafe.ai/v1/systemone` answers only `noul` (yes/no probability), `choice` (label + confidence + per-label probabilities), or `score` (ordinal rubric + confidence). Any layer whose output is prose is not replaceable.
2. **C2 — Fail-open must preserve current behavior.** A Jev outage must never change what hermes does today.
3. **C3 — Approval must fail safe, not merely fail open.** `tools/approval.py:3141-3144` currently returns `escalate` on error. That must survive.
4. **C4 — Data leaves the host.** Jev is a third-party API. Hermes users run local and self-hosted endpoints specifically to avoid that. Enablement must be explicit and bounded.
5. **C5 — No Python SDK.** The published `@typesafe-ai/sdk` is TypeScript. The wire contract must be re-implemented in Python.
6. **C6 — A gate must not sit on the user's turn path.** Every Tier B site gates work that today runs in a background thread or after the response is delivered. A Jev call costs a network round trip; placing one on the synchronous path to save a background call trades user-visible latency for backend cost, which inverts the goal.

## Architecture

Two new modules, nine touched call sites, no new dependency.

**`agent/jev_client.py`** — thin Python port of the wire contract in C5. One public method, `systemone(state, questions, *, model=None, timeout=None)`, posting `{state, questions, model}` to `/v1/systemone` via `httpx` (already used across `gateway/platforms/*`) and returning parsed typed answers. Owns transport concerns only: auth header, per-attempt timeout, bounded retry, outer deadline. Knows nothing about hermes tasks.

**`agent/jev_decide.py`** — the `decide()` wrapper every call site uses. Owns the policy that would otherwise be copy-pasted nine times:

- reads `auxiliary.<task>.jev.*` config and the global kill switch;
- short-circuits to `fallback()` when disabled, so a disabled install does zero extra work;
- builds the request, calls `JevClient`, derives a comparable confidence;
- applies `min_confidence`; on a sub-threshold or failed answer, applies that site's declared `on_uncertain` policy;
- never raises — any exception inside becomes the uncertain path.

**Nine call sites** each gain a ~6-line `decide()` branch declaring its own label set, threshold, uncertain policy, and fallback. The existing code path stays intact as that fallback — an LLM call at the Tier A sites, the current heuristic at the Tier B sites.

```
call site ──► decide()  ──► JevClient ──► /v1/systemone
                 │
                 ├─ disabled / failed / low-confidence
                 └─► on_uncertain: safe default  OR  fallback() = today's call_llm(...)
```

Placing the branch at the call site (rather than inside `call_llm`) is load-bearing for two reasons that surfaced during the audit — see Decision 1 and the `draft_contract` hazard under Consequences.

### Per-site mapping

| Site | Current call | Jev question | Uncertain policy |
|---|---|---|---|
| Approval guard | `tools/approval.py:3127` `task="approval"` | `choice{approve, deny, escalate}` | `escalate` (safe **and** free) |
| Cron urgency monitor | `cron/scripts/classify_items.py:166` `task="monitor"` | one `score` per item, 11-level rubric (0–10), batched as N named questions in one request | existing `call_llm` |
| Goal judge | `hermes_cli/goals.py:1100` `task="goal_judge"` | `choice{done, continue, wait}` + `choice` over enumerated background-process ids + `score` over duration buckets | existing `call_llm` |
| Kanban estimator | `plugins/kanban/dashboard/plugin_api.py:1853` `task="kanban_estimator"` | `choice{S, M, L}` + `score` over token-magnitude buckets | existing `call_llm` |
| Background review gate | `agent/turn_finalizer.py:698-718`, `agent/turn_context.py:592-599` | `noul` — "did this turn produce durable, reusable knowledge?" | skip the fork |
| Honcho recall gate | inside `_run`, `plugins/memory/honcho/__init__.py:939` — *after* the cadence gate at `:926-933`, *not* at it | `noul` — "would stored user context change the answer here?" | run dialectic (today's behavior) |
| Honcho reasoning level | `plugins/memory/honcho/__init__.py:1042` `_apply_reasoning_heuristic` | `score` over the level ladder, resolved once per `_run_dialectic_depth` | existing char-count heuristic |
| Honcho dialectic bail-out | `plugins/memory/honcho/__init__.py:1120-1137` `_signal_sufficient` | `score` — "how well does this answer the query with user-specific detail?" | existing length-and-bullets heuristic |
| Curator fork gate | `agent/curator.py:1679`, after `_render_candidate_list()` at `:1473` | `noul` — "is any candidate actionable?" | run the fork (today's behavior) |

### Config surface

`auxiliary.<task>` is already a free-form per-task dict (`agent/auxiliary_client.py:7536-7558`) with plugin defaults layered underneath. Jev settings nest inside it — no new config machinery.

```yaml
auxiliary:
  jev:
    enabled: true              # global; false overrides every per-task flag
    api_key_env: TYPESAFE_API_KEY
    base_url: https://api.typesafe.ai   # override for self-host/proxy
    model: null                # omit to inherit server default
    timeout: 2.0               # per attempt, seconds
    deadline: 5.0              # outer budget across all attempts
    max_retries: 1
  approval:
    jev:
      enabled: true
      min_confidence: 0.85
      i_understand_commands_leave_host: true
  monitor:
    jev: {enabled: true, min_confidence: 0.6}
  goal_judge:
    jev: {enabled: true, min_confidence: 0.75}
  kanban_estimator:
    jev: {enabled: true, min_confidence: 0.6}
  background_review_gate:
    jev:
      enabled: true
      min_confidence: 0.5
      i_understand_turn_digests_leave_host: true
  honcho_recall_gate:
    jev:
      enabled: true
      min_confidence: 0.5
      i_understand_memory_content_leaves_host: true
  honcho_reasoning_level:
    jev: {enabled: true, min_confidence: 0.6, i_understand_memory_content_leaves_host: true}
  honcho_dialectic_bailout:
    jev: {enabled: true, min_confidence: 0.6, i_understand_memory_content_leaves_host: true}
  curator_gate:
    jev: {enabled: true, min_confidence: 0.5}
```

Everything defaults off. `auxiliary.jev.enabled: false` disables all nine regardless of per-task flags.

The three Honcho sites share one acknowledgement key rather than carrying one each: they ship the same class of data (the user's current query, and at the bail-out site the synthesized user-model prose Honcho returned) and an operator enabling one of them has already accepted that exposure. The curator gate needs no key — its `state` is skill names and usage counts, never skill bodies.

## Decisions

### Decision: Integration point — explicit client plus `decide()` wrapper

- **Status:** Accepted
- **Context:** All four Tier A sites call `call_llm(task=..., messages=[...])` and parse `.choices[0].message.content`. Intercepting inside `call_llm` would give a zero-diff rollout, which is tempting given C2.
- **Options considered:** interceptor inside `call_llm`; raw client with per-site inline handling; explicit client plus a shared `decide()` wrapper.
  - *Interceptor inside `call_llm`* — config-only, no call-site diff. But the label set would have to be inferred from free-form prompt text, and Jev's `confidence` has nowhere to live in a fabricated OpenAI response, so C3's ESCALATE-on-uncertain rule becomes unexpressible without smuggling fields.
  - *Raw client only, no wrapper* — maximum per-site control, but the fail-open discipline gets duplicated at every site and drifts.
  - *Explicit client + thin `decide()` wrapper* — one real diff per site; each site declares its own labels, threshold, and fallback; confidence stays in scope; retry/deadline policy lives in exactly one place.
- **Decision:** Explicit client plus `decide()`. C3 is the deciding constraint: the approval site must read `confidence` to map low certainty onto `escalate`, and only a call-site branch can see that value.
- **Consequences:** Nine files change instead of zero, and each new site is an explicit opt-in rather than a config toggle. In exchange it defuses a live hazard the audit found: **`task="goal_judge"` is shared by two call sites, and only one is a decision.** `hermes_cli/goals.py:1100` judges a verdict; `hermes_cli/goals.py:1170` is `draft_contract`, which *generates* a `GoalContract` JSON object from an objective. A task-granularity interceptor would have silently captured `draft_contract` and broken it, because C1 means Jev cannot produce that object. Call-site placement makes the distinction structural rather than something a future reader has to remember.

### Decision: Uncertain and failure policy is per-site

- **Status:** Accepted
- **Context:** C2 says a Jev failure must not change behavior. But "unchanged behavior" means different things per site, and re-running the original LLM on every uncertain answer costs Jev **plus** the LLM — eroding the saving that motivated the work.
- **Options considered:** always fall back to the existing LLM; always fall back to a safe constant; per-site policy.
  - *Always fall back to the existing LLM* — accuracy fully preserved everywhere. But it would make the approval site call an LLM in exactly the case where escalating to a human is both safer and free.
  - *Always fall back to a safe constant* — cheapest. But it silently degrades cron urgency scoring and goal-loop verdicts whenever Jev is unsure, a behavior change nobody asked for.
  - *Per-site policy* — safe default where the safe default is also the cheap one; existing LLM where output quality is the point.
- **Decision:** Per-site. Approval escalates (C3 satisfied, and escalation is the status-quo error path already). The two *fork* gates — background review and curator — differ in direction by design: the review gate skips its fork on uncertainty (cheap, costs at most a delayed memory write), while the curator gate and the Honcho recall gate **run** their downstream work on uncertainty, because those paths are already gated by conditions the user configured and silently suppressing them would be the behavior change C2 forbids. Cron monitor, goal judge, and kanban estimator fall back to today's `call_llm`; the two Honcho tuning sites fall back to their existing heuristics. Output quality at those five is unchanged by construction.
- **Consequences:** Savings become workload-dependent rather than uniform — a site whose answers hover near its threshold pays double. That is measurable, and the per-site `min_confidence` knob is the tuning surface. Requires per-site tests rather than one shared fallback test.

### Decision: Per-task opt-in plus global kill switch

- **Status:** Accepted
- **Context:** C4. The sites are not equally sensitive: kanban estimation ships a task title and the curator gate ships skill names, while the approval guard ships shell commands, the review gate ships conversation digests, and the Honcho sites ship user queries and synthesized user-model prose.
- **Options considered:** a single global flag; per-task opt-in only; per-task opt-in plus a global kill switch.
  - *Single global flag* — simplest, but enabling Jev for kanban estimates would silently also start shipping shell commands off-box.
  - *Per-task opt-in only* — correctly scoped, but no single switch to cut everything during an incident.
  - *Per-task opt-in plus global kill switch* — both, with an extra acknowledgement key on the two sensitive sites.
- **Decision:** Per-task opt-in plus global kill switch. `auxiliary.jev.enabled: false` overrides everything. The approval site additionally requires `i_understand_commands_leave_host: true`, the review gate requires `i_understand_turn_digests_leave_host: true`, and the three Honcho sites share `i_understand_memory_content_leaves_host: true`; absent those keys the site stays on its existing path even when otherwise enabled.
- **Consequences:** More config lines to enable the sensitive sites, deliberately. Gives operators one lever to pull during an incident without editing nine keys. Note that the Honcho sites' data already leaves the host — to Honcho's own API, by the operator's existing config — so the key there acknowledges a *second* processor rather than a first.

### Decision: Review-gate state is last turn plus tool names, no payloads

- **Status:** Accepted
- **Context:** The gate's downstream fork receives `messages_snapshot=list(messages)` (`agent/turn_finalizer.py:713-717`) — the whole conversation. Handing that to Jev would maximize signal and maximize the C4 exposure the opt-in was meant to bound.
- **Options considered:** full `messages_snapshot` truncated; last turn text only; last turn plus tool names without payloads.
  - *Full snapshot, truncated* — the gate sees what the fork sees, but ships tool output, file contents, and command results to a third party on every gated turn.
  - *Last turn text only* — smallest exposure, but drops the signal that the turn did research or touched skills, which are the turns most likely to have produced something worth saving.
  - *Last turn plus tool names* — tool *names* carry the "did research happen" signal; their arguments and output carry the sensitive part.
- **Decision:** Last turn plus tool names. `state` is `{user, assistant, tools_used, iters}` with message text clipped to a configured char budget and tool arguments and results never included.
- **Consequences:** The gate is strictly less informed than the fork it gates, so some false negatives (a durable fact that only appeared inside tool output goes unsaved). Acceptable: the existing counter fires on a fixed interval regardless of content, so a content-aware gate with partial visibility is still a strict improvement, and the counter remains as the rate limit.

### Decision: Fake client for behavior, one recorded fixture for contract

- **Status:** Accepted
- **Context:** C5 means the wire contract is hand-ported, so shape drift is a live risk. But the API is network and paid, and the interesting cases (low confidence, malformed body, partial answers) are hard to provoke on demand.
- **Options considered:** recorded fixtures only; fake client only; both, split by what each proves.
  - *Recorded fixtures only* — every test hits the real parser with real payloads, but building the low-confidence and malformed cases means hand-editing fixtures, which is a fake client with extra steps.
  - *Fake client only* — fastest, but nothing validates that the real request body or response shape still matches the port.
  - *Both* — fake client for site behavior, one recorded fixture for shape.
- **Decision:** Both. The fake covers the behavior matrix; a single contract test replays a recorded real response through the production parser.
- **Consequences:** The recorded fixture needs a documented refresh path, or it rots. A `scripts/` helper to re-record against a live key covers that, run manually rather than in CI.

### Decision: Honcho recall gate sits after the cadence counter, inside the worker thread

- **Status:** Accepted
- **Context:** The Honcho prefetch path already has five free gates (`plugins/memory/honcho/__init__.py:892-933`). The last of them is a turn counter widened by an empty-streak backoff — cheap, but blind to whether the current turn would benefit from stored context.
- **Options considered:** Jev replaces the cadence counter; Jev as a second gate after the counter fires; drop the site.
  - *Jev replaces the counter* — maximum relevance, no high-value turn delayed by an arbitrary interval. But it fires a paid round trip on every non-trivial turn, including the many the counter suppresses for free, and the empty-streak backoff loses the counter it widens.
  - *Second gate after the counter* — counter stays the rate limit; Jev is consulted at most once per cadence window and answers the question the counter cannot.
  - *Drop the site* — the five existing gates already suppress most calls.
- **Decision:** Second gate. The counter remains untouched as the rate limit; Jev runs only once it has already fired.
- **Consequences:** A placement hazard the audit surfaced, and the reason this decision names a line number rather than a function: **the cadence gate at `:926-933` runs synchronously on the turn path — the worker thread is not spawned until `:955`.** Putting the Jev call where the counter lives would add a network round trip to every gated turn, violating C6 and trading user-visible latency for backend savings. The gate therefore goes *inside* `_run` at `:939`, before `_run_dialectic_depth`, where it is off the critical path. Cost: the thread is spawned and then may immediately decide to do nothing — a thread spawn is orders of magnitude cheaper than the dialectic call it guards.

### Decision: Reasoning level resolved once per dialectic run, not per pass

- **Status:** Accepted
- **Context:** `_apply_reasoning_heuristic` (`:1042`) picks Honcho's reasoning level from raw character count (`+1 at >=120, +2 at >=400`). Level drives billed compute on every pass, and character count is a weak proxy for how much reasoning a query needs.
- **Options considered:** leave out of scope; call Jev inside `_resolve_pass_level`; hoist to one call per `_run_dialectic_depth`.
  - *Leave out of scope* — the char heuristic is free and already clamped by `reasoning_level_cap`; a wrong Jev answer raises Honcho cost rather than lowering it.
  - *Call inside `_resolve_pass_level`* — smallest diff, drops into the function that holds the heuristic today. But that function runs once per pass (`:1182`), so a depth-3 run pays three Jev calls for an answer that cannot change between them — the query is fixed for the whole run.
  - *Hoist to once per run* — one call, result threaded through the pass loop.
- **Decision:** Hoist. `_run_dialectic_depth` resolves the level bump once and passes it down; `_resolve_pass_level` gains an optional pre-resolved bump and keeps the char heuristic as its fallback.
- **Consequences:** `_resolve_pass_level` gains a parameter, so its callers change. In exchange the number of Jev calls per dialectic run is independent of configured depth. Because this site and the recall gate both key off the same unchanged query text and both resolve once per run, they can share a single request — see Data flow.

### Decision: Curator gate included, gating the whole candidate list rather than triaging it

- **Status:** Accepted
- **Context:** The curator fork is a full `AIAgent` on the main model, but `curator.consolidate` defaults false (`agent/curator.py:74`) and `run_curator_review` returns early when it is off (`:1598`), so the site costs nothing in a default install.
- **Options considered:** defer to a follow-up spec; gate the whole list with one `noul`; per-candidate `choice` triage.
  - *Defer* — holds this spec at eight sites and avoids spending plan and test budget on a path many installs never execute.
  - *Whole-list gate* — reuses the accepted review-gate shape exactly; small diff; skips the fork when nothing is actionable.
  - *Per-candidate triage* — also shrinks the fork's prompt by passing only the actionable subset, but moves real curation judgment into Jev rather than only gating it, and the fork re-decides each candidate anyway.
- **Decision:** Whole-list gate. One `noul` over the rendered candidate list ahead of the fork at `:1679`; the `consolidate` check at `:1598` stays exactly as-is in front of it.
- **Consequences:** Savings apply only to operators who enabled consolidation — real but conditional, and the spec should not claim otherwise. Jev never decides a candidate's fate, only whether the existing decider runs, which keeps curation semantics unchanged and makes the site's blast radius identical to the review gate's.

## Data flow

**Synchronous sites (approval, goal judge, kanban estimator).** Call site builds `state` as a JSON object and `questions` as a name-keyed map, calls `decide()`, blocks for one HTTP round trip inside the outer deadline, receives a label or score, proceeds. No new state persists.

**Cron monitor.** One request carries all items for the run, each as a separately named `score` question (`item_0`, `item_1`, …) — the `questions` field is a map, so batching is native to the protocol. Answers are re-keyed to item indices. Threshold filtering stays local and unchanged. Per-item `confidence` is evaluated individually: only sub-threshold items fall back, and they fall back as a single reduced `call_llm` batch rather than one call each.

**Goal judge.** All three questions (verdict, wait target, wait duration) go in one request. Wait target and duration are read only when the verdict is `wait`; asking unconditionally costs one round trip instead of two. The wait-target `choice` labels are built from the background-process list already rendered into today's prompt (`gather_background_processes`, `hermes_cli/goals.py:1128`), so the candidate set is enumerable by construction. `reason` is log-only in the current contract and becomes a template string.

**Review gate.** Runs after the response is delivered, on the same post-turn path as today (`agent/turn_finalizer.py:713`), so it never competes with the user's task. The existing counters remain as the rate limit — Jev is consulted *only* when a counter has already fired, never on every turn. A yes spawns the fork unchanged; a no resets the counter and spawns nothing.

**Honcho recall gate and reasoning level — one request, two questions.** Both key off the same unchanged query text and both resolve once per run, so when both are enabled they go out as a single request with two named questions (`recall_worth_it` as `noul`, `reasoning_level` as `score`). The resolved level is threaded into `_run_dialectic_depth` as an optional pre-resolved bump. When only one site is enabled it resolves alone. Both run inside the worker thread spawned at `:955`, never on the turn path (C6, Decision 6). `state` is `{query, turns_since_last_recall, last_recall_was_empty}` — the user's current message plus the two counters the existing gate already tracks.

**Honcho dialectic bail-out.** Cannot join that batch: it scores a pass *result*, which does not exist until the pass returns. Fires after each pass inside `_run_dialectic_depth`, replacing the length-and-bullets check in `_signal_sufficient` (`:1120-1137`). `state` is `{query, result}`. Note that `_signal_sufficient` is a `@staticmethod` taking only `result` today — judging "does this answer the query" needs the query too, so the signature changes and its callers with it.

**Curator gate.** Runs inside the daemon thread `run_curator_review` already spawns (`synchronous=False` default), after the `consolidate` check at `:1598` and after `_render_candidate_list()` at `:1473`, before the fork at `:1679`. `state` is the rendered candidate list reduced to `{name, last_used, use_count, age_days}` per candidate — names and counters, never skill bodies.

**State never persisted.** No Jev response is cached or written to disk in this scope. Usage counters from `SystemOneResult.usage` are logged at debug level only.

## Error handling

**Outer deadline is mandatory.** The SDK documents `RequestOptions.timeout` as per attempt, explicitly noting "there is no total retry budget". The Python port must therefore track its own wall-clock deadline across attempts and abandon the call when it expires, independent of the per-attempt timeout. Default: 2.0s per attempt, 1 retry, 5.0s outer.

**Retry is bounded and narrow.** Retry on 408, 429, and 5xx, and on connection errors. Never retry 4xx other than 408/429. Honor `Retry-After` only up to the outer deadline.

**Every failure is the uncertain path.** Timeout, HTTP error, connection failure, malformed body, missing answer key, or a label outside the declared set all route to the site's `on_uncertain` policy. `decide()` does not raise; a bug inside it must not take down a turn.

**Confidence derivation differs by answer type.** `choice` and `score` responses carry an explicit `confidence` field. **`noul` does not** — it returns only `noul: number`, the probability of a yes. The review gate's comparable confidence is therefore `abs(noul - 0.5) * 2`, and its decision is `noul >= 0.5`. This asymmetry is in the published types and must not be papered over with a fabricated field.

> **Security requirement, not a default.** The approval guard's error path returns `escalate` today (`tools/approval.py:3141-3144`). Every Jev failure mode at that site must also return `escalate`. A Jev outage, a malformed response, an expired API key, or a label outside `{approve, deny, escalate}` must never resolve to `approve`. This is a test assertion, not a code comment.

**Injection posture improves.** Today the command under review sits in a `<command>` block inside a prompt, in the same channel as the instructions. Under Jev it moves into `state`, structurally outside the instruction channel. That is a reason to prefer the Jev path where enabled, not a reason to relax the escalate-on-failure rule.

**Disabled is free.** When the global switch or the site's flag is off, `decide()` calls `fallback()` without constructing a client, reading credentials, or touching the network.

## Testing strategy

**Behavior matrix (`tests/test_jev_decide.py`, fake client).** Each of the nine sites crossed with: healthy high-confidence answer, sub-threshold answer, per-attempt timeout, outer-deadline expiry, HTTP 500, HTTP 401, malformed JSON body, missing answer key, and out-of-set label. Assertion per cell is the site's declared uncertain policy.

**Approval safety (`tests/test_jev_approval_safety.py`).** Dedicated file asserting that no failure mode at the approval site yields `approve`. Separated from the matrix so it cannot be weakened by a bulk edit.

**Contract (`tests/test_jev_contract.py`).** One recorded `/v1/systemone` response per answer type replayed through the production parser via `httpx.MockTransport`. Catches drift in request body shape and response field names. Offline.

**Config precedence (`tests/test_jev_config.py`).** Global off beats per-task on; missing acknowledgement key keeps the sensitive sites on their existing path; absent config means every site behaves exactly as today.

**Turn-path guard (`tests/test_jev_offpath.py`).** C6 is the constraint most easily broken by a later edit that moves a gate "somewhere tidier". Assert that with every Jev site enabled and the client stubbed to block for longer than the outer deadline, `prefetch_for_turn` and the post-turn finalizer both return without waiting on it — the Honcho gate resolves inside the worker thread, not at the cadence check. A failure here means a gate migrated onto the synchronous path.

**Regression guard.** The existing test suites for the nine touched sites must pass unmodified with Jev disabled — that is the mechanical proof of C2.

**Out of scope for v1.** Live-network tests in CI; accuracy benchmarking of Jev verdicts against current LLM verdicts; cost-saving measurement. The last two need production traffic and belong in a follow-up once the sites are shipped behind their flags.

## Tech stack

- **Python 3, `httpx`** — already a transitive dependency used across `gateway/platforms/*`; no new package. (Decision 1)
- **`POST https://api.typesafe.ai/v1/systemone`**, body `{state, questions, model}`, response `{model, answers, usage}`. Base URL overridable via `auxiliary.jev.base_url`. (Constraint C5)
- **Existing `auxiliary.<task>` config dict** (`agent/auxiliary_client.py:7536-7558`) as the settings surface; no new config loader. (Decision 3)
- **API key from environment**, name configurable via `auxiliary.jev.api_key_env`, default `TYPESAFE_API_KEY`. Never written to config files. (Decision 3)
- **`pytest` with `httpx.MockTransport`** for the contract test; plain injectable fake for the behavior matrix. (Decision 5)

## Open questions

- Deferred: Confidence thresholds are seeded at 0.85 (approval), 0.75 (goal judge), 0.6 (monitor, kanban, Honcho reasoning level, Honcho bail-out), 0.5 (review gate, Honcho recall gate, curator gate). These are opening guesses, not measured values — they are per-site config keys precisely so they can be tuned after the first production traffic. Plan time should ship the knobs, not calibrate them.
- Deferred: Whether the Honcho reasoning-level site should be allowed to raise the level above what the char heuristic would have picked, or only lower it. Lower-only is strictly cost-reducing and cannot regress spend; two-way is more accurate but can cost more than today on queries Jev judges hard. `reasoning_level_cap` already bounds the top end either way. Needs production traffic to settle.
- Deferred: Whether `usage` totals get surfaced in `hermes` cost reporting alongside existing auxiliary-task accounting. Additive, no architectural consequence, and cheaper to wire once one site is live.
- Deferred: Choice of Jev model (`auxiliary.jev.model`). The request field is optional and inherits a server default; picking a specific model is a tuning decision that needs the same production traffic as the thresholds.

## Provenance

- **user-picked:** none — the user accepted the recommendation in all eight checkpoints rather than selecting an alternative.
- **recommended-and-accepted:** Decision 1 (integration point), Decision 2 (per-site uncertain policy), Decision 3 (per-task opt-in plus global kill switch), Decision 4 (review-gate state shape), Decision 5 (testing strategy), Decision 6 (Honcho recall gate placement), Decision 7 (reasoning level hoisted once per run), Decision 8 (curator whole-list gate).
- **assumed:** Batching shape for the cron monitor (all items as named questions in one request), for the goal judge (all three questions in one request), and for the Honcho recall gate plus reasoning level (two questions in one request) — flagged rather than gated because the `questions` map makes batching the protocol-native shape, and regrouping later is a local change at one call site. Shared acknowledgement key across the three Honcho sites rather than one key each, on the grounds that they ship the same class of data. Initial confidence thresholds, recorded under Open questions. Default timeout and retry values, ported from the `jev-router` policy defaults.
- **overrides:** none.
- **scope changes:** Tier B was added after the first five decisions were settled, at the user's request. Decisions 1–5 were re-read against the four new sites and none needed revisiting; Decision 2's per-site policy absorbed them by design, and Decision 3 gained one acknowledgement key.
