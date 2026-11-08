from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class Settings:
    environment: str
    database_path: Path
    upload_dir: Path
    secret_key: str
    admin_email: str
    admin_password_hash: str
    allowed_hosts: tuple[str, ...]
    cors_origins: tuple[str, ...]
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    smtp_from: str
    smtp_starttls: bool
    # False when PRINTSHOP_SECRET_KEY is missing and a temporary random key was generated for this run.
    secret_key_persistent: bool = True

    @property
    def smtp_configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_from)

    @property
    def production(self) -> bool:
        return self.environment == "production"

    def validate(self) -> None:
        if self.environment not in {"development", "test", "production"}:
            raise RuntimeError("PRINTSHOP_ENVIRONMENT must be development, test, or production.")
        if not self.production:
            return
        required = {
            "PRINTSHOP_SECRET_KEY": os.environ.get("PRINTSHOP_SECRET_KEY", ""),
            "PRINTSHOP_ADMIN_EMAIL": self.admin_email,
            "PRINTSHOP_ADMIN_PASSWORD_HASH": self.admin_password_hash,
            "PRINTSHOP_DATABASE_PATH": os.environ.get("PRINTSHOP_DATABASE_PATH", ""),
            "PRINTSHOP_UPLOAD_DIR": os.environ.get("PRINTSHOP_UPLOAD_DIR", ""),
            "PRINTSHOP_ALLOWED_HOSTS": os.environ.get("PRINTSHOP_ALLOWED_HOSTS", ""),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise RuntimeError("Production configuration is missing: " + ", ".join(missing))
        if len(self.secret_key) < 32:
            raise RuntimeError("PRINTSHOP_SECRET_KEY must contain at least 32 characters.")
        if not self.allowed_hosts or "*" in self.allowed_hosts:
            raise RuntimeError("PRINTSHOP_ALLOWED_HOSTS must contain explicit host names.")
        if not self.admin_password_hash.startswith("pbkdf2_sha256$"):
            raise RuntimeError("PRINTSHOP_ADMIN_PASSWORD_HASH must be a PBKDF2-SHA256 password hash.")


def _csv_setting(name: str, default: str = "") -> tuple[str, ...]:
    return tuple(value.strip() for value in os.environ.get(name, default).split(",") if value.strip())


def _load_env_file() -> None:
    """Read KEY=VALUE lines from the project's .env file for local development.

    Variables already set in the environment always win. Set PRINTSHOP_ENV_FILE to another path,
    or to an empty value to skip the file (the automated tests do this).
    """
    path_text = os.environ.get("PRINTSHOP_ENV_FILE", str(PROJECT_DIR / ".env"))
    if not path_text:
        return
    path = Path(path_text)
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


_load_env_file()
_environment = os.environ.get("PRINTSHOP_ENVIRONMENT", "development").strip().lower()
_database_path = os.environ.get(
    "PRINTSHOP_DATABASE_PATH",
    os.environ.get("PRINTSHOP_ORDER_DB", str(PROJECT_DIR / "backend" / "data" / "orders.sqlite3")),
)

settings = Settings(
    environment=_environment,
    database_path=Path(_database_path).expanduser().resolve(),
    upload_dir=Path(
        os.environ.get("PRINTSHOP_UPLOAD_DIR", str(PROJECT_DIR / "backend" / "data" / "order_uploads"))
    ).expanduser().resolve(),
    # Signs session cookies and keys the stored OTP hashes. Keep it stable (set it in .env) so restarts
    # do not sign everyone out or cancel password resets; without it a temporary key is generated.
    secret_key=os.environ.get("PRINTSHOP_SECRET_KEY", "") or secrets.token_urlsafe(48),
    secret_key_persistent=bool(os.environ.get("PRINTSHOP_SECRET_KEY", "")),
    admin_email=os.environ.get("PRINTSHOP_ADMIN_EMAIL", "").strip().lower(),
    admin_password_hash=os.environ.get("PRINTSHOP_ADMIN_PASSWORD_HASH", ""),
    allowed_hosts=_csv_setting("PRINTSHOP_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver"),
    cors_origins=_csv_setting("PRINTSHOP_CORS_ORIGINS"),
    smtp_host=os.environ.get("PRINTSHOP_SMTP_HOST", "").strip(),
    smtp_port=int(os.environ.get("PRINTSHOP_SMTP_PORT", "587") or "587"),
    smtp_username=os.environ.get("PRINTSHOP_SMTP_USERNAME", "").strip(),
    smtp_password=os.environ.get("PRINTSHOP_SMTP_PASSWORD", ""),
    smtp_from=os.environ.get("PRINTSHOP_SMTP_FROM", "").strip(),
    smtp_starttls=os.environ.get("PRINTSHOP_SMTP_STARTTLS", "true").strip().lower() not in {"0", "false", "no"},
)