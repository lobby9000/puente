"""Settings, read from environment variables or a local .env file."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path


def set_env_values(path: str | Path, values: dict[str, str]) -> None:
    """Write KEY=value lines into a .env file, replacing those keys and keeping every other line.

    The file is rewritten in one step and left readable by its owner only.
    """
    file = Path(path)
    lines = file.read_text(encoding="utf-8").splitlines() if file.is_file() else []
    pending = dict(values)
    for index, raw in enumerate(lines):
        key = raw.split("=", 1)[0].strip() if "=" in raw and not raw.lstrip().startswith("#") else None
        if key in pending:
            lines[index] = f"{key}={pending.pop(key)}"
    lines.extend(f"{key}={value}" for key, value in pending.items())
    temporary = file.with_name(file.name + ".tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, file)


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
    # Databricks, read under the names Databricks tools use. The token never shows in a repr.
    databricks_host: str = ""
    databricks_http_path: str = ""
    databricks_token: str = field(default="", repr=False)
    databricks_table: str = "workspace.puente.gold_vendor_commissions"
    # Zoho CRM (stage 3). `python -m puente zoho auth` writes the refresh token and API domain.
    zoho_client_id: str = ""
    zoho_client_secret: str = field(default="", repr=False)
    zoho_refresh_token: str = field(default="", repr=False)
    zoho_accounts_url: str = "https://accounts.zoho.com"
    zoho_api_domain: str = ""
    zoho_timeout_s: float = 15.0
    zoho_credit_budget: int = 500
    zoho_credit_reserve: int = 1000
    payees: str = "destination"  # where rep-to-payee mappings come from: destination or zoho

    def missing_zoho_settings(self, need_refresh_token: bool = True) -> list[str]:
        values = [("ZOHO_CLIENT_ID", self.zoho_client_id), ("ZOHO_CLIENT_SECRET", self.zoho_client_secret)]
        if need_refresh_token:
            values.append(("ZOHO_REFRESH_TOKEN", self.zoho_refresh_token))
        return [name for name, value in values if not value]

    def missing_databricks_settings(self) -> list[str]:
        values = (("DATABRICKS_HOST", self.databricks_host), ("DATABRICKS_HTTP_PATH", self.databricks_http_path), ("DATABRICKS_TOKEN", self.databricks_token))
        return [name for name, value in values if not value]

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
            databricks_host=os.environ.get("DATABRICKS_HOST", "").strip(),
            databricks_http_path=os.environ.get("DATABRICKS_HTTP_PATH", "").strip(),
            databricks_token=os.environ.get("DATABRICKS_TOKEN", "").strip(),
            databricks_table=get("databricks_table", cls.databricks_table),
            zoho_client_id=os.environ.get("ZOHO_CLIENT_ID", "").strip(),
            zoho_client_secret=os.environ.get("ZOHO_CLIENT_SECRET", "").strip(),
            zoho_refresh_token=os.environ.get("ZOHO_REFRESH_TOKEN", "").strip(),
            zoho_accounts_url=os.environ.get("ZOHO_ACCOUNTS_URL", "").strip() or cls.zoho_accounts_url,
            zoho_api_domain=os.environ.get("ZOHO_API_DOMAIN", "").strip(),
            zoho_timeout_s=get("zoho_timeout_s", cls.zoho_timeout_s, float),
            zoho_credit_budget=get("zoho_credit_budget", cls.zoho_credit_budget, int),
            zoho_credit_reserve=get("zoho_credit_reserve", cls.zoho_credit_reserve, int),
            payees=get("payees", cls.payees),
        )
