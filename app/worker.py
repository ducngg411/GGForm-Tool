from __future__ import annotations

import hashlib
import json
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import httpx

from .answer_rules import build_answers
from .form_public import fetch_form_schema, is_confirmation_page, response_fingerprint
from . import job_store as store
from .settings import MAX_ATTEMPTS, REQUEST_TIMEOUT_SECONDS
from .xlsx_io import XlsxReader, update_status_column


_active_jobs: set[str] = set()
_active_lock = threading.Lock()


class SubmissionRateLimiter:
    """Thread-safe limiter with an optional randomized delay between POST attempts."""

    def __init__(
        self,
        records_per_minute: int,
        random_delay: bool = False,
        delay_min_seconds: float = 5,
        delay_max_seconds: float = 30,
        rng: Any = None,
    ):
        self.interval_seconds = 60.0 / max(1, records_per_minute)
        self.random_delay = random_delay
        self.delay_min_seconds = delay_min_seconds
        self.delay_max_seconds = delay_max_seconds
        self._rng = rng or random
        self._next_slot = time.monotonic()
        self._lock = threading.Lock()

    def next_delay_seconds(self) -> float:
        if not self.random_delay:
            return self.interval_seconds
        randomized = self._rng.uniform(self.delay_min_seconds, self.delay_max_seconds)
        return max(self.interval_seconds, randomized)

    def wait(self, canceled: Any) -> bool:
        while True:
            if canceled():
                return False
            with self._lock:
                now = time.monotonic()
                if now >= self._next_slot:
                    self._next_slot = max(now, self._next_slot) + self.next_delay_seconds()
                    return True
                remaining = self._next_slot - now
            time.sleep(min(remaining, 0.5))


def _record_key(file_hash: str, sheet_name: str, row_number: int, source: dict[str, str]) -> str:
    canonical = json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(
        f"{file_hash}|{sheet_name}|{row_number}|{canonical}".encode("utf-8")
    ).hexdigest()


def _payload(schema_hidden: dict[str, str], submission_json: str) -> dict[str, str]:
    audit = json.loads(submission_json)
    payload = dict(audit["fields"])
    for key in ("fvv", "partialResponse", "pageHistory", "fbzx", "submissionTimestamp"):
        if key in schema_hidden:
            payload[key] = schema_hidden[key]
    payload.setdefault("fvv", "1")
    payload.setdefault("pageHistory", "0")
    payload.setdefault("submissionTimestamp", "-1")
    return payload


def _status_cell(record: dict[str, Any]) -> str:
    status = record["status"]
    if status in {"SUCCESS", "ALREADY_SUCCESS"}:
        return "SUCCESS"
    if status == "DRY_RUN_OK":
        return "DRY_RUN_OK | CHƯA GỬI FORM"
    if status in {"FAILED_FINAL", "PREPARE_FAILED"}:
        code = record.get("error_code") or "ERROR"
        message = (record.get("error_message") or "").replace("\r", " ").replace("\n", " ")
        return f"FAILED | {code} | {message}"[:32000]
    if status == "CANCELED":
        return "CANCELED"
    return status


def _checkpoint(job_id: str, workbook_path: str, sheet_name: str) -> None:
    records = store.get_records(job_id)
    statuses = {
        record["row_number"]: _status_cell(record)
        for record in records
        if record["status"] in {
            "SUCCESS", "ALREADY_SUCCESS", "DRY_RUN_OK", "FAILED_FINAL",
            "PREPARE_FAILED", "CANCELED",
        }
    }
    try:
        update_status_column(workbook_path, sheet_name, statuses)
    except PermissionError as exc:
        raise RuntimeError(
            "Không ghi được cột F vì file Excel đang mở hoặc bị khóa. Hãy đóng file rồi chạy lại."
        ) from exc


def _submit_record(
    job: dict[str, Any],
    record: dict[str, Any],
    action_url: str,
    hidden_fields: dict[str, str],
    client: httpx.Client,
    rate_limiter: SubmissionRateLimiter,
) -> dict[str, Any]:
    current_job = store.get_job(job["id"])
    if not current_job or current_job["status"] == "CANCEL_REQUESTED":
        return {**record, "status": "CANCELED", "error_code": "CANCELED"}
    payload = _payload(hidden_fields, record["submission_json"])
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": job["form_url"],
        "Origin": "https://docs.google.com",
    }
    for attempt_no in range(1, MAX_ATTEMPTS + 1):
        record.update(status="SUBMITTING", attempts=attempt_no)
        if not rate_limiter.wait(
            lambda: (store.get_job(job["id"]) or {}).get("status") == "CANCEL_REQUESTED"
        ):
            return {**record, "status": "CANCELED", "error_code": "CANCELED"}
        try:
            response = client.post(action_url, data=payload, headers=headers)
            fingerprint = response_fingerprint(response)
            response_url = str(response.url)
            if is_confirmation_page(response):
                return {
                    **record, "status": "SUCCESS", "error_code": None,
                    "error_message": None, "http_status": response.status_code,
                    "response_url": response_url, "response_hash": fingerprint,
                }
            if response.status_code == 429 or response.status_code >= 500:
                message = f"Google trả HTTP {response.status_code}."
                if attempt_no < MAX_ATTEMPTS:
                    time.sleep((1.5 * (2 ** (attempt_no - 1))) + random.random())
                    continue
                return {
                    **record, "status": "FAILED_FINAL", "error_code": "RETRY_EXHAUSTED",
                    "error_message": message, "http_status": response.status_code,
                    "response_url": response_url, "response_hash": fingerprint,
                }
            return {
                **record, "status": "FAILED_FINAL", "error_code": "CONFIRMATION_NOT_FOUND",
                "error_message": (
                    f"Không thấy câu xác nhận của Google (HTTP {response.status_code}); "
                    "không tự retry để tránh gửi trùng."
                ),
                "http_status": response.status_code, "response_url": response_url,
                "response_hash": fingerprint,
            }
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            if attempt_no < MAX_ATTEMPTS:
                time.sleep((1.5 * (2 ** (attempt_no - 1))) + random.random())
                continue
            return {
                **record, "status": "FAILED_FINAL", "error_code": "CONNECT_ERROR",
                "error_message": f"Không kết nối được tới Google: {exc}",
            }
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as exc:
            return {
                **record, "status": "FAILED_FINAL", "error_code": "UNCERTAIN_TIMEOUT",
                "error_message": (
                    f"Timeout sau khi bắt đầu gửi: {exc}. Không tự retry vì có thể Google đã nhận."
                ),
            }
        except Exception as exc:
            return {
                **record, "status": "FAILED_FINAL", "error_code": "UNEXPECTED_ERROR",
                "error_message": f"{type(exc).__name__}: {exc}",
            }
    return {**record, "status": "FAILED_FINAL", "error_code": "RETRY_EXHAUSTED"}


def _prepare_records(job: dict[str, Any], schema: Any, upload: dict[str, Any]) -> list[dict[str, Any]]:
    _, rows = XlsxReader(upload["stored_path"]).records(job["sheet_name"])
    records: list[dict[str, Any]] = []
    for row_number, source in rows:
        existing = str(source.get("FORM_STATUS", "")).strip().upper()
        base = {
            "row_number": row_number, "source": source, "status": "READY", "attempts": 0,
            "generated_phone": False, "submitted_phone": None, "error_code": None,
            "error_message": None, "http_status": None, "response_url": None,
            "response_hash": None, "updated_at": store.utc_now(),
        }
        if existing.startswith("SUCCESS"):
            records.append({**base, "status": "ALREADY_SUCCESS"})
            continue
        record_key = _record_key(upload["file_sha256"], job["sheet_name"], row_number, source)
        try:
            answers = build_answers(schema, source, job["job_seed"], record_key)
            records.append({
                **base, "record_key": record_key, "submission_json": answers.audit_json,
                "generated_phone": answers.generated_phone,
                "submitted_phone": answers.submitted_phone,
                "status": "DRY_RUN_OK" if job["dry_run"] else "READY",
            })
        except Exception as exc:
            records.append({
                **base, "record_key": record_key, "submission_json": None,
                "status": "PREPARE_FAILED", "error_code": "PREPARE_ERROR",
                "error_message": str(exc),
            })
    return records


def run_job(job_id: str) -> None:
    with _active_lock:
        if job_id in _active_jobs:
            return
        _active_jobs.add(job_id)
    try:
        job = store.get_job(job_id)
        if not job:
            return
        upload = store.get_upload(job["upload_id"])
        if not upload:
            raise ValueError("Không tìm thấy file upload của job.")
        store.set_job(job_id, status="PREPARING", started_at=store.utc_now(), last_error=None)
        schema = fetch_form_schema(job["form_url"])
        store.set_job(job_id, schema_hash=schema.schema_hash)
        records = _prepare_records(job, schema, upload)
        store.set_records(job_id, records)
        _checkpoint(job_id, upload["stored_path"], job["sheet_name"])
        if job["dry_run"]:
            store.set_job(job_id, status="DRY_RUN_COMPLETE", finished_at=store.utc_now())
            return

        ready = [record for record in records if record["status"] == "READY"]
        store.set_job(job_id, status="RUNNING")
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/128 Safari/537.36"
            ),
            "Accept-Language": "vi,en;q=0.8",
        }
        with httpx.Client(
            timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=True, headers=headers,
            limits=httpx.Limits(
                max_connections=max(2, job["concurrency"] * 2),
                max_keepalive_connections=max(2, job["concurrency"]),
            ),
        ) as client:
            rate_limiter = SubmissionRateLimiter(
                job["records_per_minute"],
                job["random_delay"],
                job["delay_min_seconds"],
                job["delay_max_seconds"],
            )
            with ThreadPoolExecutor(max_workers=job["concurrency"]) as pool:
                future_map = {
                    pool.submit(
                        _submit_record, job, record, schema.action_url, schema.hidden_fields,
                        client, rate_limiter
                    ): record["row_number"]
                    for record in ready
                }
                completed_since_checkpoint = 0
                last_checkpoint = time.monotonic()
                for future in as_completed(future_map):
                    result = future.result()
                    store.update_record(job_id, result["row_number"], **{
                        key: value for key, value in result.items() if key != "row_number"
                    })
                    completed_since_checkpoint += 1
                    if completed_since_checkpoint >= 10 or time.monotonic() - last_checkpoint >= 2:
                        _checkpoint(job_id, upload["stored_path"], job["sheet_name"])
                        completed_since_checkpoint = 0
                        last_checkpoint = time.monotonic()
        _checkpoint(job_id, upload["stored_path"], job["sheet_name"])
        final = store.refresh(job_id)
        status = "COMPLETE_WITH_ERRORS" if final and final["failed"] else "COMPLETE"
        if store.get_job(job_id)["status"] == "CANCEL_REQUESTED":
            status = "CANCELED"
        store.set_job(job_id, status=status, finished_at=store.utc_now())
    except Exception as exc:
        store.set_job(
            job_id, status="JOB_FAILED", finished_at=store.utc_now(),
            last_error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        with _active_lock:
            _active_jobs.discard(job_id)


def start_job(job_id: str) -> None:
    threading.Thread(
        target=run_job, args=(job_id,), name=f"hvcs-job-{job_id[:8]}", daemon=True
    ).start()


def is_active(job_id: str) -> bool:
    with _active_lock:
        return job_id in _active_jobs
