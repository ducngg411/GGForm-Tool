from __future__ import annotations

import hashlib
import re
import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .form_public import fetch_form_schema
from . import job_store as store
from .settings import (
    DEFAULT_CONCURRENCY,
    DEFAULT_FORM_URL,
    DEFAULT_RECORDS_PER_MINUTE,
    MAX_CONCURRENCY,
    MAX_RECORDS_PER_MINUTE,
    MAX_UPLOAD_BYTES,
    UPLOAD_DIR,
    ensure_data_dirs,
)
from .worker import is_active, start_job
from .xlsx_io import XlsxReader


app = FastAPI(title="HVCS Google Form Tool", version="1.0.0")


class JobRequest(BaseModel):
    upload_id: str
    sheet_name: str
    form_url: str = DEFAULT_FORM_URL
    concurrency: int = Field(default=DEFAULT_CONCURRENCY, ge=1, le=MAX_CONCURRENCY)
    records_per_minute: int = Field(
        default=DEFAULT_RECORDS_PER_MINUTE, ge=1, le=MAX_RECORDS_PER_MINUTE
    )
    random_delay: bool = True
    delay_min_seconds: float = Field(default=5, ge=0, le=30)
    delay_max_seconds: float = Field(default=30, ge=0, le=30)
    dry_run: bool = True


@app.on_event("startup")
def startup() -> None:
    ensure_data_dirs()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(
        Path(__file__).parent / "static" / "index.html",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@app.get("/api/config")
def config() -> dict[str, object]:
    return {
        "default_form_url": DEFAULT_FORM_URL,
        "default_concurrency": DEFAULT_CONCURRENCY,
        "max_concurrency": MAX_CONCURRENCY,
        "default_records_per_minute": DEFAULT_RECORDS_PER_MINUTE,
        "max_records_per_minute": MAX_RECORDS_PER_MINUTE,
    }


@app.post("/api/uploads")
async def upload_workbook(file: UploadFile = File(...)) -> dict[str, object]:
    original_name = file.filename or "input.xlsx"
    if not original_name.lower().endswith(".xlsx"):
        raise HTTPException(400, "Chỉ hỗ trợ file .xlsx.")
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "File vượt quá giới hạn 100 MB.")
    file_hash = hashlib.sha256(content).hexdigest()
    safe_stem = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(original_name).stem).strip("_") or "input"
    stored_path = UPLOAD_DIR / f"{uuid.uuid4().hex[:10]}_{file_hash[:8]}_{safe_stem}.xlsx"
    stored_path.write_bytes(content)
    try:
        sheets = XlsxReader(stored_path).list_sheets()
    except Exception as exc:
        stored_path.unlink(missing_ok=True)
        raise HTTPException(400, f"Không đọc được XLSX: {exc}") from exc
    item = store.create_upload(original_name, stored_path, file_hash, direct_file=False)
    return {
        "upload": {
            key: item[key]
            for key in ("id", "original_name", "file_sha256", "direct_file", "created_at")
        },
        "sheets": [sheet.__dict__ for sheet in sheets],
    }


@app.post("/api/local-files/select")
async def select_local_workbook() -> dict[str, object]:
    """Open the native Windows picker and register the original XLSX for reading."""
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askopenfilename(
            parent=root,
            title="Chọn file Excel để đọc dữ liệu",
            filetypes=[("Excel workbook", "*.xlsx")],
        )
        root.destroy()
    except Exception as exc:
        raise HTTPException(500, f"Không mở được hộp thoại chọn file: {exc}") from exc
    if not selected:
        raise HTTPException(400, "Chưa chọn file.")
    path = Path(selected).resolve()
    if path.suffix.lower() != ".xlsx" or not path.is_file():
        raise HTTPException(400, "File được chọn không phải XLSX hợp lệ.")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        sheets = XlsxReader(path).list_sheets()
    except Exception as exc:
        raise HTTPException(
            400, f"Không đọc được file. Hãy đóng Excel rồi thử lại: {exc}"
        ) from exc
    item = store.create_upload(path.name, path, digest.hexdigest(), direct_file=True)
    return {
        "upload": {
            key: item[key]
            for key in ("id", "original_name", "file_sha256", "direct_file", "created_at")
        }
        | {"local_path": str(path)},
        "sheets": [sheet.__dict__ for sheet in sheets],
    }


@app.get("/api/uploads/{upload_id}/sheets")
def upload_sheets(upload_id: str) -> dict[str, object]:
    upload = store.get_upload(upload_id)
    if not upload:
        raise HTTPException(404, "Không tìm thấy file upload.")
    sheets = XlsxReader(upload["stored_path"]).list_sheets()
    return {"sheets": [sheet.__dict__ for sheet in sheets]}


@app.get("/api/uploads/{upload_id}/preview")
def preview_sheet(upload_id: str, sheet: str, limit: int = Query(10, ge=1, le=50)) -> dict[str, object]:
    upload = store.get_upload(upload_id)
    if not upload:
        raise HTTPException(404, "Không tìm thấy file upload.")
    headers, records = XlsxReader(upload["stored_path"]).records(sheet)
    return {
        "headers": headers,
        "total": len(records),
        "rows": [{"row_number": number, **values} for number, values in records[:limit]],
    }


@app.get("/api/form/inspect")
def inspect_form(url: str = DEFAULT_FORM_URL) -> dict[str, object]:
    try:
        schema = fetch_form_schema(url)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "source_url": schema.source_url,
        "action_url": schema.action_url,
        "schema_hash": schema.schema_hash,
        "question_count": len(schema.questions),
        "entry_count": sum(len(question.entries) for question in schema.questions),
        "required_entry_count": sum(
            entry.required for question in schema.questions for entry in question.entries
        ),
        "questions": [
            {
                "id": question.question_id,
                "title": question.title,
                "type": question.question_type,
                "entries": [
                    {
                        "id": entry.entry_id,
                        "required": entry.required,
                        "row_label": entry.row_label,
                        "options": entry.options,
                    }
                    for entry in question.entries
                ],
            }
            for question in schema.questions
        ],
    }


@app.post("/api/jobs")
def create_job(request: JobRequest) -> dict[str, object]:
    upload = store.get_upload(request.upload_id)
    if not upload:
        raise HTTPException(404, "Không tìm thấy file upload.")
    sheets = {sheet.name for sheet in XlsxReader(upload["stored_path"]).list_sheets()}
    if request.sheet_name not in sheets:
        raise HTTPException(400, "Sheet đã chọn không tồn tại.")
    if "docs.google.com/forms/" not in request.form_url:
        raise HTTPException(400, "URL không phải Google Form.")
    if request.delay_min_seconds > request.delay_max_seconds:
        raise HTTPException(400, "Random delay tối thiểu không được lớn hơn tối đa.")
    job = store.create_job(
        request.upload_id,
        request.sheet_name,
        request.form_url,
        request.concurrency,
        request.records_per_minute,
        request.random_delay,
        request.delay_min_seconds,
        request.delay_max_seconds,
        request.dry_run,
    )
    start_job(job["id"])
    return {"job": job}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict[str, object]:
    job = store.get_job(job_id)
    if not job:
        raise HTTPException(404, "Không tìm thấy job.")
    job["active"] = is_active(job_id)
    return {"job": job}


@app.get("/api/jobs/{job_id}/records")
def job_records(
    job_id: str,
    status: str | None = None,
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict[str, object]:
    if not store.get_job(job_id):
        raise HTTPException(404, "Không tìm thấy job.")
    records = store.get_records(job_id, status)
    return {"records": records[offset : offset + limit]}


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict[str, str]:
    if not store.get_job(job_id):
        raise HTTPException(404, "Không tìm thấy job.")
    store.request_cancel(job_id)
    return {"status": "CANCEL_REQUESTED"}


@app.post("/api/jobs/{job_id}/retry-failed")
def retry_failed(job_id: str) -> dict[str, object]:
    job = store.get_job(job_id)
    if not job:
        raise HTTPException(404, "Không tìm thấy job.")
    if job["dry_run"]:
        raise HTTPException(400, "Dry-run không có submit để retry.")
    if is_active(job_id):
        raise HTTPException(409, "Job vẫn đang chạy.")
    retry_job = store.create_job(
        job["upload_id"], job["sheet_name"], job["form_url"], job["concurrency"],
        job["records_per_minute"], job["random_delay"], job["delay_min_seconds"],
        job["delay_max_seconds"], False
    )
    start_job(retry_job["id"])
    return {"retry_count": job["failed"], "job": retry_job}


@app.get("/api/jobs/{job_id}/workbook.xlsx")
def download_workbook(job_id: str) -> FileResponse:
    job = store.get_job(job_id)
    if not job:
        raise HTTPException(404, "Không tìm thấy job.")
    upload = store.get_upload(job["upload_id"])
    if not upload:
        raise HTTPException(404, "Không tìm thấy workbook của job.")
    output = Path(upload["stored_path"])
    return FileResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"processed_{Path(upload['original_name']).name}",
    )
