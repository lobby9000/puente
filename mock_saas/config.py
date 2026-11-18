"""Settings of the mock SaaS: credentials, limits and failure injection."""

from __future__ import annotations

import os
from collections.abc import Iterable

from pydantic import BaseModel, Field


class ChaosConfig(BaseModel):
    """Failure injection. Each rate is a probability applied to every request."""

    # 500, 502 or 503 before anything is written.
    error_rate: float = Field(0.0, ge=0, le=1)
    # The batch is written, then the answer is a 504: the response is lost, the write is not.
    ghost_write_rate: float = Field(0.0, ge=0, le=1)
    # The answer is held for slow_seconds, before or after the write (half and half).
    slow_rate: float = Field(0.0, ge=0, le=1)
    slow_seconds: float = Field(3.0, ge=0, le=60)
    # A valid token is revoked on the spot, as in a server-side key rotation: the call gets a 401.
    auth_error_rate: float = Field(0.0, ge=0, le=1)
    # The Nth batch POST starts an outage: every call gets a 503 for outage_seconds.
    outage_at_request: int | None = Field(None, ge=1)
    outage_seconds: float = Field(0.0, ge=0, le=600)
    seed: int | None = None


class ServerConfig(BaseModel):
    client_id: str = "puente-dev"
    client_secret: str = "dev-secret"
    token_ttl_seconds: int = Field(300, ge=1)
    rate_limit_enabled: bool = True
    rate_limit_capacity: int = Field(20, ge=1)
    rate_limit_refill_per_s: float = Field(10.0, gt=0)
    max_batch_lines: int = Field(200, ge=1)
    payee_count: int = Field(850, ge=1)
    admin_enabled: bool = True
    chaos: ChaosConfig = Field(default_factory=ChaosConfig)

    @classmethod
    def from_env(cls, prefix: str = "MOCK_") -> ServerConfig:
        """Read MOCK_<FIELD> variables, for example MOCK_TOKEN_TTL_SECONDS=60 or MOCK_ERROR_RATE=0.1."""

        def pick(names: Iterable[str]) -> dict[str, str]:
            return {name: os.environ[prefix + name.upper()] for name in names if prefix + name.upper() in os.environ}

        top = pick(name for name in cls.model_fields if name != "chaos")
        return cls(**top, chaos=ChaosConfig(**pick(ChaosConfig.model_fields)))
