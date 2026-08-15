from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEVELOPMENT_SECRET = "development-only-change-me"

class Settings(BaseSettings):
    APP_NAME: str = "Data Analytics Platform"
    ENVIRONMENT: str = "development"
    SECRET_KEY: str = DEVELOPMENT_SECRET
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    REFRESH_TOKEN_EXPIRE_DAYS: int = 14
    DATABASE_URL: str = "postgresql+psycopg2://analytics:analytics@db:5432/analytics"
    REDIS_URL: str = "redis://redis:6379/0"
    CELERY_EAGER: bool = False
    UPLOAD_DIR: str = "data/uploads"
    REPORT_DIR: str = "data/reports"
    MAX_FILE_SIZE_MB: int = 50
    MAX_DATASET_ROWS: int = 1_000_000
    UPLOAD_CHUNK_SIZE_MB: int = 1
    STORAGE_BACKEND: str = "local"
    CORS_ORIGINS: list[str] = ["http://localhost:5173"]
    TRUST_PROXY_HEADERS: bool = False
    AUTH_RATE_LIMIT_MAX_ATTEMPTS: int = 10
    AUTH_RATE_LIMIT_WINDOW_SECONDS: int = 60
    CACHE_FALLBACK_MAX_ITEMS: int = 1_000
    CACHE_FALLBACK_TTL_SECONDS: int = 60
    CELERY_WORKER_MAX_MEMORY_KB: int = 524_288
    CELERY_WORKER_MAX_TASKS: int = 50

    @model_validator(mode="after")
    def validate_production_secret(self) -> "Settings":
        if self.ENVIRONMENT.lower() not in {"development", "test"} and self.SECRET_KEY == DEVELOPMENT_SECRET:
            raise ValueError("SECRET_KEY must be configured outside development and test environments.")
        return self

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
