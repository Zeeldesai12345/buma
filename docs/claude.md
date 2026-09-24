# CLAUDE.md

# Buma — Intelligent Bug Triaging & Assignment System

## 1. Project Overview

Buma is an intelligent GitHub bug triaging and assignment system.

The system receives GitHub issue events through webhooks, validates and
normalizes the events, queues them through Redis, processes them through
the triage worker, selects an appropriate developer, persists the decision
in PostgreSQL, and updates the GitHub issue.

The current MVP primarily uses rule-based triage. An optional hybrid mode
can consult the Claude API as a fallback when rule-based confidence is low
(see Section 4 — Hybrid Claude Fallback). It is disabled unless
`ANTHROPIC_API_KEY` is configured.

The main goals of Buma are to:

- Automatically process GitHub issues.
- Determine whether an issue is eligible for triage.
- Categorize issues.
- Determine issue priority.
- Select an appropriate developer.
- Persist triage decisions.
- Update GitHub issues with labels, assignments, and explanations.
- Provide dashboard visibility into issues and developer workload.
- Keep triage decisions explainable and traceable.

Do not replace the existing rule-based triage with AI/LLM-based triage
unless explicitly requested. The Claude API fallback described above was
explicitly requested and is bounded (low-confidence issues only, with
automatic fallback to rules on any failure) — it supplements rule-based
triage, it does not replace it. Do not broaden Claude/LLM usage beyond this
bounded fallback unless explicitly requested again.

---

## 2. Repository Structure

The current repository structure is:

```text
buma/
├── .devcontainer/       # Development container configuration
├── .git/                # Git repository data
├── .github/             # GitHub Actions and automation
├── contributors/        # Contributor information
├── docs/                # Project documentation
├── migrations/          # Database migrations
├── scripts/             # Development and testing scripts
├── src/                 # Python backend source code
├── tests/               # Backend tests
├── web-dashboard/       # React frontend
├── .dockerignore
├── .env                 # Local environment variables; never commit secrets
├── .env.example         # Example environment configuration
├── alembic.ini          # Alembic configuration
├── docker-compose.yml   # Local container configuration
├── Dockerfile           # Container image configuration
├── pyproject.toml       # Python project and dependency configuration
├── README.md            # Main project documentation
└── uv.lock              # Locked Python dependencies

## Backend

The backend is located under:

```text
src/
└── buma/
    ├── gateway/
    │   └── services/
    └── worker/
        └── services/
```

## Tests

Tests are organized as:

```text
tests/
├── core/
├── gateway/
├── schemas/
└── worker/
```

## Frontend

The dashboard is located under:

```text
web-dashboard/
└── src/
    └── services/
```

## Scripts

Project scripts are located under:

```text
scripts/
└── smoke/
```

Use the existing repository structure and inspect the actual files before assuming that a component or file exists.

---

## 3. Technology Stack

**Backend**

- Python
- FastAPI
- SQLAlchemy
- Alembic
- uv

**Frontend**

- React
- Material UI
- Axios
- React Router
- Recharts

**Database**

- PostgreSQL

**Queue**

- Redis

**Infrastructure**

- Docker
- Docker Compose
- Devcontainer
- GitHub Actions
- Kubernetes for staging

**GitHub Integration**

- GitHub Webhooks
- GitHub API
- GitHub App
- GitHub OAuth 2.0

**AI / LLM (optional)**

- Anthropic Claude API (`anthropic` Python SDK) — hybrid triage fallback
  only, consulted below a confidence threshold. See Section 4.

**Testing**

- Unit tests
- Integration tests
- Smoke tests

Use the existing technology stack and project conventions. Do not add a new framework, library, or infrastructure component unless it is required for the requested task.

---

## 4. System Architecture

The primary Buma processing flow is:

```text
GitHub
   |
   | Webhook
   v
Webhook Gateway
   |
   | Validation
   | Idempotency
   | Normalization
   v
Redis Queue
   |
   | Consume
   v
Triage Worker
   |
   | Rule-Based Triage (+ optional Claude fallback, low confidence only)
   v
Assignee Selection
   |
   v
PostgreSQL
   |
   v
GitHub Update
```

### Gateway

The Gateway is responsible for:

- Receiving HTTP requests.
- Receiving GitHub webhooks.
- Validating webhook signatures.
- Validating requests.
- Handling authentication and authorization.
- Handling idempotency.
- Publishing events to Redis.
- Providing API endpoints used by the dashboard.

### Redis

Redis is used to decouple webhook ingestion from asynchronous Worker processing.

The current MVP queue is:

```text
buma:triage:queue
```

The existing Redis queue implementation should be preserved unless the task explicitly requires a change.

### Worker

The Worker consumes queued events and performs asynchronous processing.

The intended workflow is:

```text
Load repository configuration
        ↓
Check issue eligibility
        ↓
Determine category  ─┐
        ↓             │  low confidence + ANTHROPIC_API_KEY set
Determine priority   │  → optional Claude fallback (see below)
        ↓            ┘
Select assignee
        ↓
Persist triage decision
        ↓
Update GitHub
```

### Hybrid Claude Fallback (Optional)

`TriageEngine.classify()` (rule-based, deterministic) always runs first and
is unchanged by this feature. If its `confidence` is below
`CLAUDE_CONFIDENCE_THRESHOLD` (default `0.5`) **and** `ANTHROPIC_API_KEY` is
set, `TriageEngine.classify_with_fallback()` additionally consults
`ClaudeClassifier` (`src/buma/worker/services/claude_client.py`) for a
second opinion, using a forced tool call constrained to the same
category/priority values the rule engine uses.

Any Claude failure (timeout, connection error, non-2xx response, or an
invalid/out-of-range category, priority, or confidence value) is caught and
logged, and the original rule result is used instead — this path never
raises and never blocks the pipeline. `TriageDecision.explanation` and the
`TriageResult.engine_version` field always record which path actually
answered: `rules-v1`, `claude-hybrid-v1`, or `rules-v1-fallback`. See
[DD-23 in Worker Design](worker-design.md#dd-23--hybrid-claude-api-fallback-for-low-confidence-classifications)
for the full rationale.

If `ANTHROPIC_API_KEY` is not set, this path is skipped entirely and triage
behaves exactly as it did before this feature existed.

### Gateway and Worker Separation

Keep Gateway and Worker responsibilities separate.

The queue consumer should focus on consuming and deserializing queue messages.

`EventProcessorService` should handle event-processing logic.

Do not unnecessarily move business logic into the queue consumer.

### Database

PostgreSQL stores persistent application and triage information.

The dashboard should communicate with the backend through the Gateway rather than directly accessing the database.

---

## 5. Development and Testing

Python dependencies are managed using:

- `pyproject.toml`
- `uv.lock`

Use uv and the existing project configuration for Python development.

The project also uses Docker, Docker Compose, and Devcontainer.

Prefer existing project scripts and commands instead of creating duplicate workflows.

### Before Making Changes

1. Inspect the relevant source files.
2. Understand the existing implementation.
3. Review related tests.
4. Check project documentation when necessary.
5. Identify the smallest reasonable change.

Do not guess the implementation when the repository can be inspected.

### Testing

Tests are located under:

```text
tests/
├── core/
├── gateway/
├── schemas/
└── worker/
```

When behavior changes:

1. Identify affected tests.
2. Add or update tests when appropriate.
3. Run the relevant tests.
4. Run broader tests when the change affects shared functionality.

Smoke-test functionality is located under:

```text
scripts/smoke/
```

Never claim that a test passed unless it was actually executed.

If tests cannot be run, clearly state why.

### Development Workflow

Use the following general workflow:

```text
Understand
    ↓
Plan
    ↓
Implement
    ↓
Test
    ↓
Review
    ↓
Report
```

For larger changes, identify affected files, components, tests, and possible side effects before making broad changes.

---

## 6. Security

Security-sensitive functionality must not be bypassed.

Never commit or expose:

- API keys.
- Anthropic/Claude API keys.
- GitHub tokens.
- GitHub App private keys.
- Webhook secrets.
- Database passwords.
- Authentication credentials.
- Private keys.
- Other sensitive information.

Never expose the contents of:

```text
.env
```

Use:

```text
.env.example
```

for documenting required environment variables without real credentials.

### Webhook Security

GitHub webhook signatures must be validated.

Do not bypass webhook signature validation merely to make local testing easier.

### Idempotency

Webhook delivery IDs should be used to prevent duplicate processing.

Do not remove or weaken existing idempotency behavior.

Duplicate webhook deliveries should not create duplicate triage decisions or duplicate side effects.

### Authentication and Authorization

Do not disable existing authentication or authorization controls.

### Logging

Do not log:

- Passwords.
- Tokens.
- API keys.
- Webhook secrets.
- Private keys.
- Other sensitive credentials.

---

## 7. Code Change Guidelines

Follow the existing architecture and coding conventions.

### General Rules

- Inspect existing code before making assumptions.
- Read relevant tests before changing behavior.
- Prefer existing patterns and abstractions.
- Keep changes focused.
- Avoid unrelated refactoring.
- Avoid unnecessary dependencies.
- Avoid duplicate implementations.
- Preserve existing APIs unless a change is required.
- Preserve existing error handling.
- Preserve Gateway/Worker separation.
- Preserve asynchronous processing.
- Keep triage decisions explainable.

### API Changes

Before changing an existing API:

1. Search for existing callers.
2. Review related tests.
3. Check frontend usage.
4. Consider compatibility.
5. Update affected code and tests.

Do not casually change API contracts.

### Database Changes

Before changing database models:

1. Inspect the existing model.
2. Search for usages.
3. Check related schemas and APIs.
4. Check Worker usage.
5. Review relevant tests.
6. Create or update the appropriate migration.

Use Alembic for database schema changes.

Do not manually change the database schema without considering the corresponding migration.

### Queue Changes

Before changing queue messages:

1. Inspect the producer.
2. Inspect the consumer.
3. Inspect serialization/deserialization.
4. Review related schemas.
5. Update relevant tests.

Do not make incompatible queue-message changes without updating both sides.

### Error Handling

Do not silently swallow errors.

Preserve existing behavior for:

- Webhook validation failures.
- Invalid signatures.
- Duplicate deliveries.
- Redis failures.
- GitHub failures.
- Retryable failures.
- Non-retryable failures.

Transient external-service failures should follow the project's existing retry behavior.

### Git and File Changes

Make the smallest reasonable change.

Do not modify unrelated files unless required.

Do not rewrite working code simply to use a different style.

Before finishing, review all changed files for unintended modifications.

---

## 8. Claude Code Workflow and Definition of Done

When working on any task, follow this process.

### Step 1 — Understand

Inspect the relevant repository files, source code, tests, and configuration.

Do not guess when the information can be obtained from the repository.

### Step 2 — Plan

Identify:

- The requested behavior.
- The component responsible.
- Files likely to change.
- Related tests.
- Potential side effects.

For larger tasks, create a clear implementation plan before making broad changes.

### Step 3 — Implement

Make the smallest reasonable change.

Follow existing:

- Architecture.
- Naming conventions.
- Coding patterns.
- Error handling.
- Testing patterns.

### Step 4 — Test

Run the relevant tests.

If the change affects multiple components, run broader tests when appropriate.

Never claim tests passed unless they were actually executed.

### Step 5 — Review

Before completing the task, check:

- Changed files.
- Unintended modifications.
- Error handling.
- Security.
- Webhook validation.
- Idempotency.
- API compatibility.
- Database implications.
- Queue compatibility.
- Tests.

### Step 6 — Report

The final response should briefly include:

**Changes Made**

What functionality was changed or added.

**Files Changed**

The important files modified.

**Tests**

Tests that were actually executed.

**Results**

Whether the tests passed or failed.

**Remaining Issues**

Any limitations, warnings, or tests that could not be executed.