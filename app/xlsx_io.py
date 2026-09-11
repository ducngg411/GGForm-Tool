from __future__ import annotations

import datetime as dt
import math
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape


MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = {"m": MAIN_NS, "r": REL_NS, "pr": PKG_REL_NS}

BUILTIN_DATE_FORMATS = set(range(14, 23)) | set(range(45, 48))


@dataclass(frozen=True)
class SheetInfo:
    name: str
    path: str
    state: str
    max_row: int
    max_column: int


def _column_index(cell_reference: str) -> int:
    letters = re.match(r"[A-Z]+", cell_reference.upper())
    if not letters:
        return 0
    value = 0
    for char in letters.group(0):
        value = value * 26 + ord(char) - 64
    return value


def _excel_datetime(serial: float) -> dt.datetime:
    # Excel's 1900 calendar includes the non-existent 1900-02-29.
    base = dt.datetime(1899, 12, 30)
    return base + dt.timedelta(days=serial)


def _looks_like_date_format(format_code: str) -> bool:
    cleaned = re.sub(r'"[^"]*"|\\.|\[[^\]]*\]', "", format_code.lower())
    return "y" in cleaned and ("d" in cleaned or "m" in cleaned)


def _display_number(value: str) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    if math.isfinite(number) and number.is_integer():
        return str(int(number))
    return format(number, ".15g")


class XlsxReader:
    """Small, dependency-free XLSX reader focused on displayed cell values."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _shared_strings(self, archive: zipfile.ZipFile) -> list[str]:
        try:
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
        except KeyError:
            return []
        values: list[str] = []
        for item in root.findall("m:si", NS):
            values.append("".join(node.text or "" for node in item.findall(".//m:t", NS)))
        return values

    def _date_style_ids(self, archive: zipfile.ZipFile) -> set[int]:
        try:
            root = ET.fromstring(archive.read("xl/styles.xml"))
        except KeyError:
            return set()
        custom: dict[int, str] = {}
        for fmt in root.findall("m:numFmts/m:numFmt", NS):
            custom[int(fmt.attrib["numFmtId"])] = fmt.attrib.get("formatCode", "")
        date_styles: set[int] = set()
        cell_xfs = root.find("m:cellXfs", NS)
        if cell_xfs is None:
            return date_styles
        for index, xf in enumerate(cell_xfs.findall("m:xf", NS)):
            num_fmt_id = int(xf.attrib.get("numFmtId", "0"))
            if num_fmt_id in BUILTIN_DATE_FORMATS or _looks_like_date_format(
                custom.get(num_fmt_id, "")
            ):
                date_styles.add(index)
        return date_styles

    def list_sheets(self) -> list[SheetInfo]:
        with zipfile.ZipFile(self.path) as archive:
            workbook = ET.fromstring(archive.read("xl/workbook.xml"))
            rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
            relationship_map = {
                rel.attrib["Id"]: rel.attrib["Target"]
                for rel in rels.findall("pr:Relationship", NS)
            }
            output: list[SheetInfo] = []
            for sheet in workbook.findall("m:sheets/m:sheet", NS):
                rel_id = sheet.attrib[f"{{{REL_NS}}}id"]
                target = relationship_map[rel_id].lstrip("/")
                sheet_path = target if target.startswith("xl/") else f"xl/{target}"
                root = ET.fromstring(archive.read(sheet_path))
                dimension = root.find("m:dimension", NS)
                reference = dimension.attrib.get("ref", "A1") if dimension is not None else "A1"
                end = reference.split(":")[-1]
                row_match = re.search(r"\d+", end)
                output.append(
                    SheetInfo(
                        name=sheet.attrib["name"],
                        path=sheet_path,
                        state=sheet.attrib.get("state", "visible"),
                        max_row=int(row_match.group(0)) if row_match else 1,
                        max_column=_column_index(end),
                    )
                )
            return output

    def rows(self, sheet_name: str) -> Iterable[tuple[int, list[str]]]:
        with zipfile.ZipFile(self.path) as archive:
            shared = self._shared_strings(archive)
            date_styles = self._date_style_ids(archive)
            sheet = next((item for item in self.list_sheets() if item.name == sheet_name), None)
            if sheet is None:
                raise ValueError(f"Không tìm thấy sheet: {sheet_name}")
            root = ET.fromstring(archive.read(sheet.path))
            for row in root.findall("m:sheetData/m:row", NS):
                row_number = int(row.attrib.get("r", "0"))
                cells: dict[int, str] = {}
                for cell in row.findall("m:c", NS):
                    column = _column_index(cell.attrib.get("r", "A1"))
                    cell_type = cell.attrib.get("t", "n")
                    value_node = cell.find("m:v", NS)
                    raw = value_node.text if value_node is not None and value_node.text else ""
                    if cell_type == "s" and raw:
                        value = shared[int(raw)]
                    elif cell_type == "inlineStr":
                        value = "".join(
                            node.text or "" for node in cell.findall(".//m:t", NS)
                        )
                    elif cell_type == "b":
                        value = "TRUE" if raw == "1" else "FALSE"
                    elif cell_type in {"str", "e"}:
                        value = raw
                    else:
                        style_id = int(cell.attrib.get("s", "0"))
                        if raw and style_id in date_styles:
                            try:
                                value = _excel_datetime(float(raw)).strftime("%d/%m/%Y")
                            except (OverflowError, ValueError):
                                value = raw
                        else:
                            value = _display_number(raw)
                    cells[column] = value
                max_column = max(cells, default=0)
                yield row_number, [cells.get(index, "") for index in range(1, max_column + 1)]

    def records(self, sheet_name: str) -> tuple[list[str], list[tuple[int, dict[str, str]]]]:
        iterator = iter(self.rows(sheet_name))
        try:
            _, raw_headers = next(iterator)
        except StopIteration:
            return [], []
        headers: list[str] = []
        seen: dict[str, int] = {}
        for index, raw_header in enumerate(raw_headers, start=1):
            base = raw_header.strip() or f"Column_{index}"
            seen[base] = seen.get(base, 0) + 1
            headers.append(base if seen[base] == 1 else f"{base}_{seen[base]}")
        records: list[tuple[int, dict[str, str]]] = []
        for row_number, values in iterator:
            padded = values + [""] * max(0, len(headers) - len(values))
            if not any(str(value).strip() for value in padded[: len(headers)]):
                continue
            records.append(
                (row_number, {header: padded[index] for index, header in enumerate(headers)})
            )
        return headers, records


def _col_letter(index: int) -> str:
    output = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        output = chr(65 + remainder) + output
    return output


def write_xlsx(path: str | Path, headers: list[str], rows: Iterable[list[Any]]) -> None:
    """Write a simple, valid XLSX containing one auditable text table."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    all_rows = [headers, *list(rows)]
    sheet_rows: list[str] = []
    for row_index, row in enumerate(all_rows, start=1):
        cells: list[str] = []
        for column_index, value in enumerate(row, start=1):
            reference = f"{_col_letter(column_index)}{row_index}"
            text = "" if value is None else str(value)
            style = ' s="1"' if row_index == 1 else ""
            cells.append(
                f'<c r="{reference}" t="inlineStr"{style}><is><t xml:space="preserve">'
                f"{escape(text)}"
                "</t></is></c>"
            )
        sheet_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    max_col = _col_letter(max(1, len(headers)))
    max_row = max(1, len(all_rows))
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<worksheet xmlns="{MAIN_NS}"><dimension ref="A1:{max_col}{max_row}"/>'
        '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" '
        'activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
        f'<sheetData>{"".join(sheet_rows)}</sheetData><autoFilter ref="A1:{max_col}{max_row}"/>'
        "</worksheet>"
    )
    files = {
        "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        "</Types>",
        "_rels/.rels": '<?xml version="1.0" encoding="UTF-8"?>'
        f'<Relationships xmlns="{PKG_REL_NS}"><Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/></Relationships>',
        "xl/workbook.xml": '<?xml version="1.0" encoding="UTF-8"?>'
        f'<workbook xmlns="{MAIN_NS}" xmlns:r="{REL_NS}"><sheets>'
        '<sheet name="Failures" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": '<?xml version="1.0" encoding="UTF-8"?>'
        f'<Relationships xmlns="{PKG_REL_NS}">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        "</Relationships>",
        "xl/styles.xml": '<?xml version="1.0" encoding="UTF-8"?>'
        f'<styleSheet xmlns="{MAIN_NS}"><fonts count="2">'
        '<font><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Calibri"/></font>'
        '</fonts><fills count="3"><fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FF1F4E78"/><bgColor indexed="64"/></patternFill></fill></fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/></cellXfs>'
        "</styleSheet>",
        "xl/worksheets/sheet1.xml": sheet_xml,
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content.encode("utf-8"))




