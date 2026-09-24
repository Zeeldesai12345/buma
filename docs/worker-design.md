## DD-14 — Worker uses Redis List (`BRPOP`) rather than Redis Streams

**Decision:** The Worker consumes messages from the Redis List key `buma:triage:queue` using `BRPOP`, consistent with the gateway's `LPUSH`.

**Choice made:** Redis List with `BRPOP`.

**Alternatives considered:**

| | Redis List (`LPUSH` / `BRPOP`) | Redis Streams (`XADD` / `XREADGROUP`) |
|---|---|---|
| Complexity | Simple — no consumer groups, no message IDs | Significant — consumer groups, ACK commands, stream trimming |
| Message safety | Message removed on pop; lost if worker crashes mid-process | Message retained until explicitly ACKed; redelivered on crash |
| Multiple consumers | All consumers compete for the same messages | Each consumer group receives every message independently |
| Redelivery on crash | No automatic redelivery | Yes — pending entries list tracks unACKed messages |
| Gateway compatibility | Gateway already uses `LPUSH` — no change needed | Would require changing `QueuePublisher` in the gateway |

**Reasoning:**

The gateway already uses `LPUSH`. Changing to Streams would require modifying `QueuePublisher`, which is a tested and merged component. For MVP with a single Worker instance, no consumer group is needed. Message loss on Worker crash is an acceptable trade-off at this stage: the `WebhookDelivery` table records every delivery the gateway accepted, making dropped messages detectable, and GitHub retries webhook deliveries. Redis Streams can be adopted post-MVP without changing the gateway — the migration can be handled transparently in the Worker.

---

## DD-15 — `QueueConsumer` exposes `run_once()` and `run_forever()` as separate methods

**Decision:** The consumer loop is split into two methods: `run_once()` (single iteration) and `run_forever()` (infinite loop calling `run_once()`).

**Choice made:** Two-method design.

**Alternative considered:**

A single `run()` method containing the full loop.

*Reason rejected:* An infinite loop cannot be unit tested directly without `asyncio.wait_for()` timeouts or `cancel()` — both of which make tests slow and flaky. `run_once()` is a clean, synchronous-feeling coroutine: call it, await it, assert on the result. `run_forever()` is tested only for its control-flow behaviour (stop on event, continue after error) using a `stop_event` that halts the loop after a controlled number of iterations.

**Return value contract for `run_once()`:**

| Return | Meaning |
|---|---|
| `False` | `BRPOP` timed out — queue was empty during the timeout window |
| `True` | A message was popped — either processed successfully or dropped as malformed |

Returning `True` for malformed messages is intentional: the message has been consumed from Redis and cannot be returned. Returning `False` would imply the queue is empty, which is incorrect.

---

## DD-16 — Graceful shutdown uses `asyncio.Event`, set by OS signal handlers

**Decision:** The Worker runner installs `SIGINT` and `SIGTERM` signal handlers that set a shared `asyncio.Event`. The consumer loop checks this event before each iteration and exits cleanly when it is set.

**Choice made:** `asyncio.Event` + signal handlers.

**Alternatives considered:**

| Option | Problem |
|---|---|
| `asyncio.CancelledError` via `task.cancel()` | Cancellation can interrupt mid-message processing, leaving a partially processed event with no audit record |
| `KeyboardInterrupt` only | Does not handle `SIGTERM`, which is the standard Kubernetes termination signal |
| `threading.Event` | Introduces thread-safety concerns in an otherwise pure-async codebase |

**Reasoning:**

`asyncio.Event` is checked at the top of each loop iteration — after `run_once()` returns. This means the current message always finishes processing before the loop exits. No message is abandoned mid-flight. The pattern is idiomatic for async Python services and integrates cleanly with Kubernetes pod lifecycle: Kubernetes sends `SIGTERM`, the handler sets the event, the worker drains the current message and exits, Kubernetes proceeds with the pod replacement.

---

## DD-17 — Worker is fully asynchronous

**Decision:** The Worker uses `asyncio`, `redis.asyncio`, and will use `AsyncSession` for future DB writes. The entry point is `asyncio.run(main())`.

**Choice made:** Async throughout.

**Alternative considered:**

A synchronous Worker using the blocking `redis-py` client and synchronous SQLAlchemy.

*Reason rejected:* The rest of the codebase — `redis.asyncio`, `sqlalchemy.ext.asyncio`, FastAPI — is already async. A synchronous Worker would require a separate Redis client, a separate SQLAlchemy engine configuration, and a different test fixture approach. It would also block the event loop during I/O operations, preventing clean shutdown signal handling from working correctly. Consistency with the gateway reduces cognitive overhead and keeps shared infrastructure (settings, DB engine) reusable across both services.

---

## DD-18 — `EventProcessorService` is introduced as a deliberate placeholder

**Decision:** The Worker's processing pipeline is encapsulated in `EventProcessorService` from the start, even though it contains only a log statement at this stage.

**Choice made:** Introduce the class now with a `# TODO` body.

**Alternative considered:**

Skip the processor class entirely until triage logic is ready; have the consumer log the event directly.

*Reason rejected:* If the consumer calls triage logic directly, adding the triage engine later requires editing consumer code and breaking consumer tests. The placeholder establishes the boundary now: the consumer's responsibility ends at deserialization and dispatch; the processor's responsibility begins at event receipt. Each future step (triage, assignment, persistence, GitHub patch) is added to `EventProcessorService` without touching `QueueConsumer`.

**Planned expansion of `EventProcessorService.process()`:**

```
Phase 1 (current): log receipt only
Phase 2: load RepoConfig from DB
Phase 3: run rule-based triage engine (category + priority)
Phase 4: assignee selection (skills + capacity + optimistic locking on DeveloperProfile.version)
Phase 5: persist IssueSnapshot + TriageDecision
Phase 6: GitHub patch (labels, assignee, explanation comment)
```

---

## DD-23 — Hybrid Claude API fallback for low-confidence classifications

**Decision:** `TriageEngine.classify()` (deterministic, rule-based) always runs first and is unchanged. A new async `TriageEngine.classify_with_fallback()` wraps it: if the rule result's `confidence` is below `confidence_threshold` (default `0.5`, env `CLAUDE_CONFIDENCE_THRESHOLD`) **and** an `ANTHROPIC_API_KEY` is configured, it consults `ClaudeClassifier` (`src/buma/worker/services/claude_client.py`) for a second opinion. Claude is forced (via `tool_choice`) to respond with a structured `classify_issue` tool call constrained to the same category/priority enums the rule engine uses, so its answer can be validated the same way rule output already is.

**Choice made:** Rules-first, Claude as an optional low-confidence fallback — never the reverse.

**Alternatives considered:**

| | Rules-first, Claude fallback (chosen) | Claude-first, rules as fallback | Claude replaces rules entirely |
|---|---|---|---|
| Cost | One paid API call only on ambiguous issues | A paid API call on every issue | A paid API call on every issue |
| Availability | A Claude outage never blocks triage — rules always answer | An outage degrades every issue, not just ambiguous ones | An outage stops triage entirely |
| Determinism | Clear, obvious issues stay 100% deterministic and free | Non-deterministic even for obvious cases | Fully non-deterministic |
| Explainability | `engine_version` records exactly which path answered | Same, but the "normal" path is now the black box | No rule-based baseline left to compare against |

**Reasoning:**

Section 1 of `docs/claude.md` requires triage decisions to stay explainable and traceable, and explicitly says not to replace rule-based triage with LLM-based triage unless requested. Consulting Claude only when the rule engine itself reports low confidence keeps the common case (clear labels, obvious keywords) fast, free, and fully deterministic, and bounds the blast radius of an LLM outage or bad response to the already-ambiguous minority of issues — exactly where a second opinion is most useful anyway.

**Failure handling:** `ClaudeClassifier.classify()` never raises — any timeout (`CLAUDE_TIMEOUT_SECONDS`, default `8s`), connection error, non-2xx response, or invalid/out-of-enum category, priority, or confidence value is caught and logged, and `classify_with_fallback()` falls back to the original rule result. `TriageResult.engine_version` distinguishes all three outcomes:

| `engine_version` | Meaning |
|---|---|
| `rules-v1` | Rule confidence was already high enough — Claude was never called |
| `claude-hybrid-v1` | Rule confidence was low; Claude answered and its response passed validation |
| `rules-v1-fallback` | Rule confidence was low; Claude was attempted but failed/timed out/returned invalid data — the rule result was used anyway |

No database migration was required — `engine_version` is an existing string column on `TriageDecision`; the new values are just additional strings it can hold.