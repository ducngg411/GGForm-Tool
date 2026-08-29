from __future__ import annotations

import hashlib
import html
import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

import httpx

from .settings import REQUEST_TIMEOUT_SECONDS


QUESTION_PATTERN = re.compile(r'data-params="%\.@\.(\[.*?\])">', re.DOTALL)
FORM_PATTERN = re.compile(r'<form\s+action="([^"]+)"[^>]*\bid="mG61Hd"', re.DOTALL)
HIDDEN_PATTERN = re.compile(r'<input\s+type="hidden"\s+name="([^"]+)"[^>]*value="([^"]*)"', re.DOTALL)


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFD", value or "")
    value = "".join(char for char in value if unicodedata.category(char) != "Mn")
    value = value.replace("đ", "d").replace("Đ", "D")
    value = re.sub(r"\s+", " ", value).strip().lower()
    return value


@dataclass(frozen=True)
class Entry:
    entry_id: int
    required: bool
    options: tuple[str, ...] = ()
    row_label: str = ""


@dataclass(frozen=True)
class Question:
    question_id: int
    title: str
    question_type: int
    entries: tuple[Entry, ...] = ()


@dataclass
class FormSchema:
    source_url: str
    action_url: str
    hidden_fields: dict[str, str]
    questions: list[Question]
    schema_hash: str
    html_text: str = field(repr=False, default="")

    def find(self, title_fragment: str) -> Question:
        needle = normalize_text(title_fragment)
        matches = [question for question in self.questions if needle in normalize_text(question.title)]
        if len(matches) != 1:
            raise ValueError(
                f"Không tìm được duy nhất câu hỏi '{title_fragment}' (matches={len(matches)})."
            )
        return matches[0]


def _entry_from_raw(raw: list[Any]) -> Entry:
    option_rows = raw[1] if len(raw) > 1 and isinstance(raw[1], list) else []
    options = tuple(str(item[0]) for item in option_rows if isinstance(item, list) and item)
    row_data = raw[3] if len(raw) > 3 and isinstance(raw[3], list) else []
    row_label = str(row_data[0]) if row_data else ""
    return Entry(
        entry_id=int(raw[0]),
        required=bool(raw[2]) if len(raw) > 2 else False,
        options=options,
        row_label=row_label,
    )


def parse_form_html(source_url: str, page: str) -> FormSchema:
    form_match = FORM_PATTERN.search(page)
    if not form_match:
        raise ValueError("Không tìm thấy endpoint formResponse; Form có thể đã đóng hoặc đổi cấu trúc.")
    questions: list[Question] = []
    for match in QUESTION_PATTERN.finditer(page):
        encoded = "[" + html.unescape(match.group(1))
        try:
            outer = json.loads(encoded)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Không đọc được schema câu hỏi Google Form: {exc}") from exc
        raw = outer[0]
        entries = tuple(_entry_from_raw(item) for item in (raw[4] or []))
        questions.append(
            Question(
                question_id=int(raw[0]),
                title=re.sub(r"<[^>]+>", "", str(raw[1] or "")).strip(),
                question_type=int(raw[3]),
                entries=entries,
            )
        )
    if not questions:
        raise ValueError("Không đọc được câu hỏi nào từ Google Form.")
    hidden = {
        name: html.unescape(value)
        for name, value in HIDDEN_PATTERN.findall(page)
        if not name.startswith("entry.")
    }
    canonical = [
        {
            "id": question.question_id,
            "title": question.title,
            "type": question.question_type,
            "entries": [
                {
                    "id": entry.entry_id,
                    "required": entry.required,
                    "options": entry.options,
                    "row": entry.row_label,
                }
                for entry in question.entries
            ],
        }
        for question in questions
    ]
    schema_hash = hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return FormSchema(
        source_url=source_url,
        action_url=html.unescape(form_match.group(1)),
        hidden_fields=hidden,
        questions=questions,
        schema_hash=schema_hash,
        html_text=page,
    )


def fetch_form_schema(url: str) -> FormSchema:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/128 Safari/537.36"
        ),
        "Accept-Language": "vi,en;q=0.8",
    }
    with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=True, headers=headers) as client:
        response = client.get(url)
        response.raise_for_status()
        return parse_form_html(str(response.url), response.text)


def is_confirmation_page(response: httpx.Response) -> bool:
    normalized = normalize_text(response.text)
    markers = (
        "cau tra loi cua ban da duoc ghi lai",
        "cau tra loi da duoc ghi lai",
        "your response has been recorded",
        "we've recorded your response",
    )
    return response.status_code == 200 and any(marker in normalized for marker in markers)


def response_fingerprint(response: httpx.Response) -> str:
    return hashlib.sha256(response.content).hexdigest()

