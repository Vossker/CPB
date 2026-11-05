from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CPB_", env_file=".env", extra="ignore")

    environment: str = "development"
    database_url: str = "postgresql+psycopg://cpb:cpb-dev-only@localhost:5432/cpb"
    api_secret: str = "development-only-change-me"

    storage_endpoint: str = "http://localhost:9000"
    storage_public_endpoint: str = "http://localhost:9000"
    storage_access_key: str = "cpb-minio"
    storage_secret_key: str = "development-only-change-me"
    storage_bucket: str = "cpb"
    storage_region: str = "us-east-1"

    signup_bonus: int = 100
    platform_username: str = "__platform__"
    max_cpu: int = 4
    max_memory_mb: int = 4096
    max_timeout_seconds: int = 900
    max_bundle_bytes: int = 50 * 1024 * 1024
    max_artifact_bytes: int = 100 * 1024 * 1024
    max_retries: int = 2
    lease_seconds: int = 120
    node_offline_seconds: int = 90
    worker_poll_seconds: int = 5
    worker_heartbeat_seconds: int = 20
    worker_log_upload_seconds: int = 30
    runtime_images: tuple[str, ...] = (
        "python:3.11-slim",
        "python:3.12-slim",
        "node:20-slim",
        "ubuntu:24.04",
        "gcc:14",
    )

    @field_validator("api_secret", "storage_secret_key")
    @classmethod
    def reject_empty_secret(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("secret must not be empty")
        return value

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
