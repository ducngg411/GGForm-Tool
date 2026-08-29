from __future__ import annotations

import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("HVCS_DATA_DIR", BASE_DIR / "data")).resolve()
UPLOAD_DIR = DATA_DIR / "uploads"

DEFAULT_FORM_URL = (
    "https://docs.google.com/forms/d/"
    "1dOVQtWvl5EykI73XjCHNCsjZQzc1xDvMrzsTjVUSHP8/viewform"
    "?edit_requested=true"
)

MAX_UPLOAD_BYTES = 100 * 1024 * 1024
MAX_CONCURRENCY = 10
DEFAULT_CONCURRENCY = 3
DEFAULT_RECORDS_PER_MINUTE = 10
MAX_RECORDS_PER_MINUTE = 600
MAX_ATTEMPTS = 3
REQUEST_TIMEOUT_SECONDS = 35.0


def ensure_data_dirs() -> None:
    for path in (DATA_DIR, UPLOAD_DIR):
        path.mkdir(parents=True, exist_ok=True)
