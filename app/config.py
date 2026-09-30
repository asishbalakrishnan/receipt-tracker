"""Settings read from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("RT_DATA_DIR", BASE_DIR / "data")))
    model: str = field(default_factory=lambda: os.environ.get("RT_MODEL", "gpt-4o"))
    # Any OpenAI-compatible endpoint. OPENAI_API_KEY is accepted as a fallback for the key.
    llm_api_key: str = field(default_factory=lambda: os.environ.get("RT_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY", ""))
    llm_base_url: str = field(default_factory=lambda: os.environ.get("RT_LLM_BASE_URL") or "https://api.openai.com/v1")
    llm_base_url_explicit: bool = field(default_factory=lambda: bool(os.environ.get("RT_LLM_BASE_URL")))
    pdf_mode: str = field(default_factory=lambda: os.environ.get("RT_PDF_MODE", "auto"))  # auto | images | file
    json_mode: bool = field(default_factory=lambda: os.environ.get("RT_JSON_MODE", "1") != "0")
    fernet_key: str = field(default_factory=lambda: os.environ.get("RT_KEY", ""))
    token: str = field(default_factory=lambda: os.environ.get("RT_TOKEN", ""))  # long-lived API token for scripts and Shortcuts
    password_hash: str = field(default_factory=lambda: os.environ.get("RT_PASSWORD_HASH", ""))  # browser login (python -m app.auth)
    # Cloudflare Access (Zero Trust). Set both to require a valid Access token on every request.
    access_team_domain: str = field(default_factory=lambda: os.environ.get("RT_ACCESS_TEAM_DOMAIN", ""))  # yourteam.cloudflareaccess.com
    access_aud: str = field(default_factory=lambda: os.environ.get("RT_ACCESS_AUD", ""))  # the Access application's AUD tag
    access_emails: str = field(default_factory=lambda: os.environ.get("RT_ACCESS_EMAILS", ""))  # optional extra check, comma-separated
    require_auth: bool = field(default_factory=lambda: os.environ.get("RT_REQUIRE_AUTH", "0") == "1")  # refuse to start with no login (set in docker-compose)
    session_days: int = field(default_factory=lambda: int(os.environ.get("RT_SESSION_DAYS", "30")))
    sync_ingest: bool = field(default_factory=lambda: os.environ.get("RT_SYNC_INGEST", "0") == "1")  # read receipts inside the upload request (tests)
    confidence_threshold: float = field(
        default_factory=lambda: float(os.environ.get("RT_CONFIDENCE_THRESHOLD", "0.8"))
    )
    # Email intake: a dedicated mailbox that the app checks over IMAP. Disabled unless host, user, password
    # and at least one allowed sender are set.
    imap_host: str = field(default_factory=lambda: os.environ.get("RT_IMAP_HOST", ""))
    imap_port: int = field(default_factory=lambda: int(os.environ.get("RT_IMAP_PORT", "993")))
    imap_user: str = field(default_factory=lambda: os.environ.get("RT_IMAP_USER", ""))
    imap_password: str = field(default_factory=lambda: os.environ.get("RT_IMAP_PASSWORD", ""))
    imap_folder: str = field(default_factory=lambda: os.environ.get("RT_IMAP_FOLDER", "INBOX"))
    imap_done_folder: str = field(default_factory=lambda: os.environ.get("RT_IMAP_DONE_FOLDER", "Processed"))
    imap_reject_folder: str = field(default_factory=lambda: os.environ.get("RT_IMAP_REJECT_FOLDER", "Rejected"))
    mail_allowed_senders: str = field(default_factory=lambda: os.environ.get("RT_MAIL_ALLOWED_SENDERS", ""))
    mail_require_auth: bool = field(default_factory=lambda: os.environ.get("RT_MAIL_REQUIRE_AUTH", "1") != "0")
    mail_poll_seconds: int = field(default_factory=lambda: int(os.environ.get("RT_MAIL_POLL_SECONDS", "120")))
    mail_address: str = field(default_factory=lambda: os.environ.get("RT_MAIL_ADDRESS", ""))  # shown in the app; defaults to the IMAP user
    ntfy_url: str = field(default_factory=lambda: os.environ.get("RT_NTFY_URL", ""))  # e.g. https://ntfy.sh/<long-random-topic>
    ntfy_token: str = field(default_factory=lambda: os.environ.get("RT_NTFY_TOKEN", ""))
    ntfy_details: bool = field(default_factory=lambda: os.environ.get("RT_NTFY_DETAILS", "0") == "1")
    public_url: str = field(default_factory=lambda: os.environ.get("RT_PUBLIC_URL", ""))
    max_upload_bytes: int = 15 * 1024 * 1024
    few_shot_corrections: int = 20

    @property
    def access_enabled(self) -> bool:
        return bool(self.access_team_domain and self.access_aud)

    @property
    def allowed_senders(self) -> set[str]:
        return {a.strip().lower() for a in self.mail_allowed_senders.split(",") if a.strip()}

    @property
    def mail_enabled(self) -> bool:
        return bool(self.imap_host and self.imap_user and self.imap_password and self.allowed_senders)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "receipts.db"

    @property
    def files_dir(self) -> Path:
        return self.data_dir / "files"
