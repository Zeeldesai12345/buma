# buma

**Automated bug triage and assignment for GitHub, built as a production-shaped async pipeline.**

[![CI](https://github.com/Zeeldesai12345/buma/actions/workflows/ci.yml/badge.svg)](https://github.com/Zeeldesai12345/buma/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-async%20gateway-009688)
![React](https://img.shields.io/badge/React-19-61DAFB)
![Docker](https://img.shields.io/badge/Docker-Compose-2496ED)
![Tests](https://img.shields.io/badge/tests-314%20passing-brightgreen)
![Coverage](https://img.shields.io/badge/coverage-≥80%25%20gated-brightgreen)

Every new GitHub issue is a small decision problem: what kind of bug is this, how urgent is it, and who on the team should own it? buma answers that automatically — the moment an issue is opened, it classifies it, scores its priority, picks the best-fit developer by skills and current workload, and writes an auditable explanation straight into the GitHub issue thread. No human touches the triage backlog unless the rules say they should.

> **Demo video:** [Watch buma triage a live issue end-to-end](https://stevens.zoom.us/rec/share/DpeGj3KoGgIkXlwmR3SSpuH__39eKzjHtn9kkbzFWN42T6k-wy4XPu1xdiyueV37.emvdavdLR5sSbDlY?startTime=1776824419000)

---

## Why it's interesting

This isn't a script that calls the GitHub API — it's a small distributed system, built the way you'd defend it in a design review:

- **Decoupled by a queue, not a function call.** The webhook gateway never blocks on triage logic; it validates and hands off to Redis. A separate async worker consumes, processes, and retries — so a slow GitHub API call or a bad payload can't take down ingestion.
- **Every automated decision is explainable and reversible.** Each triage run writes a decision log row *and* posts a human-readable comment on the issue — category, priority, and why a specific developer was chosen. Nothing is a black box.
- **Security is not an afterthought.** Webhook deliveries are HMAC-signature verified before anything touches the queue; the dashboard uses real GitHub OAuth 2.0 session auth, not a shared password.
- **Idempotent by design.** Webhook deliveries are deduplicated and retried safely — reprocessing the same event twice never double-assigns or double-comments.
- **Rule-based first, LLM as a bounded safety net.** Classification runs through deterministic rules by default; only when confidence is low does it optionally consult the Claude API for a second opinion, and any timeout, invalid response, or outage falls back to the rule result automatically — no single point of failure, no unbounded LLM dependency. Issue text is treated as untrusted input: it is truncated and fenced off from instructions, Claude's output is schema-checked, a Claude-only answer can never raise a P0, and a per-repo daily budget plus a circuit breaker cap API spend.
- **Tested like it matters.** 377 tests across gateway, worker, and schemas, with an 80% coverage gate enforced in CI on every PR — plus a deterministic red-team suite for hostile model output and a separate live prompt-injection eval.
- **Documented for a stranger to pick up.** Numbered design decisions (`docs/worker-design.md`), a full setup walkthrough (`docs/user-guide.md`), and a 10-scenario UAT script with sign-off tables (`docs/uat.md`) — the kind of documentation a real engineering org expects before something ships.

---

## Architecture

```
                                   ┌─────────────────┐
   GitHub issue opened  ──────────▶  Webhook Gateway  │  FastAPI · HMAC-verified
                                   │   (async, thin)   │  Enqueues, never blocks
                                   └────────┬─────────┘
                                            │  Redis (BRPOP list queue)
                                            ▼
                                   ┌──────────────────┐
                                   │   Triage Worker   │  async, graceful shutdown
                                   │  ───────────────  │
                                   │  1. Classify      │  rules first, optional Claude fallback
                                   │  2. Assign        │  skills + capacity + tie-break
                                   │  3. Persist        │  decision log (Postgres)
                                   │  4. Patch GitHub  │  labels, assignee, comment
                                   └────────┬─────────┘
                                            │
                       ┌────────────────────┼───────────────────┐
                       ▼                                        ▼
              PostgreSQL (audit trail)                React Dashboard
                                                   config · history · workload
```

The queue absorbs bursts and enables retries without dropping events; the decision log makes every automated action traceable after the fact.

---

## Tech stack

| Layer | Technology |
|---|---|
| Webhook gateway | FastAPI, async, HMAC-signed webhook validation, GitHub OAuth 2.0 |
| Triage worker | Python asyncio, Redis-backed queue consumer, graceful signal-based shutdown |
| Data layer | PostgreSQL, SQLAlchemy 2.0 ORM, Alembic migrations |
| Hybrid classification (optional) | Anthropic Claude API via the official `anthropic` SDK — consulted only when rule-based confidence is below a threshold |
| Dashboard | React 19, MUI, Recharts, React Router, Axios |
| Testing | pytest, pytest-asyncio, pytest-cov (80% gate), respx (HTTP mocking) |
| Tooling | uv, ruff, black, Docker Compose, VS Code Dev Containers |
| CI/CD | GitHub Actions — lint + test on every PR |

---

## Repository status

The full backend pipeline is implemented and end-to-end smoke-tested:

- **Gateway** (`src/buma/gateway/`) — webhook ingest, HMAC validation, Redis publish; dashboard config + observability API; GitHub OAuth 2.0 login + session auth
- **Worker** (`src/buma/worker/`) — queue consumer, triage engine (rule-based, with an optional low-confidence Claude API fallback), assignee selector, DB persistence, GitHub patch (labels + assignee + comment)
- **Database** (`src/buma/db/`) — 6 ORM models, Alembic migrations applied
- **API schemas** (`src/buma/schemas/api/`) — typed request/response contracts for all `/api/*` routes
- **Dashboard** (`web-dashboard/`) — React app for configuration, triage history, and workload visibility
- **Tests** — unit coverage across gateway/worker/schemas + an end-to-end smoke script (`scripts/smoke.py`)

---

## Quick start

**Prerequisites:** Docker + Docker Compose (Python 3.11 and `uv` only needed for host-mode dev).

```bash
git clone https://github.com/Zeeldesai12345/buma.git
cd buma
cp .env.example .env        # fill in GitHub App / OAuth credentials
docker compose build
docker compose up
```

Docker Compose brings up Postgres and Redis, runs Alembic migrations, then starts the gateway and worker in the right order. See the [User Guide](docs/user-guide.md) for the full walkthrough — GitHub App + OAuth App setup, `ngrok` tunneling, and enrolling your first repo.

**Optional — hybrid Claude fallback:** set `ANTHROPIC_API_KEY` in `.env` to let the worker consult Claude for issues the rule engine classifies with low confidence (see [DD-23](docs/worker-design.md#dd-23--hybrid-claude-api-fallback-for-low-confidence-classifications)). Leave it unset and triage stays 100% rule-based, exactly as before. The Claude path is guarded against prompt injection and runaway cost (see [DD-24](docs/worker-design.md#dd-24--prompt-injection-guardrail-and-cost-limits-on-the-claude-path)); the limits — `CLAUDE_MAX_BODY_CHARS`, `CLAUDE_DAILY_CALL_LIMIT_PER_REPO`, `CLAUDE_BREAKER_THRESHOLD`, `CLAUDE_BREAKER_COOLDOWN_SECONDS`, `CLAUDE_MAX_PRIORITY` — are documented in `.env.example`.

<details>
<summary><strong>Host-mode dev (infra in Docker, services on your machine)</strong></summary>

```bash
docker compose up db redis -d

export DATABASE_URL=postgresql+psycopg://buma:buma@localhost:5432/buma
export REDIS_URL=redis://localhost:6379/0

uv sync --dev
uv run alembic upgrade head

uv run uvicorn buma.gateway.app:app --reload --port 8000   # terminal 1
uv run python -m buma.worker.runner                        # terminal 2
```
</details>

---

## API surface

| Method | Route | Description |
|---|---|---|
| `GET` | `/health` | Liveness check |
| `POST` | `/webhook/github` | GitHub webhook receiver (HMAC-verified) |
| `GET` | `/auth/github` → `/auth/callback` | OAuth login flow |
| `POST` / `GET` / `PATCH` | `/api/config/repos/...` | Repo + developer profile configuration |
| `GET` | `/api/triage/{repo_id}` | Paginated triage decision history |
| `GET` | `/api/workload/{repo_id}` | Developer workload view |

All `/api/*` routes require an authenticated session (GitHub OAuth).

---

## Testing & quality gates

```bash
./scripts/lint.sh    # ruff + black --check
./scripts/test.sh     # pytest + 80% coverage gate
```

Both run in CI on every pull request. Local dev and CI intentionally share the same scripts — nothing is duplicated into workflow YAML.

```bash
uv run python scripts/smoke.py run     # full end-to-end smoke test
```

The smoke script exercises the real pipeline: seeds data, fires a signed webhook, drains the worker, and verifies the resulting triage decision, labels, assignee, and comment.

---

## Repository layout

```
src/buma/
├── core/           settings, HMAC security
├── db/             ORM models, SQLAlchemy base
├── schemas/        gateway↔worker contract + API schemas
├── gateway/        FastAPI ingest service + dashboard API
└── worker/         async queue consumer + triage pipeline

web-dashboard/       React 19 + MUI + Recharts operator dashboard
tests/                mirrors src/buma/, 377 tests (+ live evals under tests/eval/)
migrations/           Alembic migrations
scripts/               lint, test, codegen, smoke test
docs/
├── user-guide.md     install + configure + run, end to end
├── uat.md              10-scenario UAT script with sign-off
└── worker-design.md   numbered design decisions (DD-14…DD-24)
.github/workflows/    CI: lint + test on every PR
.devcontainer/         reproducible VS Code dev environment
```

---

## Documentation

| Document | Audience | What it covers |
|---|---|---|
| [User Guide](docs/user-guide.md) | New users, evaluators | Clone, configure GitHub App + OAuth App, run with Docker, ngrok, enroll a repo |
| [UAT Script](docs/uat.md) | Reviewers | 10 acceptance test scenarios, defect log, sign-off sheet |
| [Worker Design](docs/worker-design.md) | Contributors | Design rationale for the queue consumer and async pipeline |

---

## Team

Built as a team capstone project. See [`contributors/`](contributors/) for individual contributors.

## License

A project license has not been added yet — do not assume reuse permissions until a `LICENSE` file is present.
