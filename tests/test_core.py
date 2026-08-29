from __future__ import annotations

import tempfile
import unittest
import random
from pathlib import Path

from app.answer_rules import phone_for_form, resolve_form_option
from app.worker import SubmissionRateLimiter
from app.xlsx_io import XlsxReader, update_status_column, write_xlsx


class PhoneRulesTest(unittest.TestCase):
    def test_adds_leading_zero_to_nine_digits(self) -> None:
        value, generated = phone_for_form("912170353", "seed", "row-2")
        self.assertEqual(value, "0912170353")
        self.assertFalse(generated)

    def test_keeps_valid_ten_digit_phone(self) -> None:
        value, generated = phone_for_form("0912170353", "seed", "row-2")
        self.assertEqual(value, "0912170353")
        self.assertFalse(generated)

    def test_invalid_phone_is_deterministic_mobile(self) -> None:
        first, generated = phone_for_form("#N/A", "seed", "row-62")
        second, _ = phone_for_form("#N/A", "seed", "row-62")
        self.assertTrue(generated)
        self.assertEqual(first, second)
        self.assertRegex(first, r"^0\d{9}$")

    def test_form_option_match_ignores_case_and_accents(self) -> None:
        self.assertEqual(
            resolve_form_option(("Phường Thành Nam",), "phường thành nam", "Đơn vị tổ chức"),
            "Phường Thành Nam",
        )

    def test_ten_records_per_minute_is_six_second_spacing(self) -> None:
        self.assertEqual(SubmissionRateLimiter(10).interval_seconds, 6.0)

    def test_random_delay_stays_inside_range_and_respects_rate_cap(self) -> None:
        limiter = SubmissionRateLimiter(
            10, random_delay=True, delay_min_seconds=5, delay_max_seconds=30,
            rng=random.Random(7),
        )
        samples = [limiter.next_delay_seconds() for _ in range(100)]
        self.assertGreaterEqual(min(samples), 6.0)
        self.assertLessEqual(max(samples), 30.0)


class XlsxRoundTripTest(unittest.TestCase):
    def test_failure_export_can_be_read_back(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "failures.xlsx"
            write_xlsx(
                path,
                ["STT", "Họ và tên", "_error"],
                [["1", "NGUYỄN VĂN A", "Timeout"]],
            )
            reader = XlsxReader(path)
            self.assertEqual([sheet.name for sheet in reader.list_sheets()], ["Failures"])
            headers, rows = reader.records("Failures")
            self.assertEqual(headers, ["STT", "Họ và tên", "_error"])
            self.assertEqual(rows[0][1]["Họ và tên"], "NGUYỄN VĂN A")

    def test_status_is_written_to_column_f(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.xlsx"
            write_xlsx(
                path,
                ["STT", "Họ và tên", "Ngày sinh", "Giới tính", "SĐT"],
                [["1", "NGUYỄN VĂN A", "01/01/1990", "Nam", "912345678"]],
            )
            update_status_column(path, "Failures", {2: "SUCCESS"})
            headers, rows = XlsxReader(path).records("Failures")
            self.assertEqual(headers[5], "FORM_STATUS")
            self.assertEqual(rows[0][1]["FORM_STATUS"], "SUCCESS")


if __name__ == "__main__":
    unittest.main()
