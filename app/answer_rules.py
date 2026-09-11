from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Mapping

from .form_public import FormSchema, Question, normalize_text


MOBILE_PREFIXES = (
    "032", "033", "034", "035", "036", "037", "038", "039",
    "070", "076", "077", "078", "079",
    "081", "082", "083", "084", "085", "086", "088", "089",
    "090", "091", "092", "093", "094", "096", "097", "098", "099",
)


def _digest(seed: str, record_key: str, purpose: str) -> bytes:
    return hashlib.sha256(f"{seed}|{record_key}|{purpose}".encode("utf-8")).digest()


def deterministic_choice(options: tuple[str, ...], seed: str, record_key: str, purpose: str) -> str:
    if not options:
        raise ValueError(f"Câu hỏi '{purpose}' không có option.")
    number = int.from_bytes(_digest(seed, record_key, purpose)[:8], "big")
    return options[number % len(options)]


def deterministic_phone(seed: str, record_key: str) -> str:
    digest = _digest(seed, record_key, "fallback-phone")
    prefix = MOBILE_PREFIXES[int.from_bytes(digest[:2], "big") % len(MOBILE_PREFIXES)]
    tail_number = int.from_bytes(digest[2:10], "big") % 10_000_000
    return f"{prefix}{tail_number:07d}"


def resolve_form_option(options: tuple[str, ...], desired: str, title: str) -> str:
    """Return the Form's exact option text using accent/case-insensitive matching."""
    if desired in options:
        return desired
    desired_key = normalize_text(desired)
    matches = [option for option in options if normalize_text(option) == desired_key]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError(f"Option '{desired}' không còn trong '{title}'.")
    raise ValueError(f"Option '{desired}' bị trùng/không rõ ràng trong '{title}'.")


def phone_for_form(raw: str, seed: str, record_key: str) -> tuple[str, bool]:
    text = str(raw or "").strip()
    digits = re.sub(r"\D", "", text)
    if len(digits) == 9 and digits != "0" * 9:
        return "0" + digits, False
    if len(digits) == 10 and digits.startswith("0") and digits != "0" * 10:
        return digits, False
    return deterministic_phone(seed, record_key), True


def _source_value(source: Mapping[str, str], *aliases: str) -> str:
    normalized = {normalize_text(key): str(value) for key, value in source.items()}
    for alias in aliases:
        if normalize_text(alias) in normalized:
            return normalized[normalize_text(alias)]
    raise ValueError(f"Excel thiếu cột bắt buộc: {aliases[0]}")


def _single_entry(question: Question) -> int:
    if len(question.entries) != 1:
        raise ValueError(f"Câu hỏi '{question.title}' không phải trường đơn.")
    return question.entries[0].entry_id


def _choose(question: Question, seed: str, record_key: str, allowed: tuple[str, ...] | None = None) -> str:
    entry = question.entries[0]
    options = (
        tuple(resolve_form_option(entry.options, option, question.title) for option in allowed)
        if allowed
        else entry.options
    )
    return deterministic_choice(options, seed, record_key, question.title)


@dataclass(frozen=True)
class BuiltAnswers:
    fields: dict[str, str]
    generated_phone: bool
    source_phone: str
    submitted_phone: str
    audit_json: str


def build_answers(
    schema: FormSchema,
    source: Mapping[str, str],
    seed: str,
    record_key: str,
) -> BuiltAnswers:
    fields: dict[str, str] = {}

    name = _source_value(source, "Họ và tên", "Ho va ten")
    birth = _source_value(source, "Ngày sinh", "Năm sinh")
    source_phone = _source_value(source, "SĐT", "SDT", "Số điện thoại")
    phone, generated_phone = phone_for_form(source_phone, seed, record_key)

    def set_text(title: str, value: str) -> None:
        question = schema.find(title)
        fields[f"entry.{_single_entry(question)}"] = value

    def set_fixed(title: str, value: str) -> None:
        question = schema.find(title)
        exact_value = (
            resolve_form_option(question.entries[0].options, value, question.title)
            if question.entries[0].options
            else value
        )
        fields[f"entry.{_single_entry(question)}"] = exact_value

    def set_random(title: str, allowed: tuple[str, ...] | None = None) -> None:
        question = schema.find(title)
        fields[f"entry.{_single_entry(question)}"] = _choose(
            question, seed, record_key, allowed
        )

    set_text("Họ và tên", name)
    set_text("Năm sinh", birth)
    set_text("Nghề nghiệp", "Tự do")
    set_text("Phương tiện thường sử dụng", "Xe máy")
    set_fixed("Đơn vị tổ chức", "phường Thành Nam")
    set_text("SĐT người được khảo sát", phone)
    set_fixed("nắm được quy định", "Biết rõ")

    behavior_grid = schema.find("thường xuyên thực hiện các nội dung")
    for entry in behavior_grid.entries:
        value = "Thường xuyên"
        if "quan sat, bat tin hieu" in normalize_text(entry.row_label):
            bucket = int.from_bytes(_digest(seed, record_key, entry.row_label)[:4], "big") % 5
            value = "Thỉnh thoảng" if bucket == 0 else "Thường xuyên"
        fields[f"entry.{entry.entry_id}"] = resolve_form_option(
            entry.options, value, entry.row_label
        )

    set_random("hành vi nào dễ gây tai nạn")
    set_fixed("cấm điều khiển phương tiện sau khi sử dụng rượu bia", "Có")
    set_random("thường gặp các hành vi vi phạm")
    set_random("nguyên nhân chủ yếu dẫn đến vi phạm")
    set_random("đánh giá tình hình TTATGT", ("Tốt", "Cơ bản ổn định"))

    criminal_grid = schema.find("có thể bị xử lý hình sự")
    for entry in criminal_grid.entries:
        fields[f"entry.{entry.entry_id}"] = resolve_form_option(
            entry.options, "Có biết", entry.row_label
        )

    set_random("thanh thiếu niên hiện nay thường vi phạm")
    set_random("Nguyên nhân thanh thiếu niên")
    set_random("tiếp cận thông tin giáo dục giao thông")
    set_random("Hình thức giáo dục nào dễ hiểu")
    set_random("xem clip tai nạn giao thông thực tế", ("Rất hiệu quả", "Có hiệu quả"))
    set_random("Nội dung giáo dục nào cần được phổ biến")

    set_fixed("có đề xuất gì", "Không")

    audit = {
        "record_key": record_key,
        "generated_phone": generated_phone,
        "source_phone": source_phone,
        "submitted_phone": phone,
        "fields": fields,
    }
    return BuiltAnswers(
        fields=fields,
        generated_phone=generated_phone,
        source_phone=source_phone,
        submitted_phone=phone,
        audit_json=json.dumps(audit, ensure_ascii=False, sort_keys=True),
    )
