"""Settings, read from environment variables or a local .env file."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: str | Path = ".env") -> None:
    """Minimal .env support: KEY=value lines and # comments. The real environment wins."""
    file = Path(path)
    if not file.is_file():
        return
    for raw in file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


@dataclass(frozen=True)
class Settings:
    saas_url: str = "http://localhost:8000"
    client_id: str = "puente-dev"
    client_secret: str = "dev-secret"
    state_db: str = "state/puente.db"
    log_file: str = "logs/puente.jsonl"
    batch_size: int = 100
    timeout_s: float = 2.0
    max_attempts: int = 6
    max_elapsed_s: float = 30.0

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv()

        def get(name: str, default, cast: Callable = str):
            value = os.environ.get(f"PUENTE_{name.upper()}")
            return default if value in (None, "") else cast(value)

        return cls(
            saas_url=get("saas_url", cls.saas_url),
            client_id=get("client_id", cls.client_id),
            client_secret=get("client_secret", cls.client_secret),
            state_db=get("state_db", cls.state_db),
            log_file=get("log_file", cls.log_file),
            batch_size=get("batch_size", cls.batch_size, int),
            timeout_s=get("timeout_s", cls.timeout_s, float),
            max_attempts=get("max_attempts", cls.max_attempts, int),
            max_elapsed_s=get("max_elapsed_s", cls.max_elapsed_s, float),
        )
