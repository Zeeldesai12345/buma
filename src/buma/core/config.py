from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    redis_url: str = "redis://localhost:6379/0"
    github_webhook_secret: str

    # GitHub App credentials — required for Phase 6 (GitHub patch).
    # If either is absent, Phase 6 is skipped and patch_state stays DECIDED.
    github_app_id: int | None = None
    github_app_private_key: str | None = None  # PEM content, newlines as \n

    # GitHub OAuth App credentials — required for dashboard login.
    # Register a GitHub OAuth App at github.com/settings/developers.
    github_oauth_client_id: str | None = None
    github_oauth_client_secret: str | None = None

    # Session cookie signing key. Override with a strong random value in production.
    session_secret: str = "dev-secret-change-in-production!"

    # Comma-separated allowed CORS origins, e.g. "https://buma.example.com,http://localhost:3000"
    cors_origins: str = "http://localhost:3000,http://localhost:5173"

    # Enable dev-only endpoints (e.g. POST /dev/sign-webhook).
    # Never set to True in production.
    debug: bool = False

    # === Claude API (hybrid triage fallback) ===
    # Used only by the worker, and only when rule-based confidence is below
    # claude_confidence_threshold. If unset, the hybrid path is disabled and
    # triage stays 100% rule-based.
    anthropic_api_key: str | None = None
    claude_model: str = "claude-haiku-4-5-20251001"
    claude_confidence_threshold: float = 0.5
    claude_timeout_seconds: float = 8.0
    # SDK retries on connection errors, 408, 409, 429 and 5xx. Worst-case latency per event is
    # roughly (claude_max_retries + 1) * claude_timeout_seconds plus backoff.
    claude_max_retries: int = 2

    # === Claude guardrails (prompt-injection + cost limits) ===
    # Issue bodies longer than this are cut before being sent to Claude.
    claude_max_body_chars: int = 4000
    # Max Claude calls per repo per UTC day; over budget the rule result is used.
    claude_daily_call_limit_per_repo: int = 200
    # After this many consecutive Claude failures, skip Claude for claude_breaker_cooldown_seconds.
    claude_breaker_threshold: int = 5
    claude_breaker_cooldown_seconds: int = 300
    # Most severe priority a Claude-only answer may set. Rule-engine priorities are never capped.
    claude_max_priority: str = "P1"

    # === Semantic duplicate detection (T3 / DD-25) ===
    # Worker-only. Every opened issue in an enrolled repo is embedded locally (CPU, fastembed)
    # and stored in issue_embeddings. If the model fails to load, the feature is disabled and
    # triage continues unchanged.
    embedding_enabled: bool = True
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    # Issue body is cut to this many characters before embedding (the model truncates at 512 tokens anyway).
    embedding_max_chars: int = 2000
    # Where fastembed caches model files. The Docker image bakes the model into /opt/fastembed_cache.
    embedding_cache_dir: str | None = None
    duplicate_top_k: int = 5
    # PROVISIONAL — not yet chosen from a labelled eval. Only used when duplicate_comment_enabled is true.
    duplicate_similarity_threshold: float = 0.9
    # Keep false until the threshold has been chosen from measured precision/recall.
    duplicate_comment_enabled: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
