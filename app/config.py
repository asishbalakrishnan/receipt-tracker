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
    token: str = field(default_factory=lambda: os.environ.get("RT_TOKEN", ""))
    confidence_threshold: float = field(
        default_factory=lambda: float(os.environ.get("RT_CONFIDENCE_THRESHOLD", "0.8"))
    )
    max_upload_bytes: int = 15 * 1024 * 1024
    few_shot_corrections: int = 20

    @property
    def db_path(self) -> Path:
        return self.data_dir / "receipts.db"

    @property
    def files_dir(self) -> Path:
        return self.data_dir / "files"
