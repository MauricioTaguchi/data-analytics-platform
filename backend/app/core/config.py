from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEVELOPMENT_SECRET = "development-only-change-me"
INSECURE_SECRET_PLACEHOLDERS = {
    DEVELOPMENT_SECRET,
    "change-me",
    "changeme",
    "replace-me",
    "replace-with-a-long-random-secret",
    "secret",
    "your-secret-key",
}


class Settings(BaseSettings):
    APP_NAME: str = "Data Analytics Platform"
    ENVIRONMENT: str = "development"
    SECRET_KEY: str = DEVELOPMENT_SECRET
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    REFRESH_TOKEN_EXPIRE_DAYS: int = 14
    DATABASE_URL: str = "postgresql+psycopg2://analytics:analytics@db:5432/analytics"
    DATABASE_POOL_SIZE: int = Field(default=10, ge=1)
    DATABASE_MAX_OVERFLOW: int = Field(default=10, ge=0)
    DATABASE_POOL_TIMEOUT_SECONDS: int = Field(default=30, ge=1)
    DATABASE_POOL_RECYCLE_SECONDS: int = Field(default=1_800, ge=1)
    DATABASE_CONNECT_TIMEOUT_SECONDS: int = Field(default=10, ge=1)
    DATABASE_STATEMENT_TIMEOUT_MS: int = Field(default=30_000, ge=1)
    DATABASE_IDLE_TRANSACTION_TIMEOUT_MS: int = Field(default=60_000, ge=1)
    REDIS_URL: str = "redis://redis:6379/0"
    CELERY_EAGER: bool = False
    UPLOAD_DIR: str = "data/uploads"
    REPORT_DIR: str = "data/reports"
    MAX_FILE_SIZE_MB: int = Field(default=50, ge=1, le=10_240)
    UPLOAD_MULTIPART_OVERHEAD_MB: int = Field(default=2, ge=1, le=64)
    MAX_CONCURRENT_UPLOAD_REQUESTS: int = Field(default=2, ge=1, le=64)
    MAX_REQUEST_BODY_SIZE_MB: int = Field(default=1, ge=1, le=64)
    MAX_DATASET_ROWS: int = 1_000_000
    MAX_DATASET_COLUMNS: int = Field(default=1_000, ge=1, le=10_000)
    MAX_DATASET_COLUMN_NAME_CHARS: int = Field(default=256, ge=16, le=4_096)
    UPLOAD_CHUNK_SIZE_MB: int = 1
    USER_STORAGE_QUOTA_MB: int = Field(default=500, ge=1)
    MAX_ACTIVE_JOBS_PER_USER: int = Field(default=5, ge=1, le=100)
    UPLOAD_RATE_LIMIT_MAX_ATTEMPTS: int = Field(default=10, ge=1)
    UPLOAD_RATE_LIMIT_WINDOW_SECONDS: int = Field(default=60, ge=1)
    MIN_FREE_DISK_SPACE_MB: int = Field(default=512, ge=1)
    MAX_DATASET_EXPANDED_SIZE_MB: int = Field(default=512, ge=1)
    MAX_DATASET_EXPANSION_RATIO: int = Field(default=100, ge=1)
    MAX_XLSX_ARCHIVE_ENTRIES: int = Field(default=10_000, ge=1)
    MAX_PROFILE_CORRELATION_COLUMNS: int = Field(default=50, ge=2, le=250)
    TRANSFORMATION_PREVIEW_MAX_COLUMNS: int = Field(default=100, ge=1, le=1_000)
    TRANSFORMATION_PREVIEW_MAX_CELL_CHARS: int = Field(default=512, ge=32, le=10_000)
    MAX_JOB_RESULT_SIZE_MB: int = Field(default=2, ge=1, le=64)
    MAX_REPORT_SIZE_MB: int = Field(default=10, ge=1, le=100)
    STORAGE_BACKEND: str = "local"
    CORS_ORIGINS: list[str] = ["http://localhost:5173"]
    TRUST_PROXY_HEADERS: bool = False
    AUTH_RATE_LIMIT_MAX_ATTEMPTS: int = 10
    AUTH_RATE_LIMIT_WINDOW_SECONDS: int = 60
    CACHE_FALLBACK_MAX_ITEMS: int = 1_000
    CACHE_FALLBACK_TTL_SECONDS: int = 60
    CELERY_WORKER_MAX_MEMORY_KB: int = 524_288
    CELERY_WORKER_MAX_TASKS: int = 50
    CELERY_WORKER_CONCURRENCY: int = Field(default=2, ge=1, le=16)
    REFRESH_SESSION_CLEANUP_BATCH_SIZE: int = Field(default=1_000, ge=1, le=100_000)
    OUTBOX_DISPATCH_BATCH_SIZE: int = Field(default=100, ge=1, le=1_000)
    OUTBOX_CLAIM_TTL_SECONDS: int = Field(default=120, ge=30, le=3_600)
    OUTBOX_MAX_RETRY_SECONDS: int = Field(default=300, ge=10, le=3_600)
    JOB_RETENTION_DAYS: int = Field(default=30, ge=1, le=3_650)
    JOB_RETENTION_BATCH_SIZE: int = Field(default=1_000, ge=1, le=10_000)

    @model_validator(mode="after")
    def validate_production_secret(self) -> "Settings":
        if self.ENVIRONMENT.lower() not in {"development", "test"}:
            secret = self.SECRET_KEY.strip()
            normalized = secret.casefold()
            looks_like_placeholder = (
                normalized in INSECURE_SECRET_PLACEHOLDERS
                or normalized.startswith(("change-", "replace-", "your-secret"))
                or "placeholder" in normalized
            )
            if len(secret.encode("utf-8")) < 32 or looks_like_placeholder:
                raise ValueError(
                    "SECRET_KEY must contain at least 32 non-placeholder UTF-8 bytes "
                    "outside development and test environments."
                )
        return self

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
