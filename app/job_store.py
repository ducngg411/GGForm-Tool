from __future__ import annotations

import copy
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_lock = threading.RLock()
_uploads: dict[str, dict[str, Any]] = {}
_jobs: dict[str, dict[str, Any]] = {}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create_upload(
    original_name: str,
    stored_path: Path,
    file_sha256: str,
    direct_file: bool = False,
) -> dict[str, Any]:
    item = {
        "id": uuid.uuid4().hex,
        "original_name": original_name,
        "stored_path": str(stored_path),
        "file_sha256": file_sha256,
        "direct_file": bool(direct_file),
        "created_at": utc_now(),
    }
    with _lock:
        _uploads[item["id"]] = item
    return copy.deepcopy(item)


def get_upload(upload_id: str) -> dict[str, Any] | None:
    with _lock:
        item = _uploads.get(upload_id)
        return copy.deepcopy(item) if item else None


def create_job(
    upload_id: str,
    sheet_name: str,
    form_url: str,
    concurrency: int,
    records_per_minute: int,
    random_delay: bool,
    delay_min_seconds: float,
    delay_max_seconds: float,
    dry_run: bool,
) -> dict[str, Any]:
    item = {
        "id": uuid.uuid4().hex,
        "upload_id": upload_id,
        "sheet_name": sheet_name,
        "form_url": form_url,
        "concurrency": concurrency,
        "records_per_minute": records_per_minute,
        "random_delay": bool(random_delay),
        "delay_min_seconds": float(delay_min_seconds),
        "delay_max_seconds": float(delay_max_seconds),
        "dry_run": bool(dry_run),
        "job_seed": uuid.uuid4().hex,
        "schema_hash": None,
        "status": "QUEUED",
        "total": 0,
        "processed": 0,
        "success": 0,
        "failed": 0,
        "generated_phones": 0,
        "created_at": utc_now(),
        "started_at": None,
        "finished_at": None,
        "last_error": None,
        "records": [],
    }
    with _lock:
        _jobs[item["id"]] = item
    return _public_job(item)


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    return {key: copy.deepcopy(value) for key, value in job.items() if key != "records"}


def get_job(job_id: str) -> dict[str, Any] | None:
    with _lock:
        item = _jobs.get(job_id)
        return _public_job(item) if item else None


def set_job(job_id: str, **values: Any) -> None:
    with _lock:
        if job_id not in _jobs:
            return
        _jobs[job_id].update(values)


def set_records(job_id: str, records: list[dict[str, Any]]) -> None:
    with _lock:
        _jobs[job_id]["records"] = records
        _refresh_locked(_jobs[job_id])


def get_records(job_id: str, status: str | None = None) -> list[dict[str, Any]]:
    with _lock:
        records = _jobs.get(job_id, {}).get("records", [])
        if status:
            records = [record for record in records if record["status"] == status]
        return copy.deepcopy(records)


def update_record(job_id: str, row_number: int, **values: Any) -> None:
    with _lock:
        for record in _jobs[job_id]["records"]:
            if record["row_number"] == row_number:
                record.update(values)
                record["updated_at"] = utc_now()
                break
        _refresh_locked(_jobs[job_id])


def _refresh_locked(job: dict[str, Any]) -> None:
    records = job["records"]
    terminal = {"SUCCESS", "DRY_RUN_OK", "FAILED_FINAL", "PREPARE_FAILED", "ALREADY_SUCCESS", "CANCELED"}
    job["total"] = len(records)
    job["processed"] = sum(record["status"] in terminal for record in records)
    job["success"] = sum(
        record["status"] in {"SUCCESS", "DRY_RUN_OK", "ALREADY_SUCCESS"} for record in records
    )
    job["failed"] = sum(record["status"] in {"FAILED_FINAL", "PREPARE_FAILED"} for record in records)
    job["generated_phones"] = sum(bool(record.get("generated_phone")) for record in records)


def refresh(job_id: str) -> dict[str, Any] | None:
    with _lock:
        if job_id not in _jobs:
            return None
        _refresh_locked(_jobs[job_id])
        return _public_job(_jobs[job_id])


def request_cancel(job_id: str) -> None:
    set_job(job_id, status="CANCEL_REQUESTED")
