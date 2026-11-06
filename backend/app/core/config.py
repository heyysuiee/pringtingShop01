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
    secret_key=os.environ.get("PRINTSHOP_SECRET_KEY", "") or secrets.token_urlsafe(48),
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