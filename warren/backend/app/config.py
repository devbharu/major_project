"""All configuration comes from environment variables (see ../../.env.example)."""
import logging
import os
import secrets
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))
load_dotenv()


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _secret_key() -> str:
    value = os.getenv("SECRET_KEY", "")
    if len(value) >= 32 and not value.startswith(("change-me", "dev-secret")):
        return value
    if os.getenv("PUBLIC_URL") or os.getenv("WARREN_ENV") == "production":
        raise RuntimeError("SECRET_KEY must be a random value of at least 32 characters (python -c \"import secrets;print(secrets.token_urlsafe(48))\")")
    logging.getLogger("warren").warning("SECRET_KEY is not set: using a random key for this run, so sessions reset on restart.")
    return secrets.token_urlsafe(48)


@dataclass(frozen=True)
class Settings:
    env: str = os.getenv("WARREN_ENV", "development")
    secret_key: str = field(default_factory=_secret_key)
    token_encryption_key: str = os.getenv("TOKEN_ENCRYPTION_KEY", "")
    session_hours: int = int(os.getenv("SESSION_HOURS", "24"))
    trust_proxy: bool = _bool("TRUST_PROXY")
    frontend_url: str = os.getenv("FRONTEND_URL", "http://localhost:5173").rstrip("/")
    backend_url: str = os.getenv("BACKEND_URL", "http://localhost:5000").rstrip("/")
    public_url: str = os.getenv("PUBLIC_URL", "").rstrip("/")
    sqlite_path: str = os.getenv("SQLITE_PATH", ":memory:")
    chroma_path: str = os.getenv("CHROMA_PATH", ".data/chroma")
    checkpoint_path: str = os.getenv("CHECKPOINT_PATH", ".data/checkpoints.db")
    demo_seed: bool = _bool("DEMO_SEED")
    poll_interval: int = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))

    github_client_id: str = os.getenv("GITHUB_CLIENT_ID", "")
    github_client_secret: str = os.getenv("GITHUB_CLIENT_SECRET", "")
    github_webhook_secret: str = os.getenv("GITHUB_WEBHOOK_SECRET", "")
    github_bot_token: str = os.getenv("GITHUB_BOT_TOKEN", "")
    bot_mention: str = os.getenv("BOT_MENTION", "@warren")

    llm_base_url: str = os.getenv("OMNIROUTE_BASE_URL", "http://localhost:20128/v1")
    llm_api_key: str = os.getenv("OMNIROUTE_API_KEY", "sk_omniroute")
    llm_model: str = os.getenv("LLM_MODEL", "auto")
    embeddings_provider: str = os.getenv("EMBEDDINGS_PROVIDER", "default")
    embedding_model: str = os.getenv("EMBEDDING_MODEL", "")

    e2b_api_key: str = os.getenv("E2B_API_KEY", "")
    sandbox_timeout: int = int(os.getenv("SANDBOX_TIMEOUT_SECONDS", "900"))

    # Agent guardrails (CodeRabbit guide: max steps, tool budgets, strict size limits)
    max_agent_steps: int = 8
    max_diff_chars: int = 45_000
    max_review_batches: int = 4
    max_findings: int = 25
    max_inline_comments: int = 10     # precision over recall: the rest go into the summary table
    min_confidence: float = 0.6
    max_index_files: int = 60
    max_index_file_bytes: int = 60_000
    worker_threads: int = 4
    # CI Doctor guardrails
    max_ci_attempts: int = 2
    ci_log_chars: int = 7000
    protected_paths: tuple = (".github/workflows/", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Pipfile.lock",
                              ".env", "secrets", "terraform/", "Dockerfile", "docker-compose")

    @property
    def github_oauth_enabled(self) -> bool:
        return bool(self.github_client_id and self.github_client_secret)

    @property
    def sandbox_enabled(self) -> bool:
        return bool(self.e2b_api_key)


settings = Settings()
