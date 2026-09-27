# Buma

**Automated GitHub issue triage and assignment, with semantic retrieval, a read-only MCP server, and an agentic RAG assistant.**

[![CI](https://github.com/Zeeldesai12345/buma/actions/workflows/ci.yml/badge.svg)](https://github.com/Zeeldesai12345/buma/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-async-009688)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16%20%2B%20pgvector-336791)
![React](https://img.shields.io/badge/React-19-61DAFB)
![Coverage gate](https://img.shields.io/badge/coverage%20gate-80%25-brightgreen)

---

## Contents

- [Overview](#overview)
- [Key Capabilities](#key-capabilities)
- [Architecture](#architecture)
  - [High-Level Architecture](#high-level-architecture)
  - [GitHub Issue Processing](#github-issue-processing)
  - [Agentic RAG Assistant](#agentic-rag-assistant-architecture)
  - [MCP Architecture](#mcp-architecture)
  - [Semantic Retrieval Architecture](#semantic-retrieval-architecture)
- [Components](#components)
- [Triage Pipeline](#triage-pipeline)
- [AI Capabilities](#ai-capabilities)
  - [Hybrid Claude Classification](#hybrid-claude-classification)
  - [Prompt-Injection Protection and Cost Controls](#prompt-injection-protection-and-cost-controls)
  - [Semantic Duplicate Detection](#semantic-duplicate-detection)
  - [Ask Buma: Agentic RAG Assistant](#ask-buma-agentic-rag-assistant)
  - [MCP Integration](#mcp-integration)
- [Web Dashboard](#web-dashboard)
- [API Reference](#api-reference)
- [Security and Reliability](#security-and-reliability)
- [Data and Retrieval Architecture](#data-and-retrieval-architecture)
- [Configuration](#configuration)
- [Local Development](#local-development)
- [MCP Setup](#mcp-setup)
- [Testing](#testing)
- [CI and Developer Tooling](#ci-and-developer-tooling)
- [Technical Architecture and Design Decisions](#technical-architecture-and-design-decisions)
- [Repository Layout](#repository-layout)
- [Further Documentation](#further-documentation)

---

## Overview

Buma triages GitHub issues automatically. When an issue is opened in an enrolled repository, Buma classifies its category and priority, selects a developer based on skills and current workload, records the decision, and writes the result back to GitHub: labels, an assignee, and a comment explaining the decision.

**The problem it solves.** Every new issue needs someone to decide what kind of issue it is, how urgent it is, and who should own it. Doing this by hand is slow and inconsistent, and it hides workload imbalances. Buma makes the decision in seconds, applies the same rules every time, balances assignments against each developer's capacity, and keeps an auditable record of every decision.

On top of the triage pipeline, Buma provides:

- **Semantic retrieval** over issue embeddings stored in PostgreSQL with pgvector.
- **Ask Buma**, a dashboard assistant that answers natural-language questions about a repository using a bounded, tool-calling Claude workflow grounded in Buma's data.
- **A read-only MCP server** that lets MCP clients such as Claude Code query triage history, workload, and enrolled repositories.

---

## Key Capabilities

| Area | Capability |
|---|---|
| **Ingestion** | GitHub App webhook receiver with HMAC-SHA256 signature verification and delivery-level idempotency. Events are queued in Redis. |
| **Triage** | Deterministic rule engine (labels, then keywords, then per-repo defaults) with per-repository label overrides. |
| **AI classification** | Optional Claude fallback, used only when rule confidence is below a threshold. Uses forced tool output, strict validation, a severity ceiling, a Redis-backed daily budget, and a circuit breaker. |
| **Assignment** | Skill-matched, capacity-aware developer selection with optimistic locking. Workload is decremented when issues close. |
| **GitHub write-back** | Labels, assignee, and an explanation comment, applied through a GitHub App installation token. |
| **Semantic duplicate detection** | Local CPU embeddings (`fastembed`, `BAAI/bge-small-en-v1.5`), stored in pgvector with an HNSW cosine index. Nearest-neighbour search is scoped to one repository. |
| **Ask Buma assistant** | Agentic RAG over one repository's data, streamed to the dashboard over Server-Sent Events. Runs on a read-only database connection with its own budget and circuit breaker. |
| **MCP server** | stdio MCP server with two read-only tools and one resource, backed by a Postgres-enforced read-only session. |
| **Observability API** | Triage history, issue snapshots, developer workload, and productivity metrics per repository. |
| **Dashboard** | React 19 + MUI app for enrollment, team management, issues, productivity, and the assistant. |

---

## Architecture

Each diagram below is followed by a short explanation. Component details follow in later sections.

### High-Level Architecture

```mermaid
flowchart TB
    subgraph Sources["Inbound"]
        direction LR
        GHI["GitHub repository<br/>issues webhooks"]
        UI["React dashboard"]
        MCPC["MCP client<br/>e.g. Claude Code"]
    end

    subgraph Buma["Buma services"]
        direction LR
        GW["Gateway (FastAPI)<br/>webhooks · REST API · Ask Buma SSE · OAuth"]
        Q[("Redis<br/>buma:triage:queue<br/>LLM budget keys")]
        WK["Worker (asyncio)<br/>triage · assignment · embeddings<br/>(local fastembed model)"]
        MCP["MCP server<br/>stdio, read-only"]
    end

    subgraph Backing["Data and external APIs"]
        direction LR
        PG[("PostgreSQL 16<br/>+ pgvector")]
        CL["Anthropic Claude API"]
        GHAPI["GitHub REST API"]
    end

    GHI -- "signed webhook" --> GW
    UI -- "REST + SSE, Bearer JWT" --> GW
    MCPC -- "stdio" --> MCP
    GW -- "LPUSH" --> Q
    Q -- "BRPOP" --> WK
    GW --> PG
    WK --> PG
    MCP -- "read-only" --> PG
    GW -- "Ask Buma" --> CL
    WK -- "low-confidence fallback" --> CL
    WK -- "labels, assignee, comment" --> GHAPI
```

The **gateway** is the only HTTP entry point. It verifies GitHub webhooks, records each delivery, and pushes a normalized event onto a Redis list. It also serves the dashboard's REST API and the Ask Buma streaming endpoint. The **worker** consumes the queue and runs the triage pipeline: embedding and similarity search, classification with an optional Claude fallback, assignment, persistence, and GitHub write-back. PostgreSQL holds all durable state, including the vector index. Redis holds the work queue and the counters behind the LLM budgets and circuit breakers. The **MCP server** is a separate process that reads PostgreSQL through a read-only session.

### GitHub Issue Processing

```mermaid
flowchart TD
    A["GitHub: issue opened or closed"] --> B["POST /webhook/github"]
    B --> C{"HMAC-SHA256 signature valid?"}
    C -- No --> C1["401 Invalid signature"]
    C -- Yes --> D{"issues event, action opened or closed?"}
    D -- No --> D1["202 ignored"]
    D -- Yes --> E{"delivery_id already recorded?"}
    E -- Yes --> E1["202 duplicate"]
    E -- No --> F["Insert webhook_delivery row<br/>LPUSH NormalizedEvent to buma:triage:queue"]
    F --> G["Worker BRPOP + schema validation"]
    G --> H{"Repository enrolled?"}
    H -- No --> H1["Skip"]
    H -- Yes --> I{"Action"}
    I -- closed --> CL1["Mark embedding closed<br/>Set closed_at on latest decision<br/>Decrement assignee open_assignments"]
    I -- opened --> J["Embed issue, find similar issues,<br/>upsert embedding (failures isolated)"]
    J --> K["Rule-based classification"]
    K --> L{"Confidence below threshold<br/>and Claude configured?"}
    L -- No --> R["Rule result"]
    L -- Yes --> M{"Budget and circuit breaker allow?"}
    M -- No --> R
    M -- Yes --> N["Claude classify_issue tool call"]
    N --> O{"Valid response?"}
    O -- No --> R
    O -- Yes --> P["Claude result<br/>priority capped at CLAUDE_MAX_PRIORITY"]
    R --> S{"Category is bug?"}
    P --> S
    S -- No --> S1["Stop: not persisted, no GitHub update"]
    S -- Yes --> T["Select assignee + persist IssueSnapshot and TriageDecision<br/>(one transaction)"]
    T --> U{"GitHub App configured?"}
    U -- No --> U1["patch_state = DECIDED"]
    U -- Yes --> V["PATCH labels + assignee, POST comment"]
    V -- success --> W["patch_state = APPLIED"]
    V -- HTTP error --> X["patch_state = FAILED_RETRY<br/>non-transient errors also written to dlq_records"]
```

The gateway does only cheap work (verify, deduplicate, enqueue) and returns `202` straight away. The worker does the rest. Embedding runs for every opened issue in an enrolled repository, before the bug check, so later bug reports can match issues filed under other categories. Only issues classified as `bug` are persisted, assigned, and written back to GitHub. Claude is consulted only on the low-confidence path, and every failure on that path returns the rule result.

### Agentic RAG Assistant Architecture

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant UI as React Assistant page
    participant GW as Gateway POST /api/chat/repo_id
    participant AG as ChatAgent loop
    participant CL as Claude API
    participant T as Read-only tools
    participant DB as PostgreSQL + pgvector (read-only session)

    User->>UI: Question about the selected repository
    UI->>GW: message + prior turns, Bearer JWT
    GW->>GW: Auth, repo exists, chat enabled, chat budget allows
    GW-->>UI: text/event-stream opened
    loop At most CHAT_MAX_TOOL_ROUNDS model turns
        AG->>CL: System prompt + messages + 5 tool definitions (streamed)
        CL-->>AG: Text deltas and/or tool_use blocks
        AG-->>UI: SSE "text" and "tool" events
        alt Claude requested tools
            AG->>T: Validate arguments, run with repo_id fixed by the server
            T->>DB: Semantic or keyword search, snapshots, decisions, workload
            DB-->>T: Rows scoped to this repository
            T-->>AG: JSON results, GitHub text in untrusted_* fields
            AG->>CL: tool_result blocks appended to the conversation
        else Final answer, refusal, or error
            AG->>AG: Leave loop
        end
    end
    AG-->>UI: SSE "sources" (cited issues that tools returned)
    AG-->>UI: SSE "done"
```

A question starts a bounded tool-use loop in the gateway. In each round Claude either answers or asks for one or more of five read-only tools. The gateway validates the arguments, runs the queries against a read-only database session for the repository in the URL, and returns the results to Claude as context. Text streams to the browser while it is generated. The loop ends at a final answer, a refusal, an API error, or the round limit. A `sources` event then lists only the issues that the answer cites **and** that a tool actually returned.

### MCP Architecture

```mermaid
flowchart LR
    Client["MCP client<br/>Claude Code / Claude Desktop"] -- "MCP over stdio" --> Server

    subgraph Server["buma.mcp_server (MCPServer, official MCP Python SDK)"]
        direction TB
        R1["Resource: buma://repos"]
        T1["Tool: get_triage_history(repo_id, limit)"]
        T2["Tool: get_workload(repo_id)"]
        U["untrusted_text()<br/>sanitize + truncate + label"]
    end

    subgraph RO["Read-only boundary"]
        direction TB
        QL["observability_queries.py<br/>SELECT-only shared query layer"]
        S["Session options:<br/>default_transaction_read_only=on<br/>statement_timeout=5000 ms"]
    end

    R1 --> QL
    T1 --> QL
    T2 --> QL
    T1 --> U
    QL --> S --> PG[("PostgreSQL")]
```

MCP provides a standardized interface through which AI clients can discover and use Buma capabilities and data. The client launches the Buma MCP server as a local subprocess and talks to it over stdio. The server exposes one resource for finding repositories and two read-only tools. Both tools call the same query functions as the REST API, on connections that PostgreSQL itself keeps read-only.

### Semantic Retrieval Architecture

```mermaid
flowchart TD
    subgraph Ingest["Indexing path (worker)"]
        W1["issues.opened event"] --> W2["title + first EMBEDDING_MAX_CHARS of body"]
        B1["backfill_embeddings CLI<br/>(GitHub issues API, PRs skipped)"] --> W2
        W2 --> W3["EmbeddingService.embed<br/>fastembed, 384-dim"]
        W3 --> W4["find_similar: top-k in same repo + model,<br/>excluding the issue itself"]
        W3 --> W5["Upsert issue_embeddings<br/>key (repo_id, issue_number)"]
        W4 --> W6["Nearest matches logged<br/>optional duplicate line in comment"]
        C1["issues.closed event"] --> C2["issue_state = closed<br/>(row kept)"]
    end

    W5 --> IDX[("issue_embeddings<br/>vector(384) + HNSW vector_cosine_ops")]
    C2 --> IDX

    subgraph Query["Query path (Ask Buma, gateway)"]
        Q1["search_issues(query)"] --> Q2{"Embedding model loaded?"}
        Q2 -- Yes --> Q3["EmbeddingService.embed_query"]
        Q3 --> Q4["Cosine search filtered by<br/>repo_id and model_version"]
        Q2 -- No --> Q5["Keyword fallback: Postgres full-text search<br/>over issue_snapshot title + body"]
        Q4 --> Q6["Join latest issue_snapshot + triage_decision"]
        Q5 --> Q6
        Q6 --> Q7["untrusted_* fields returned to Claude as tool_result"]
    end

    IDX --> Q4
```

Issues are embedded when they are opened, or in bulk by the backfill command, and stored one vector per issue. The worker uses the index to find likely duplicates. The assistant uses the same index, with the same model, to retrieve issues relevant to a question. If the embedding model is unavailable in the gateway, retrieval falls back to PostgreSQL full-text search.

---

## Components

| Component | Location | Responsibility |
|---|---|---|
| **Gateway** | `src/buma/gateway/` | FastAPI app (`buma.gateway.app:app`). Webhook ingestion, GitHub OAuth token exchange, config and observability REST API, Ask Buma SSE endpoint, and debug-only dev routes. |
| **Ingest service** | `gateway/services/ingest.py` | Filters events, records `webhook_delivery` (unique `delivery_id`), builds a `NormalizedEvent`, and publishes it to Redis. |
| **Observability query layer** | `gateway/services/observability_queries.py` | The single, SELECT-only query path shared by the REST routes, the MCP server, and the chat tools. |
| **Chat assistant** | `gateway/chat/` | `agent.py` (bounded tool loop), `tools.py` (tool schemas and handlers), `runtime.py` (Anthropic client, read-only engine, lazily loaded embedding model, budget). |
| **Worker** | `src/buma/worker/` | `runner.py` wires dependencies and handles `SIGINT`/`SIGTERM`. `consumer.py` pops and validates events. `services/event_processor.py` runs the pipeline. |
| **Triage engine** | `worker/services/triage_engine.py`, `category_rules.py`, `priority_rules.py` | Rule-based classification and the Claude fallback orchestration. |
| **Claude classifier** | `worker/services/claude_client.py` | Hardened prompt, forced `classify_issue` tool call, and response validation. |
| **LLM budget** | `worker/services/llm_budget.py` | Redis per-repo daily budget and circuit breaker, namespaced separately for triage (`llm`) and chat (`chat`). |
| **Assignee selector** | `worker/services/assignee_selector.py` | Skill- and capacity-based selection with optimistic locking. |
| **Embeddings and duplicates** | `worker/services/embedding_service.py`, `duplicate_detector.py`, `worker/backfill_embeddings.py` | Local embedding model, pgvector search and upsert, and the backfill CLI. |
| **GitHub client** | `worker/services/github_client.py` | GitHub App JWT (RS256), installation tokens, issue patch, comments, and paginated issue listing. |
| **MCP server** | `src/buma/mcp_server/` | stdio MCP server with its own minimal settings, a read-only engine, and untrusted-text handling. |
| **Data model** | `src/buma/db/models.py`, `migrations/` | SQLAlchemy 2.0 models and Alembic migrations. |
| **Contracts** | `src/buma/schemas/` | `NormalizedEvent` (the gateway-to-worker queue contract, `schema_version` `1.0`) and the API request/response schemas. |
| **Dashboard** | `web-dashboard/` | React 19 (Create React App) with MUI, Recharts, React Router, and Axios. |

---

## Triage Pipeline

### Event ingestion

- Only `issues` events with action `opened` or `closed` are processed. All other events return `202` with status `ignored`.
- Each delivery is recorded in `webhook_delivery`. A repeated `X-GitHub-Delivery` ID returns `202` with status `duplicate` and is not re-queued.
- The gateway pushes the event onto the Redis list `buma:triage:queue` (`LPUSH`), and the worker consumes it with `BRPOP`. Messages that fail `NormalizedEvent` validation are logged and dropped. Errors inside the consumer loop are logged, and the loop keeps running.
- Events for repositories without a `repo_config` row are skipped by the worker.

### Rule-based classification

Categories: `bug`, `feature`, `question`, `security`, `docs`. Priorities: `P0` (most severe) to `P3`.

| Step | Category | Priority |
|---|---|---|
| 1. Issue labels | Label map (global defaults merged with per-repo `label_map.categories`), confidence **1.0** | Label map (global defaults merged with per-repo `label_map.priorities`), confidence **1.0** |
| 2. Keywords in title + body | First matching phrase group wins: strong bug phrases **0.9**, medium bug phrases **0.7**, other categories **0.7** | All tiers scanned and the most severe match wins: P0/P1 **0.9**, P2/P3 **0.7** |
| 3. Fallback | Repo default (`defaults.category`, normally `bug`), confidence **0.0** | Repo default (`defaults.priority`, normally `P2`), confidence **0.0** |

Overall confidence is the lower of the two non-zero confidences, or `0.0` when neither category nor priority matched. With these confidence values and the default threshold of `0.5`, the Claude fallback runs only for issues where **no label or keyword matched for either category or priority**.

### Assignment

Only `bug` issues are assigned. Candidates are the repository's developers who have `bug` in their `skills` and `open_assignments < max_capacity`, least-loaded first. A developer is claimed with a conditional update on the `version` column (optimistic locking), which increments `open_assignments`. If no candidate can be claimed, the decision is recorded with no assignee. Assignment and the `IssueSnapshot`/`TriageDecision` inserts commit in one transaction. A unique `event_id` makes reprocessing the same event a no-op.

When an issue is closed, the worker sets `closed_at` on its latest decision and decrements the assignee's `open_assignments`. These `closed_at` values feed the productivity metrics.

### GitHub write-back

If `GITHUB_APP_ID` and `GITHUB_APP_PRIVATE_KEY` are set, the worker requests an installation token and then:

1. `PATCH`es the issue with its existing labels plus the category and priority labels (for example `bug` and `P1`), and the assignee.
2. Posts an explanation comment containing category, priority, assignee, confidence, engine version, an optional system note (for example a capped priority), and an optional possible-duplicate line.

| `patch_state` | Meaning |
|---|---|
| `DECIDED` | Decision persisted. GitHub was not updated (no GitHub App configured, or the patch did not run). |
| `APPLIED` | Labels, assignee, and comment applied. |
| `FAILED_RETRY` | GitHub returned an HTTP error. `patch_attempts` and `last_error` are recorded. Non-transient errors (not 429 and not 5xx) also create a `dlq_records` row with `error_type = GITHUB_PATCH`. |

The worker does not automatically re-attempt failed patches. `FAILED_RETRY` rows and `dlq_records` are the record of what needs attention.

---

## AI Capabilities

Buma uses Claude in exactly two places: the triage fallback in the worker and the Ask Buma assistant in the gateway. Each has its own budget and circuit breaker. Embeddings are computed locally and never call an external API.

### Hybrid Claude Classification

**Flow:**

1. An issue event reaches the worker, and the rule engine classifies it.
2. If `ANTHROPIC_API_KEY` is set **and** rule confidence is below `CLAUDE_CONFIDENCE_THRESHOLD` (default `0.5`), the triage `LLMBudget` is checked (per-repo daily limit and circuit breaker).
3. `ClaudeClassifier` calls the Messages API (`CLAUDE_MODEL`, default `claude-haiku-4-5-20251001`, `max_tokens=256`). The call sends the title, labels, and truncated body inside `<untrusted_issue>` tags, with `tool_choice` forcing the `classify_issue` tool.
4. The `classify_issue` input schema constrains `category` and `priority` to the same enums the rule engine uses, and `confidence` to 0.0–1.0.
5. The response is validated: a `tool_use` block must exist, the tool name must be `classify_issue`, the input must be an object, the values must be in their enums, and `confidence` must be a number in range (booleans are rejected).
6. A valid result more severe than `CLAUDE_MAX_PRIORITY` (default `P1`) is capped, and the comment states that the AI-suggested priority needs human confirmation.
7. The result then continues through the normal pipeline (bug check, assignment, persistence, GitHub update).

**Outcome tracking.** Each result carries an `engine_version`, which is written into the persisted explanation text and the GitHub comment, and appears in worker logs:

| `engine_version` | Meaning |
|---|---|
| `rules-v1` | Rule confidence met the threshold (or Claude is not configured). Claude was not called. |
| `claude-hybrid-v1` | Claude answered and passed validation. |
| `rules-v1-fallback` | Claude was attempted but failed, timed out, or returned invalid data. The rule result was used. |
| `rules-v1-budget` | Claude was skipped by the daily budget or an open circuit breaker. The rule result was used. |

**Failure handling.** `ClaudeClassifier.classify()` never raises. Rate limits, timeouts, connection errors, 4xx/5xx responses, and invalid output are logged by type with `event_id` and issue number, and return `None`. `classify_with_fallback()` wraps the call in a second exception guard. The SDK retries transient errors up to `CLAUDE_MAX_RETRIES` times, each attempt bounded by `CLAUDE_TIMEOUT_SECONDS`. Every outcome is recorded against the circuit breaker. Without an API key, triage is purely rule-based.

### Prompt-Injection Protection and Cost Controls

Issue titles, bodies, labels, and any text derived from them are treated as untrusted input throughout.

**Input protection (triage fallback)**
- The body is truncated at `CLAUDE_MAX_BODY_CHARS` (default 4000) and marked `[truncated]`.
- Title, labels, and body are wrapped in `<untrusted_issue>` tags. Any opening or closing form of that tag in the input is replaced (case- and whitespace-insensitive), so the content cannot close the data block.
- The system prompt states that tagged content is data and must not be followed, including requests to pick a category or priority.

**Output protection (triage fallback)**
- Forced tool choice, a tool-name check, type checks, enum checks, and a range check (see above). Any failure falls back to the rule result.
- A severity ceiling (`CLAUDE_MAX_PRIORITY`) stops a valid but injected "P0" from a Claude-only answer. Rule-engine priorities are never capped.
- Free text from the model is never posted to GitHub. The comment is built only from validated enum values and Buma's own strings.

**Untrusted data returned to models (MCP and Ask Buma)**
- GitHub-authored or derived text is returned only in fields whose names start with `untrusted_`, as `{text, truncated}`. Control, zero-width, and bidi-override characters are stripped, and length limits are fixed (titles 200 characters, explanations 500, chat bodies 1500, search excerpts 300).
- Every payload carries a `data_notice`, and the MCP server instructions and the chat system prompt both tell the model to treat these fields as data only.
- These measures lower the likelihood that a model follows injected text. The hard boundary is that no MCP or chat tool can write anything.

**Cost and reliability controls (`LLMBudget`, Redis)**

| Control | Triage fallback (`buma:llm_*`) | Ask Buma (`buma:chat_*`) |
|---|---|---|
| Daily limit per repo (UTC day) | `CLAUDE_DAILY_CALL_LIMIT_PER_REPO` (200 calls) | `CHAT_DAILY_QUESTION_LIMIT_PER_REPO` (100 questions) |
| Circuit breaker | `CLAUDE_BREAKER_THRESHOLD` consecutive failures (5) open it for `CLAUDE_BREAKER_COOLDOWN_SECONDS` (300 s) | `CHAT_BREAKER_THRESHOLD` (5) / `CHAT_BREAKER_COOLDOWN_SECONDS` (300 s). Only `llm_unavailable` outcomes count as failures |
| When the gate refuses | Rule result used (`rules-v1-budget`) | HTTP `429` before streaming starts |
| Per-request bounds | `max_tokens=256`, timeout, retries | `CHAT_MAX_TOOL_ROUNDS`, `CHAT_MAX_TOKENS`, `CHAT_TIMEOUT_SECONDS` |

The budget is counted before the call, because a call that fails may still be billed. If Redis is unavailable the gate **fails closed**: it refuses the call rather than allowing unmetered spend. The two namespaces are independent, so chat usage cannot exhaust the triage budget or trip the triage breaker.

**Testing.** `tests/worker/test_claude_parse_guardrail.py` sends hostile model outputs through response validation. `tests/worker/test_claude_client.py`, `test_triage_engine.py`, and `test_llm_budget.py` cover prompt construction, fallback paths, the severity ceiling, budgets, and the breaker. `tests/eval/test_prompt_injection_live.py` is an opt-in red-team evaluation. It runs the adversarial issues in `tests/fixtures/injection_attempts.json` against the live model with the baseline and hardened prompts and reports the injection success rate for each.

### Semantic Duplicate Detection

Keyword rules find issues that share words. Embeddings find issues that describe the same problem in different words ("app crashes at sign-in" and "login throws exception"), which is what duplicate detection and topical search need. Buma uses semantic similarity for these two purposes. Rule-based classification still uses labels and keywords.

- **Model.** `fastembed` with `BAAI/bge-small-en-v1.5` (ONNX, CPU, 384 dimensions). The worker loads it once at startup. The Docker image bakes it into `/opt/fastembed_cache`, so there is no download at runtime. Inference runs in a thread (`asyncio.to_thread`).
- **Text.** Issue title plus the first `EMBEDDING_MAX_CHARS` (default 2000) characters of the body.
- **Lifecycle.** On `opened`, the issue is embedded, its top `DUPLICATE_TOP_K` (default 5) nearest neighbours are queried, and its vector is upserted. On `closed`, `issue_state` becomes `closed` and the vector is kept, because closed issues are valid duplicate targets. Only `opened` and `closed` events are ingested, so title or body edits are not re-embedded and reopened issues keep the `closed` state.
- **Scoping.** Every similarity query filters on `repo_id` and `model_version` and excludes the issue itself. Vectors from different models are never compared.
- **Failure isolation.** Embedding and search run in their own database session, and any error is logged without affecting triage. If the model fails to load, the feature is disabled and the worker runs normally.
- **Flagging.** Nearest-neighbour scores are always logged. When `DUPLICATE_COMMENT_ENABLED=true`, matches with similarity ≥ `DUPLICATE_SIMILARITY_THRESHOLD` are added to the explanation comment as issue numbers, states, and scores only. Other issues' text is never reposted, and nothing is closed or relabelled. Comment flagging is **disabled by default**. The default threshold (`0.9`) is a starting value and should be calibrated against labelled duplicate pairs from the target repositories before flagging is enabled.
- **Backfill.** Issues created before enrollment can be indexed from the GitHub API. The command is idempotent. See [Running the Application](#running-the-application).

### Ask Buma: Agentic RAG Assistant

Ask Buma answers questions such as *"Are there open issues about login?"*, *"Who has spare capacity?"*, or *"Who resolved the most issues in the last 30 days?"* about one enrolled repository.

**Why it is RAG.** The assistant does not answer from the model's own knowledge. For each question, Claude requests Buma data through tools. The gateway retrieves that data from PostgreSQL (semantic vector search over `issue_embeddings`, plus relational queries over snapshots, decisions, and developer profiles) and passes it back to Claude as `tool_result` context. The answer is generated from that context. The system prompt tells the model not to answer data questions from memory and to cite only issue numbers that appeared in tool results. The server enforces the citation rule for the `sources` list.

**Why it is agentic.** Buma implements a bounded agentic tool-calling workflow with explicit tool and execution limits. Claude analyzes the question, chooses which tools to call, inspects the results, and may call more tools, for example `search_issues` and then `get_issue` on a promising hit, before writing the final answer. The loop is bounded and read-only. It is not an unrestricted autonomous agent.

**Tools** (`gateway/chat/tools.py`)

| Tool | Arguments | Returns |
|---|---|---|
| `search_issues` | `query` (1–300 chars), `limit` (1–10, default 5) | Ranked issues with state, similarity, title, excerpt, and latest triage decision. Semantic search when the embedding model is available, otherwise PostgreSQL full-text search. `search_mode` reports which ran |
| `get_issue` | `issue_number` | Latest snapshot (title, truncated body, labels, author, dates) and latest decision (category, priority, confidence, assignee, patch state, explanation, `closed_at`) |
| `get_recent_triage` | `limit` (1–20, default 10) | Most recent decisions with titles, plus the repo's total decision count |
| `get_workload` | none | Each developer's open assignments, capacity, spare capacity, at-capacity flag, and skills |
| `get_productivity` | `window` (`7d`, `30d`, `90d`, `all`) | Per-developer resolved count and average resolution time in hours |

Input schemas are generated from pydantic models with `extra="forbid"`, and the same models validate every call before a query runs. Invalid input, or an unknown tool, is returned to Claude as an `is_error` tool result so it can correct itself.

**Agent loop** (`gateway/chat/agent.py`)
- Each round streams a Messages API turn (`CHAT_MODEL`, default `claude-opus-5`, at `CHAT_EFFORT`, default `medium`) with the system prompt, the conversation, and the five tool definitions. Server-side refusal fallbacks are enabled through the API's beta.
- Tool calls within a round run sequentially on one read-only session, and all results are returned in one message.
- The loop stops on a final answer, `stop_reason = refusal`, an API error, a `max_tokens` cut-off (a truncated tool call is never executed), or after `CHAT_MAX_TOOL_ROUNDS` (default 6) model turns. An unparseable streamed tool input causes the turn to be re-issued once.
- A database error in a tool is rolled back and reported to the model as "data temporarily unavailable", and the loop continues.
- The loop never raises into the HTTP response. Every outcome is an event, and the stream always ends with `done`.

**Streaming (SSE).** `POST /api/chat/{repo_id}` returns `text/event-stream`, with one JSON object per `data:` line:

| Event | Payload |
|---|---|
| `tool` | `name`: a tool call started (the UI shows progress chips) |
| `text` | `text`: answer delta |
| `sources` | `issues`: `[{issue_number, title}]` cited in the answer **and** returned by a tool |
| `error` | `code` (`llm_unavailable`, `refused`, `truncated`, `round_limit`, `internal`) and `message` |
| `done` | Always last |

The dashboard reads the stream with `fetch` and a `ReadableStream` reader, renders `#N` references as GitHub links, and can cancel a request with an `AbortController`. The server keeps no conversation state. The browser sends up to 20 prior plain-text turns with each question.

**Boundaries**
- The repository comes from the URL and is held in the server-side `ToolContext`. No tool accepts a repository argument.
- Tools read only through `observability_queries.py`, on an engine whose connections set `default_transaction_read_only=on` and `statement_timeout=5000`.
- Request validation: questions are limited to 2000 characters, history to 20 turns of at most 8000 characters each.
- Checks before streaming: `401` without a valid session, `404` for an unknown repository, `503` if the assistant is disabled (`CHAT_ENABLED=false` or no `ANTHROPIC_API_KEY`), `429` when the chat budget or breaker refuses.
- The query embedding model (about 100 MB) loads lazily on the first question in each gateway process. If it cannot load, or `CHAT_SEMANTIC_SEARCH_ENABLED=false`, `search_issues` uses keyword search.

### MCP Integration

MCP provides a standardized interface through which AI clients can discover and use Buma capabilities and data. Buma's MCP server (`python -m buma.mcp_server`) is built on the official MCP Python SDK (`MCPServer`) and uses the **stdio** transport. The client launches it as a local process that runs with the user's own database credentials.

| Kind | Name | Returns |
|---|---|---|
| Resource | `buma://repos` | JSON list of enrolled repositories: `repo_id`, `repo_full_name`, `enrolled_at`. Installation IDs and configuration are not exposed |
| Tool | `get_triage_history(repo_id, limit=20)` | Newest-first decisions (`limit` 1–50): category, priority, confidence, assignee, `patch_state`, `closed_at`, `untrusted_issue_title`, `untrusted_explanation`, plus the repository's total decision count. Issue bodies are never returned |
| Tool | `get_workload(repo_id)` | Each developer's skills, open assignments, capacity, available capacity, at-capacity flag, plus totals |

**Read-only by construction**
- Only read tools are registered, each annotated `readOnlyHint=True`, `destructiveHint=False`, `idempotentHint=True`, `openWorldHint=False`.
- Database sessions use `default_transaction_read_only=on`, so PostgreSQL rejects writes. `statement_timeout=5000` ms and `connect_timeout=5` s bound slow queries and outages.
- All data access goes through the SELECT-only `observability_queries.py` layer shared with the REST API.

**Isolation and error handling.** The server reads only `BUMA_MCP_DATABASE_URL` (falling back to `DATABASE_URL`) through its own `MCPSettings`. The webhook secret, GitHub App key, and Anthropic key are never loaded into the process. It does not import worker, Claude, GitHub, or embedding code. Database failures are logged to stderr and returned as a generic tool error. An unknown `repo_id` returns an error pointing to `buma://repos`. stdout carries only protocol messages.

---

## Web Dashboard

The dashboard (`web-dashboard/`) is a React 19 single-page app built with Create React App, MUI, Recharts, React Router, and Axios. It talks only to the gateway (`REACT_APP_API_URL`, default `http://localhost:8000`) and sends the session JWT as `Authorization: Bearer <token>`.

| Page | Route | Backend calls |
|---|---|---|
| Login / OAuth callback | `/login`, `/auth/callback` | `GET /auth/github` redirect, then `POST /api/v1/auth/github` (code → JWT) |
| Home | `/dashboard` | `GET /api/triage/{repo_id}`: summary cards, trends, category breakdown, recent activity |
| Repositories | `/repositories` | `GET /api/config/repos` |
| Setup | `/setup` | `POST /api/config/repos`: enroll a repo with GitHub repo ID, installation ID, full name, and label mappings |
| People | `/team` | `GET /api/workload/{repo_id}`, add/edit/remove developers under `/api/config/repos/{repo_id}/developers` |
| Issues | `/issues` | `GET /api/issues/{repo_id}` (persisted issue snapshots) |
| Productivity | `/productivity` | `GET /api/productivity/{repo_id}?window=` |
| Ask Buma | `/assistant` | `GET /api/chat/status`, `POST /api/chat/{repo_id}` (SSE) |

Sign-in uses GitHub OAuth. The gateway's `/api/v1/auth/login` email/password endpoint returns `501`.

---

## API Reference

Interactive OpenAPI docs are served at `http://localhost:8000/docs` while the gateway is running.

| Method | Route | Auth | Description |
|---|---|---|---|
| `GET` | `/health` | none | Liveness check (`{"status":"ok","service":"buma"}`) |
| `POST` | `/webhook/github` | HMAC | GitHub webhook receiver. Returns `202` with `queued`, `duplicate`, or `ignored` |
| `GET` | `/auth/github` | none | Redirect to GitHub OAuth authorization (`read:user` scope) |
| `GET` | `/auth/callback` | none | Server-side code exchange that sets a `buma_session` cookie |
| `POST` | `/auth/logout` | none | Clears the session cookie |
| `POST` | `/api/v1/auth/github` | none | Exchange an OAuth code for a Buma JWT (used by the dashboard) |
| `GET` / `POST` | `/api/config/repos` | Bearer | List (paginated) / enroll repositories |
| `GET` / `PATCH` | `/api/config/repos/{repo_id}` | Bearer | Read / update repository config (`label_map`, `defaults`) |
| `POST` | `/api/config/repos/{repo_id}/developers` | Bearer | Add a developer (`github_login`, `skills`, `max_capacity`) |
| `PATCH` / `DELETE` | `/api/config/repos/{repo_id}/developers/{github_login}` | Bearer | Update / remove a developer |
| `GET` | `/api/triage/{repo_id}` | Bearer | Triage decisions, newest first (`limit` ≤ 500, `offset`) |
| `GET` | `/api/issues/{repo_id}` | Bearer | Issue snapshots, newest first (`limit` ≤ 500, `offset`) |
| `GET` | `/api/workload/{repo_id}` | Bearer | Developer workload, busiest first |
| `GET` | `/api/productivity/{repo_id}` | Bearer | Resolved counts, average resolution hours, and zero-filled time buckets (`window` = `7d`, `30d`, `90d`, `all`) |
| `GET` | `/api/chat/status` | Bearer | `{enabled, model}` |
| `POST` | `/api/chat/{repo_id}` | Bearer | Ask Buma. Body `{message, history}`, response is SSE |
| `POST` | `/dev/sign-webhook`, `/dev/session` | `DEBUG=true` only | Sign a test webhook payload / mint a session. Return `404` unless `DEBUG=true` |

Developer `skills` must be a subset of the issue categories (`bug`, `feature`, `question`, `security`, `docs`). `max_capacity` is 1–100 (default 5).

---

## Security and Reliability

### Authentication

- **Webhooks.** Each request body is verified against `X-Hub-Signature-256` using HMAC-SHA256 with `GITHUB_WEBHOOK_SECRET` and a constant-time comparison. Invalid signatures return `401` before any parsing or database work.
- **Dashboard sessions.** Users sign in with a GitHub OAuth App (`read:user` scope). The gateway exchanges the code for the user's GitHub login and issues an HS256 JWT signed with `SESSION_SECRET`, valid for 8 hours. The dashboard stores the JWT in `localStorage` and sends it as a Bearer token. Every `/api/config/*`, `/api/triage|issues|workload|productivity/*`, and `/api/chat/*` route requires a valid token.
- **GitHub App.** Write-back uses a short-lived RS256 app JWT exchanged for a per-installation access token on each event.
- **Dev endpoints.** `/dev/*` return `404` unless `DEBUG=true`.

### Data isolation

- **Repository scoping.** Every observability, retrieval, and similarity query filters by `repo_id`. The assistant's repository is fixed by the server from the request URL, and MCP tools require an explicit, validated `repo_id`.
- **Authorization model.** A valid session identifies a GitHub user. The API does not restrict which enrolled repositories that user can read or configure: any signed-in user can access every enrolled repository. Deployments should limit who can obtain a session (for example, by restricting access to the OAuth App or the dashboard).
- **Read-only AI access.** The Ask Buma tools and the MCP server use database connections with `default_transaction_read_only=on` and a 5-second statement timeout. No AI-facing tool writes, configures, or triggers anything.

### AI security

- Untrusted GitHub text is fenced and truncated on the way into Claude, and labelled and sanitized on the way out to MCP and chat models ([details](#prompt-injection-protection-and-cost-controls)).
- Structured outputs are validated before use. Classification output is limited to enum values, and a Claude-only priority is capped at `CLAUDE_MAX_PRIORITY`.
- Chat tool arguments are validated, and repository scope cannot be influenced by the model.
- Chat citations are checked against the issues that tools actually returned.

### Reliability

| Mechanism | Behaviour |
|---|---|
| Queue decoupling | Webhook ingestion never waits on triage, the GitHub API, or Claude. |
| Idempotency | Unique `webhook_delivery.delivery_id` at ingestion. Unique `event_id` on `issue_snapshot` and `triage_decision`, so a replayed event cannot create a second decision or a second assignment. |
| Concurrency | Optimistic locking (`developer_profile.version`) when claiming an assignee. |
| LLM failure | Triage falls back to rules. Chat emits an `error` event and ends with `done`. Both paths feed their circuit breakers. |
| LLM cost | Independent per-repo daily limits and circuit breakers. The gate fails closed if Redis is unavailable. |
| Bounded agent | Round limit, `max_tokens` per turn, request timeout, one re-issue for malformed tool input. |
| Embedding failure | Isolated from triage. The worker disables the feature if the model does not load, and chat falls back to keyword search. |
| GitHub failure | `FAILED_RETRY` with `last_error` and attempt count. Non-transient failures are also recorded in `dlq_records`. |
| Shutdown | `SIGINT`/`SIGTERM` set a stop event, and the worker finishes the current message before exiting. |

**Queue semantics.** The queue is a Redis list: a message is removed when popped, and a worker crash during processing loses that message. Every accepted delivery is recorded in `webhook_delivery`, and GitHub keeps delivery history for manual redelivery.

---

## Data and Retrieval Architecture

Buma keeps three kinds of state:

| Kind | Store | Contents |
|---|---|---|
| Operational relational data | PostgreSQL | Deliveries, repo config, developer profiles, issue snapshots, triage decisions, dead-letter records |
| Queue and control state | Redis | `buma:triage:queue` (list). LLM budget counters `buma:{llm,chat}_calls:{repo_id}:{YYYY-MM-DD}` (48-hour TTL), failure counters, and breaker flags with a TTL |
| Vector / semantic data | PostgreSQL + pgvector | `issue_embeddings`, one 384-dim vector per issue, with an HNSW index |

### Tables

| Table | Key | Purpose |
|---|---|---|
| `webhook_delivery` | unique `delivery_id` | Ingestion log and idempotency |
| `repo_config` | `repo_id` (GitHub repo ID) | Enrollment: `installation_id`, `repo_full_name`, JSONB `config` (`label_map`, `defaults`) |
| `developer_profile` | unique `(repo_id, github_login)` | `skills` (categories), `max_capacity`, `open_assignments`, `version` |
| `issue_snapshot` | unique `event_id` | Issue title, body, labels, author, and timestamps as received (bugs only) |
| `triage_decision` | unique `event_id` | Category, priority, confidence, assignee, explanation, `patch_state`, `patch_attempts`, `last_error`, `closed_at` |
| `issue_embeddings` | `(repo_id, issue_number)` | `embedding vector(384)`, `model_version`, `issue_state` (`open`/`closed`) |
| `dlq_records` | unique `event_id` | Failed events with payload, `error_type`, and `status` |

`developer_profile`, `issue_snapshot`, `triage_decision`, and `issue_embeddings` reference `repo_config` with `ON DELETE CASCADE`.

### Derived views

- **Triage history**: `triage_decision` by repository, newest first.
- **Workload**: `developer_profile.open_assignments` against `max_capacity`, kept current by assignment (increment) and close events (decrement).
- **Productivity**: resolved count and average `closed_at − decided_at` per assignee over `7d`, `30d`, `90d`, or `all` (12 months), with zero-filled day, week, or month buckets.
- **Semantic search**: cosine distance (`<=>`, via pgvector's SQLAlchemy comparator) over `issue_embeddings`, filtered by `repo_id` and `model_version`, joined to each issue's latest snapshot and decision with `DISTINCT ON`.
- **Keyword search** (chat fallback): `to_tsvector('english', title || body) @@ websearch_to_tsquery(...)` ranked by `ts_rank`. The query words are OR-ed together, so arbitrary input cannot cause a query error.

### Why pgvector

Embeddings sit next to the relational data they describe. One PostgreSQL instance holds vectors, snapshots, and decisions, so a repository-scoped similarity query and the join to the latest snapshot and decision run against the same database with the same transactions, backups, and migrations. No separate vector store needs to be operated. The HNSW index with `vector_cosine_ops` provides approximate nearest-neighbour search. The index covers all repositories and the `repo_id`/`model_version` filter is applied to its candidates, so a small repository in a large multi-repository index can receive fewer than `k` results.

### Migrations

Alembic manages the schema (`migrations/versions/`), applied in order:

| Revision | Change |
|---|---|
| `e60ad1eb0a30` | Initial schema: all core tables, indexes, constraints |
| `770ceda6d5a8` | Removes the sequence default from `repo_config.repo_id` (IDs are GitHub-assigned) |
| `c9f2a1b4e8d3` | `triage_decision.closed_at` |
| `a7d3e9f1c2b5` | `CREATE EXTENSION IF NOT EXISTS vector`, the `issue_embeddings` table, and the HNSW index `ix_issue_embeddings_embedding_hnsw` (`vector_cosine_ops`) |

The pgvector migration needs a PostgreSQL build that includes the extension. Docker Compose uses `pgvector/pgvector:pg16`. `migrations/env.py` reads `DATABASE_URL` from the environment or `.env`.

```bash
uv run alembic upgrade head          # apply
uv run alembic revision --autogenerate -m "describe change"   # new migration from model changes
```

---

## Configuration

Settings are read from environment variables or `.env` by `buma.core.config.Settings`. The MCP server reads only its own `MCPSettings`. Start from [`.env.example`](.env.example). Never commit `.env`.

### Core

| Variable | Required | Default | Used by | Purpose |
|---|---|---|---|---|
| `DATABASE_URL` | yes | — | gateway, worker, migrations | SQLAlchemy URL, e.g. `postgresql+psycopg://buma:buma@db:5432/buma` |
| `REDIS_URL` | no | `redis://localhost:6379/0` | gateway, worker | Queue and LLM budgets |
| `GITHUB_WEBHOOK_SECRET` | yes | — | gateway | HMAC secret configured on the GitHub App |
| `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY` | for write-back and backfill | unset | worker | GitHub App credentials. The PEM goes on one line with `\n` escapes. If unset, decisions stay `DECIDED` |
| `GITHUB_OAUTH_CLIENT_ID`, `GITHUB_OAUTH_CLIENT_SECRET` | for dashboard login | unset | gateway | GitHub OAuth App credentials |
| `SESSION_SECRET` | yes, outside local dev | development placeholder | gateway | JWT signing key. Set a strong random value |
| `CORS_ORIGINS` | no | `http://localhost:3000,http://localhost:5173` | gateway | Comma-separated allowed origins. Read from the process environment at startup |
| `DEBUG` | no | `false` | gateway | Enables `/dev/*` endpoints. Never enable in shared environments |
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | Docker only | — | `db` service | Database container initialization |

### Triage fallback (worker)

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | unset | Enables the Claude fallback (and Ask Buma in the gateway) |
| `CLAUDE_MODEL` | `claude-haiku-4-5-20251001` | Fallback classification model |
| `CLAUDE_CONFIDENCE_THRESHOLD` | `0.5` | Rule confidence below this triggers the fallback |
| `CLAUDE_TIMEOUT_SECONDS` | `8.0` | Per-attempt timeout |
| `CLAUDE_MAX_RETRIES` | `2` | SDK retries for transient errors |
| `CLAUDE_MAX_BODY_CHARS` | `4000` | Body truncation before sending |
| `CLAUDE_DAILY_CALL_LIMIT_PER_REPO` | `200` | Daily call budget per repository (UTC) |
| `CLAUDE_BREAKER_THRESHOLD` | `5` | Consecutive failures that open the breaker |
| `CLAUDE_BREAKER_COOLDOWN_SECONDS` | `300` | Breaker open duration |
| `CLAUDE_MAX_PRIORITY` | `P1` | Most severe priority a Claude-only result may set (`P0` disables the cap) |

### Embeddings and duplicate detection (worker; model settings also used by chat)

| Variable | Default | Purpose |
|---|---|---|
| `EMBEDDING_ENABLED` | `true` | Enables indexing and similarity search in the worker |
| `EMBEDDING_MODEL` | `BAAI/bge-small-en-v1.5` | fastembed model. Changing it requires `backfill_embeddings --force` |
| `EMBEDDING_MAX_CHARS` | `2000` | Body characters embedded |
| `EMBEDDING_CACHE_DIR` | unset (image sets `/opt/fastembed_cache`) | Model cache directory. Set it only for host-mode runs, and never to an empty value |
| `DUPLICATE_TOP_K` | `5` | Neighbours retrieved per new issue |
| `DUPLICATE_SIMILARITY_THRESHOLD` | `0.9` | Minimum cosine similarity for a flagged duplicate |
| `DUPLICATE_COMMENT_ENABLED` | `false` | Adds the possible-duplicate line to GitHub comments |

### Ask Buma (gateway)

| Variable | Default | Purpose |
|---|---|---|
| `CHAT_ENABLED` | `true` | The assistant also requires `ANTHROPIC_API_KEY` |
| `CHAT_MODEL` | `claude-opus-5` | Assistant model |
| `CHAT_EFFORT` | `medium` | `low`, `medium`, `high`, `xhigh`, or `max` |
| `CHAT_TIMEOUT_SECONDS` | `60` | Per-request timeout |
| `CHAT_MAX_TOKENS` | `8000` | Output token cap per model turn |
| `CHAT_MAX_TOOL_ROUNDS` | `6` | Maximum model turns per question |
| `CHAT_DAILY_QUESTION_LIMIT_PER_REPO` | `100` | Daily question budget per repository |
| `CHAT_BREAKER_THRESHOLD` / `CHAT_BREAKER_COOLDOWN_SECONDS` | `5` / `300` | Chat circuit breaker |
| `CHAT_SEMANTIC_SEARCH_ENABLED` | `true` | Load the embedding model in the gateway for semantic search. Set `false` to use keyword search only |

### Other

| Variable | Used by | Purpose |
|---|---|---|
| `BUMA_MCP_DATABASE_URL` | MCP server | Database URL reachable from where the MCP client launches the server (falls back to `DATABASE_URL`) |
| `REACT_APP_API_URL` | dashboard | Gateway base URL (default `http://localhost:8000`) |
| `BUMA_TEST_DATABASE_URL` | tests | Enables the PostgreSQL/pgvector integration tests |

---

## Local Development

### Prerequisites

| Tool | Needed for |
|---|---|
| Git | Cloning |
| Docker + Docker Compose | PostgreSQL (pgvector), Redis, and the containerized gateway and worker |
| Node.js 18+ and npm | The dashboard |
| Python 3.11+ and [`uv`](https://docs.astral.sh/uv/) | Host-mode backend, tests, lint, MCP server |
| A GitHub App and a GitHub OAuth App | Webhooks and write-back, and dashboard login ([setup guide](docs/user-guide.md)) |
| An Anthropic API key (optional) | Claude fallback and Ask Buma |
| A public HTTPS tunnel, e.g. ngrok (for live webhooks) | Letting GitHub reach the local gateway |

### Environment Configuration

```bash
git clone https://github.com/Zeeldesai12345/buma.git
cd buma
cp .env.example .env
```

Fill in `GITHUB_WEBHOOK_SECRET`, the GitHub App and OAuth App credentials, and a strong `SESSION_SECRET`. Optionally add `ANTHROPIC_API_KEY`. `.env.example` uses the Docker service hostnames `db` and `redis`. For host-mode processes, point `DATABASE_URL` at `localhost:5433` and `REDIS_URL` at `localhost:6379`. The [User Guide](docs/user-guide.md) walks through creating both GitHub apps, formatting the private key, and exposing the gateway with ngrok.

### Database Setup

Docker Compose runs migrations automatically through the one-shot `migrate` service. In host mode:

```bash
docker compose up db redis -d
export DATABASE_URL=postgresql+psycopg://buma:buma@localhost:5433/buma
uv sync --dev
uv run alembic upgrade head
```

If you have a development volume created with the earlier `postgres:16-alpine` image, recreate it with `docker compose down -v` before starting `pgvector/pgvector:pg16`. The two images use different text collations.

### Running the Application

**Full stack in Docker**

```bash
docker compose build
docker compose up          # add -d to run detached
curl http://localhost:8000/health
```

| Service | Image / command | Port | Depends on |
|---|---|---|---|
| `db` | `pgvector/pgvector:pg16` | host `5433` → `5432` | healthcheck: `pg_isready` |
| `redis` | `redis:7-alpine` | `6379` | healthcheck: `redis-cli ping` |
| `migrate` | `alembic upgrade head` (exits) | — | `db` healthy |
| `gateway` | `uvicorn buma.gateway.app:app` | `8000` | `db`, `redis` healthy, `migrate` completed |
| `worker` | `python -m buma.worker.runner` | — | `db`, `redis` healthy, `migrate` completed |

The application image (`Dockerfile`) is `python:3.11-slim`. It installs locked dependencies with `uv`, bakes the embedding model into `/opt/fastembed_cache`, and runs as the non-root user `buma`.

**Host mode** (infrastructure in Docker, services on the host)

```bash
docker compose up db redis -d
export DATABASE_URL=postgresql+psycopg://buma:buma@localhost:5433/buma
export REDIS_URL=redis://localhost:6379/0
uv sync --dev
uv run alembic upgrade head

uv run uvicorn buma.gateway.app:app --reload --port 8000   # terminal 1
uv run python -m buma.worker.runner                        # terminal 2
```

In host mode the embedding model is downloaded on first use. Set `EMBEDDING_CACHE_DIR` (for example `.fastembed_cache`) to keep it in a stable location.

**Dashboard**

```bash
cd web-dashboard
npm install
npm start                  # http://localhost:3000
```

**Embedding backfill** (index issues that existed before enrollment. Requires GitHub App credentials. Safe to re-run.)

```bash
docker compose run --rm worker uv run --no-sync python -m buma.worker.backfill_embeddings
# host mode, with options:
uv run python -m buma.worker.backfill_embeddings --repo owner/name   # one repo
uv run python -m buma.worker.backfill_embeddings --force             # re-embed everything
```

**Dev container.** `.devcontainer/` defines a VS Code dev container that reuses the Compose `db` and `redis` services and runs `uv sync --dev && uv run alembic upgrade head` on creation.

**Serverless gateway.** `vercel.json` and `api/index.py` expose the gateway app as a single Vercel function, with dependencies from `requirements.txt`. That file does not include `fastembed`, so set `CHAT_SEMANTIC_SEARCH_ENABLED=false` there. The worker and MCP server are not part of this deployment.

---

## MCP Setup

The MCP server runs on the machine of the MCP client, so its database URL must be reachable from there. The Docker database is exposed on `localhost:5433`.

```bash
# Run directly (stdio; normally launched by the client)
BUMA_MCP_DATABASE_URL=postgresql+psycopg://buma:buma@localhost:5433/buma uv run python -m buma.mcp_server

# Register with Claude Code
claude mcp add buma \
  -e BUMA_MCP_DATABASE_URL=postgresql+psycopg://buma:buma@localhost:5433/buma \
  -- uv run --directory /path/to/buma python -m buma.mcp_server
```

Any MCP client that supports stdio servers can launch the same command. Read `buma://repos` to find `repo_id` values, then call `get_triage_history` or `get_workload`. On Windows the server uses a selector event loop automatically, as the async PostgreSQL driver requires. Logs go to stderr.

The server has no authentication layer of its own. It runs as the local user, with the database credentials that user supplies. Use a database role limited to what the user is allowed to read.

---

## Testing

```bash
./scripts/test.sh                 # pytest with coverage; fails under 80%
./scripts/lint.sh                 # ruff check + black --check
uv run pytest tests/worker        # a subset
```

The default run (`addopts = -m 'not live'`) needs no external services. Redis, HTTP (`respx`), the Anthropic SDK, and the database are mocked or replaced with in-process doubles.

| Suite | Location | Covers |
|---|---|---|
| Core | `tests/core/` | Settings, HMAC signature verification |
| Gateway | `tests/gateway/` | Webhook route and ingest service, OAuth/auth routes, config routes and services, repositories, queue publisher, observability queries and routes, dev routes, chat routes, agent loop (rounds, refusals, truncation, malformed input, errors), chat tools (validation, scoping, untrusted fields, keyword fallback) |
| Worker | `tests/worker/` | Consumer, event processor (including duplicate flagging and failure isolation), triage engine and fallback paths, Claude client, parse guardrail (hostile model output), LLM budget and circuit breaker, assignee selector, embedding service, duplicate detector, backfill, GitHub client |
| MCP | `tests/mcp_server/` | Tool/resource surface, read-only annotations, untrusted-text handling, error paths via the SDK's in-memory client, and a stdio subprocess test |
| Schemas | `tests/schemas/`, `tests/test_normalized_event_contract.py` | API schemas and the queue contract |
| Integration (`integration` marker) | `tests/integration/` | Real PostgreSQL + pgvector: embedding storage and search, chat retrieval SQL, and the MCP server's read-only enforcement. Each test uses a throwaway schema |
| Live eval (`live` marker) | `tests/eval/` | Prompt-injection red-team evaluation against the real Claude API |

```bash
# Integration tests (skipped unless BUMA_TEST_DATABASE_URL is set; safe against the dev database)
BUMA_TEST_DATABASE_URL=postgresql+psycopg://buma:buma@localhost:5433/buma uv run pytest -m integration

# Live prompt-injection eval (calls the Anthropic API and incurs cost; needs ANTHROPIC_API_KEY)
uv run pytest -m live -s tests/eval
```

**End-to-end smoke test.** `scripts/smoke.py` runs the real pipeline against local PostgreSQL. It seeds a repository and developers, starts the gateway, sends a signed webhook, runs the worker for one message, verifies the persisted decision, and previews the GitHub patch.

```bash
docker compose up db redis -d && uv run alembic upgrade head
uv run python scripts/smoke.py run          # all phases
uv run python scripts/smoke.py --help       # seed | gateway | webhook | worker | verify | preview | api
```

The dashboard's `web-dashboard/src/App.test.js` is the Create React App template test and is not part of CI.

---

## CI and Developer Tooling

| Tool | Configuration | Notes |
|---|---|---|
| GitHub Actions | `.github/workflows/ci.yml` | On pull requests to `main`: a `lint` job and a `test` job on Python 3.11 with `uv` |
| Test runner | `scripts/test.sh`, `[tool.pytest.ini_options]` | `pytest --cov=src --cov-fail-under=80`, `asyncio_mode = auto` |
| Lint / format | `scripts/lint.sh`, `[tool.ruff]`, `[tool.black]` | ruff (`E`, `F`, `I`, `N`, `W`, `UP`) and black, line length 120, target py311 |
| Dependencies | `pyproject.toml`, `uv.lock` | `uv sync --dev` |
| Dev container | `.devcontainer/` | VS Code container attached to the Compose services |
| Issue templates | `.github/ISSUE_TEMPLATE/` | Epic, feature, and user-story templates |

CI and local development run the same scripts.

---

## Technical Architecture and Design Decisions

Detailed records are in [`docs/worker-design.md`](docs/worker-design.md) (DD-14 to DD-27).

- **Queue between gateway and worker.** Webhook handling stays fast and independent of GitHub API latency, LLM latency, and worker restarts. A Redis list (`LPUSH`/`BRPOP`) is used for its simplicity with a single consumer group. See DD-14.
- **Rules first, LLM as a bounded fallback.** Deterministic rules decide the common case at no cost, reproducibly, and in an explainable way. Claude is consulted only when the rules report low confidence, so an LLM outage or bad answer can affect only the already-ambiguous minority of issues, and it always falls back to the rule result. See DD-23.
- **Layered LLM guardrails.** Input fencing, forced structured output with validation, a severity ceiling, and a fail-closed cost gate each limit a different failure mode. No single layer is relied on. See DD-24.
- **Local embeddings with pgvector.** Embedding runs on every opened issue, so a local CPU model keeps marginal cost and latency flat. Storing vectors in PostgreSQL keeps them transactional with the relational data and avoids a separate vector store. See DD-25.
- **Repository-scoped retrieval.** Every similarity and retrieval query is filtered by repository (and by embedding model version), so results never mix data between repositories and vectors from different models are never compared.
- **One read-only query layer.** The REST API, MCP server, and chat tools share `observability_queries.py`, so there is one implementation of each query and one place to audit for writes. See DD-26.
- **MCP for AI clients.** MCP provides a standardized interface through which AI clients can discover and use Buma capabilities and data, without custom integrations per client. stdio keeps the trust model simple: the server runs as the local user. See DD-26.
- **Read-only AI access, enforced by the database.** Tools and prompts reduce the chance of misuse. `default_transaction_read_only=on` makes PostgreSQL reject any write attempted through the sessions used by the MCP server and the assistant.
- **Agentic RAG over a fixed retrieve-then-answer step.** Questions mix unstructured retrieval with structured facts (workload, productivity, decisions). Letting the model choose tools covers both in one loop. See DD-27.
- **Bounded agent loop.** Round limits, token caps, timeouts, and a single malformed-input retry give each question a predictable worst-case cost and latency. The loop always ends with a `done` event.
- **SSE streaming.** Answers can take several tool rounds. Streaming text and tool-progress events over a single HTTP response gives immediate feedback without WebSocket infrastructure. Validation and budget failures are returned as ordinary HTTP errors before the stream opens.
- **Separate chat budget.** Interactive usage is less predictable than triage. Independent budgets and breakers ensure that assistant traffic cannot degrade automated triage.

---

## Repository Layout

```
src/buma/
├── core/           settings, HMAC verification
├── db/             SQLAlchemy models and declarative base
├── schemas/        NormalizedEvent queue contract + API schemas
├── gateway/        FastAPI app: routes/, services/, repositories/, publishers/, chat/
├── worker/         runner, queue consumer, services/ (triage, Claude, embeddings, GitHub), backfill
└── mcp_server/     read-only stdio MCP server
web-dashboard/      React dashboard
migrations/         Alembic environment and versions
tests/              unit suites mirroring src/, integration/, eval/, fixtures/
scripts/            lint.sh, test.sh, smoke.py (+ smoke/), codegen.sh, sample webhook payload
docs/               user guide, UAT script, design decisions, contributor guidance
api/index.py        Vercel entry point for the gateway
.devcontainer/      VS Code dev container
.github/            CI workflow and issue templates
```

---

## Further Documentation

| Document | Contents |
|---|---|
| [User Guide](docs/user-guide.md) | GitHub App and OAuth App setup, environment, running the stack, ngrok, enrolling a repository |
| [Design Decisions](docs/worker-design.md) | Numbered decision records DD-14 to DD-27, with alternatives and trade-offs |
| [UAT Script](docs/uat.md) | Manual acceptance scenarios with a sign-off sheet |
| [Engineering Guidance](docs/claude.md) | Conventions and change rules for contributors and coding agents |

Contributors are listed in [`contributors/`](contributors/). No license file is included in this repository.
