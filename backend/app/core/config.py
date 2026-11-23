from __future__ import annotations

import os
import secrets
import tempfile
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
    # Public URL scheme as seen by visitors. Set to "https" when a TLS-terminating proxy sits in front
    # of the app so same-origin checks compare against the public scheme, not the internal http scheme.
    public_scheme: str = ""
    # Direct peers allowed to supply X-Forwarded-For / X-Real-IP. Empty means no forwarded header is
    # trusted: requests then fall back to the immediate peer (or no IP key at all when a proxy is present).
    trusted_proxy_ips: tuple[str, ...] = ()
    # Optional allow-list for payment-provider host names. Empty accepts any public HTTPS host.
    gateway_allowed_hosts: tuple[str, ...] = ()
    # New accounts must prove they own their email address before they can sign in. On by default in
    # production; development/tests keep the original immediate-sign-in behaviour unless opted in.
    require_email_verification: bool = False
    # Turso (hosted SQLite) used instead of the local database file when the URL is set (Vercel).
    turso_database_url: str = ""
    turso_auth_token: str = ""
    # True on Vercel, whose proxy puts the visitor's real address in the X-Real-IP header.
    behind_vercel_proxy: bool = False
    # False when PRINTSHOP_SECRET_KEY is missing and a temporary random key was generated for this run.
    secret_key_persistent: bool = True
    # Backup files are signed with a dedicated key so rotating session secrets does not invalidate old
    # backups. It falls back to PRINTSHOP_SECRET_KEY when no dedicated key is configured.
    backup_signing_key: str = ""
    backup_signing_key_persistent: bool = False
    # Accepted when restoring backups made before the current signing key was installed (key rotation).
    previous_backup_signing_keys: tuple[str, ...] = ()
    # Google Gemini, used by the customer support chatbot. The key stays on the server; without it the
    # chatbot still answers from the database, but without the AI model.
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    # Upload limits (megabytes per file). Large-format categories (tarpaulins) accept bigger print files.
    max_upload_mb: int = 5
    large_format_max_upload_mb: int = 25
    large_upload_categories: tuple[str, ...] = ("large-format",)
    # Where order files (designs, references, drafts, payment proofs) are kept: "disk" (PRINTSHOP_UPLOAD_DIR,
    # needs a persistent volume) or "database" (stored in the database, shared by every copy of the app;
    # the default when Turso is configured, because Vercel has no persistent disk).
    file_storage: str = "disk"
    # OCR-assisted payment receipts. "none" disables scanning; "auto" tries the optional Tesseract provider
    # and falls back to manual review when no engine is installed. Absence is never an error.
    ocr_engine: str = "auto"
    ocr_language: str = "eng"
    ocr_max_bytes: int = 5 * 1024 * 1024
    ocr_timeout_seconds: int = 20

    @property
    def gemini_configured(self) -> bool:
        return bool(self.gemini_api_key)

    @property
    def smtp_configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_from)

    @property
    def ocr_configured(self) -> bool:
        return self.ocr_engine.casefold() not in {"", "none", "off", "false"}

    @property
    def production(self) -> bool:
        return self.environment == "production"

    def _ephemeral_storage_problem(self) -> str:
        """Why ``PRINTSHOP_FILE_STORAGE=disk`` would lose uploads on this deployment, if it would."""
        if self.file_storage != "disk":
            return ""
        if self.turso_database_url or self.behind_vercel_proxy:
            return (
                "This deployment uses a temporary filesystem (Vercel/Turso), so order files must be kept "
                "in the database. Set PRINTSHOP_FILE_STORAGE=database."
            )
        try:
            upload = self.upload_dir.resolve()
            ephemeral_roots = [Path(tempfile.gettempdir()).resolve()]
            for candidate in ("/tmp", "/var/tmp", "/dev/shm"):
                root = Path(candidate)
                if root.exists():
                    ephemeral_roots.append(root.resolve())
            if any(upload == root or root in upload.parents for root in ephemeral_roots):
                return (
                    "PRINTSHOP_UPLOAD_DIR points at a temporary directory. Production uploads need a "
                    "persistent volume, or PRINTSHOP_FILE_STORAGE=database."
                )
        except OSError:  # pragma: no cover - an unresolvable path is caught by the explicit env check below
            pass
        return ""

    def validate(self) -> None:
        if self.environment not in {"development", "test", "production"}:
            raise RuntimeError("PRINTSHOP_ENVIRONMENT must be development, test, or production.")
        if self.file_storage not in {"disk", "database"}:
            raise RuntimeError("PRINTSHOP_FILE_STORAGE must be disk or database.")
        if self.public_scheme and self.public_scheme not in {"http", "https"}:
            raise RuntimeError("PRINTSHOP_PUBLIC_SCHEME must be http or https when set.")
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
        # Render's filesystem is ephemeral on free instances; a local SQLite file would silently lose
        # real orders on every restart, so require Turso rather than letting the blueprint boot unsafely.
        if os.environ.get("RENDER", "").strip() and not self.turso_database_url:
            raise RuntimeError("Render deployments require TURSO_DATABASE_URL (and TURSO_AUTH_TOKEN); the local filesystem is temporary.")
        if self.require_email_verification and not self.smtp_configured:
            raise RuntimeError("Email verification requires PRINTSHOP_SMTP_HOST and PRINTSHOP_SMTP_FROM, or PRINTSHOP_REQUIRE_EMAIL_VERIFICATION=false.")
        ephemeral = self._ephemeral_storage_problem()
        if ephemeral:
            raise RuntimeError(ephemeral)
        if len(self.backup_signing_key) < 32:
            raise RuntimeError("PRINTSHOP_BACKUP_SIGNING_KEY (or PRINTSHOP_SECRET_KEY) must contain at least 32 characters.")
        for key in self.previous_backup_signing_keys:
            if len(key) < 32:
                raise RuntimeError("Every key in PRINTSHOP_BACKUP_PREVIOUS_SIGNING_KEYS must contain at least 32 characters.")


def _bool_setting(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off"}


def _int_setting(name: str, default: int, low: int, high: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError as error:
        raise RuntimeError(f"{name} must be a whole number of megabytes.") from error
    return max(low, min(high, value))


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


def _allowed_hosts() -> tuple[str, ...]:
    hosts = _csv_setting("PRINTSHOP_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver")
    # Render sets RENDER_EXTERNAL_HOSTNAME (e.g. myapp.onrender.com) on every web service.
    render_host = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "").strip()
    if render_host and render_host not in hosts:
        hosts += (render_host,)
    return hosts


_load_env_file()
_environment =os.environ.get("PRINTSHOP_ENVIRONMENT", "development").strip().lower()
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
    backup_signing_key=os.environ.get("PRINTSHOP_BACKUP_SIGNING_KEY", "").strip()
    or os.environ.get("PRINTSHOP_SECRET_KEY", "").strip()
    or secrets.token_urlsafe(48),
    backup_signing_key_persistent=bool(
        os.environ.get("PRINTSHOP_BACKUP_SIGNING_KEY", "").strip()
        or os.environ.get("PRINTSHOP_SECRET_KEY", "").strip()
    ),
    previous_backup_signing_keys=tuple(
        key for key in _csv_setting("PRINTSHOP_BACKUP_PREVIOUS_SIGNING_KEYS") if key
    ),
    admin_email=os.environ.get("PRINTSHOP_ADMIN_EMAIL", "").strip().lower(),
    admin_password_hash=os.environ.get("PRINTSHOP_ADMIN_PASSWORD_HASH", ""),
    allowed_hosts=_allowed_hosts(),
    cors_origins=_csv_setting("PRINTSHOP_CORS_ORIGINS"),
    public_scheme=os.environ.get("PRINTSHOP_PUBLIC_SCHEME", "").strip().lower(),
    trusted_proxy_ips=_csv_setting("PRINTSHOP_TRUSTED_PROXY_IPS"),
    gateway_allowed_hosts=_csv_setting("PRINTSHOP_GATEWAY_ALLOWED_HOSTS"),
    require_email_verification=_bool_setting("PRINTSHOP_REQUIRE_EMAIL_VERIFICATION", _environment == "production"),
    smtp_host=os.environ.get("PRINTSHOP_SMTP_HOST", "").strip(),
    smtp_port=int(os.environ.get("PRINTSHOP_SMTP_PORT", "587") or "587"),
    smtp_username=os.environ.get("PRINTSHOP_SMTP_USERNAME", "").strip(),
    smtp_password=os.environ.get("PRINTSHOP_SMTP_PASSWORD", ""),
    smtp_from=os.environ.get("PRINTSHOP_SMTP_FROM", "").strip(),
    smtp_starttls=os.environ.get("PRINTSHOP_SMTP_STARTTLS", "true").strip().lower() not in {"0", "false", "no"},
    turso_database_url=os.environ.get("TURSO_DATABASE_URL", "").strip(),
    turso_auth_token=os.environ.get("TURSO_AUTH_TOKEN", "").strip(),
    behind_vercel_proxy=bool(os.environ.get("VERCEL")),
    gemini_api_key=(os.environ.get("GEMINI_API_KEY", "") or os.environ.get("GOOGLE_API_KEY", "")).strip(),
    gemini_model=os.environ.get("GEMINI_MODEL", "").strip() or "gemini-2.5-flash",
    max_upload_mb=_int_setting("PRINTSHOP_MAX_UPLOAD_MB", 5, 1, 50),
    large_format_max_upload_mb=_int_setting("PRINTSHOP_LARGE_FORMAT_MAX_UPLOAD_MB", 25, 1, 200),
    large_upload_categories=_csv_setting("PRINTSHOP_LARGE_UPLOAD_CATEGORIES", "large-format"),
    file_storage=(
        os.environ.get("PRINTSHOP_FILE_STORAGE", "").strip().lower()
        or ("database" if os.environ.get("TURSO_DATABASE_URL", "").strip() else "disk")
    ),
    ocr_engine=os.environ.get("PRINTSHOP_OCR_ENGINE", "auto").strip().lower() or "auto",
    ocr_language=os.environ.get("PRINTSHOP_OCR_LANGUAGE", "eng").strip() or "eng",
    ocr_max_bytes=_int_setting("PRINTSHOP_OCR_MAX_MB", 5, 1, 50) * 1024 * 1024,
    ocr_timeout_seconds=_int_setting("PRINTSHOP_OCR_TIMEOUT_SECONDS", 20, 1, 120),
)