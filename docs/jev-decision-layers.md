# Jev Decision Layers

> **Audience:** Operators enabling or auditing third-party decision routing
> **Source files:** `agent/jev_client.py`, `agent/jev_decide.py`, `tools/approval.py`,
> `cron/scripts/classify_items.py`, `hermes_cli/goals.py`,
> `plugins/kanban/dashboard/plugin_api.py`, `agent/turn_finalizer.py`,
> `plugins/memory/honcho/__init__.py`, `agent/curator.py`
> **Config:** `cli-config.yaml.example` (`auxiliary.jev` and per-task blocks)
> **Related:** `docs/specs/2026-09-23-jev-decision-layers.md`

## What Jev replaces, and what it cannot

Jev (`api.typesafe.ai`, `/v1/systemone`) is a third-party decision endpoint. Eight call
sites in Hermes can optionally consult it instead of running a full LLM call or a blind
heuristic/counter: the smart approval guard, the cron urgency monitor, the goal-loop
judge, the kanban token/complexity estimator, the background memory/skill review gate,
the Honcho recall gate (which also resolves the dialectic reasoning-level bump in the
same request), the Honcho dialectic bail-out, and the curator review-fork gate.

Jev only ever answers one of three fixed shapes: `noul` (a yes/no probability), `choice`
(a label plus confidence and per-label probabilities), or `score` (a position on an
ordinal rubric plus confidence). It never emits prose. Any site whose job is to *generate*
text or structured content is not a candidate — for example `hermes_cli/goals.py`'s
`draft_contract` shares a task name (`goal_judge`) with the judge that Jev does answer,
but `draft_contract` itself is untouched: it builds a `GoalContract` object, which Jev
cannot produce.

## Enablement is explicit and layered

Every site ships disabled. Nothing is sent to Jev until **both**:

- `auxiliary.jev.enabled: true` (the global switch), **and**
- the per-task flag, e.g. `auxiliary.approval.jev.enabled: true`, is also `true`.

`auxiliary.jev.enabled: false` overrides all eight per-task flags regardless of their own
settings — it is the single lever to cut every site during an incident.

`TYPESAFE_API_KEY` (the env var named by `auxiliary.jev.api_key_env`, which defaults to
that name) is read from the environment at call time (`agent/jev_decide.py`,
`_build_client`). It is never read from, or written to, `config.yaml` — there is no
config key for the key value itself, only for the name of the environment variable that
holds it.

See `cli-config.yaml.example` for the full commented-out `auxiliary.jev` block and all
eight per-task blocks, ready to uncomment and edit.

## Per-site reference

Each row is one `decide(task, ...)` call site. "State sent" lists exactly what leaves the
host if the site is enabled; nothing else in `state` is included. The uncertain-direction
column covers every non-confident outcome: Jev disabled, missing key, missing
acknowledgement, a transport/HTTP error, or an answer below `min_confidence`.

| Config key | Jev question | State sent | On unavailable / unsure |
|---|---|---|---|
| `auxiliary.approval.jev` | "Is this shell command safe for an autonomous agent to execute?" (`choice`: approve / deny / escalate) | `command` (the sanitized shell command text), `flagged_as` (the flag description) | Disabled: the existing `call_llm` guard runs unchanged. Missing acknowledgement: the existing guard still runs, but its `APPROVE` reply is downgraded to `escalate` — it can `deny` or `escalate`, never auto-`approve`. Any other failure or low-confidence answer: returns `escalate` directly, without calling `call_llm` at all. |
| `auxiliary.monitor.jev` | One `score` question per item: "How urgent is this item against the user's criteria?" (11-level rubric) | `criteria` (the classify run's `--criteria` text), `items` (a compact per-item view: only `title`/`subject`/`summary`/`text`/`body`/`from`/`sender`/`url` keys, whichever are present) | Disabled: every item is scored by the existing single `call_llm` batch call. Otherwise: items with a confident score keep it; the remaining items are re-scored in one reduced `call_llm` call. |
| `auxiliary.goal_judge.jev` | `verdict` (`choice`: done / continue / wait), `wait_seconds` (`score`, duration bucket), and — only when a background process is running — `wait_target` (`choice` over running PIDs) | `goal`, `last_response`, `background`, `current_time` (all truncated), plus `contract` and `subgoals` when present | Disabled, or `verdict` not confidently answered: falls back to the existing `call_llm` path unchanged. A confident `wait` verdict without a confident `wait_target` or `wait_seconds` also falls back to `call_llm`. `draft_contract` (same task name, different call site) never goes through `decide` at all. |
| `auxiliary.kanban_estimator.jev` | `complexity` (`choice`: S / M / L), `tokens` (`score`, token-magnitude bucket) | `title`, `description` (both capped) | Disabled, or either answer not confident: falls back to the existing `call_llm` path unchanged. |
| `auxiliary.background_review_gate.jev` | `durable` (`noul`): "Did this exchange produce a durable, reusable fact or procedure worth saving to memory or a skill?" | `user`, `assistant` (last turn's text, each clipped to 2000 chars), `tools_used` (tool **name** strings only, de-duplicated — never arguments or results), `iters` | **Skips the fork** on any uncertain or failed outcome — the fork runs only when Jev is disabled (today's counter-only behavior) or when it confidently answers `noul >= 0.5`. |
| `auxiliary.honcho_recall_gate.jev` | One request, two questions: `recall_worth_it` (`noul`): "Would stored knowledge about this user change how this message should be answered?"; `reasoning_level` (`score`, 3-level rubric): "How much reasoning does answering this message from stored user context require?" | `query` (clipped to 2000 chars), `turns_since_last_recall`, `last_recall_was_empty` | **Runs the dialectic pass** (today's behavior) unless Jev confidently answers `recall_worth_it < 0.5`, in which case it's skipped. The reasoning-level bump falls back to the existing char-count heuristic whenever that answer isn't confident, independently of the recall decision. |
| `auxiliary.honcho_dialectic_bailout.jev` | `sufficiency` (`score`, 5-level rubric): "How well does this answer the query with specific detail about this user?" | `query` (clipped to 2000 chars), `result` (the prior pass's synthesized answer text, clipped to 4000 chars) | Disabled, or the answer isn't confident: falls back to the existing `_signal_sufficient` length/structure heuristic. Never called at `dialecticDepth == 1` (there is no prior pass to judge). |
| `auxiliary.curator_gate.jev` | `actionable` (`noul`): "Is any of these skills worth archiving, consolidating, or patching right now?" | A list of candidate rows, each reduced to `{name, state, use_count, activity_count, last_activity_at}` — never skill body text | **Runs the review fork** (today's behavior) unless Jev confidently answers `noul < 0.5`, in which case it's skipped. |

The two fork gates point in opposite directions on purpose: the background review gate
**skips** its fork when uncertain (the cost is at most a delayed memory write, and the
turn counter fires again next interval), while the curator gate and the Honcho recall
gate **run** their downstream work when uncertain, because that work is already gated by
conditions the operator configured, and silently suppressing it on an uncertain answer
would be a behavior change.

## Acknowledgement keys

Three sensitive sites additionally require a per-task acknowledgement key. Without it,
the site stays on its existing path even when `auxiliary.jev.enabled` and its own
`jev.enabled` are both `true`:

- **`i_understand_commands_leave_host`** — approval guard. Acknowledges that shell command
  text leaves the host.
- **`i_understand_turn_digests_leave_host`** — background review gate. Acknowledges that
  the last turn's user/assistant text and the names of tools it called leave the host.
- **`i_understand_memory_content_leaves_host`** — shared by the Honcho recall gate, the
  reasoning-level bump (same request as the recall gate), and the dialectic bail-out.
  Acknowledges that the user's current query leaves the host, and — at the bail-out site
  specifically — that the synthesized user-model prose Honcho already returned also
  leaves the host a second time. For the Honcho sites this data already reaches Honcho's
  own API under the operator's existing configuration; the key acknowledges a *second*
  processor, not a first.

For the approval guard, an unacknowledged operator keeps the existing `call_llm` guard —
it can still `deny`, but an `APPROVE` reply from it is downgraded to `escalate`; it can
never auto-approve without the key.

The curator gate needs no acknowledgement key: its `state` is skill names and usage
counts, never skill bodies.

## Approval fails safe, never open

The approval guard returns `escalate` on every Jev failure, timeout, missing key, missing
acknowledgement, or out-of-set answer — it never returns `approve` from the Jev path.
This is enforced by `tests/test_jev_approval_safety.py`.
