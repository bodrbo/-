"""Small, dependency-free reader for the legacy investor XLSX report."""

import datetime as dt
import io
import posixpath
import re
import zipfile
from xml.etree import ElementTree


MAX_WORKBOOK_BYTES = 5 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 25 * 1024 * 1024
_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CELL_RE = re.compile(r"([A-Z]+)(\d+)$")


class InvestorWorkbookError(ValueError):
    """The uploaded workbook cannot be safely interpreted."""


def _xml(root_bytes):
    try:
        return ElementTree.fromstring(root_bytes)
    except ElementTree.ParseError as exc:
        raise InvestorWorkbookError("Файл XLSX повреждён или имеет неверную структуру.") from exc


def _read_shared_strings(archive):
    try:
        root = _xml(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    strings = []
    for item in root.findall(f"{{{_MAIN_NS}}}si"):
        strings.append("".join(node.text or "" for node in item.iter(f"{{{_MAIN_NS}}}t")))
    return strings


def _first_sheet_path(archive):
    workbook = _xml(archive.read("xl/workbook.xml"))
    sheet = workbook.find(f"{{{_MAIN_NS}}}sheets/{{{_MAIN_NS}}}sheet")
    if sheet is None:
        raise InvestorWorkbookError("В книге нет листов.")
    relationship_id = sheet.attrib.get(f"{{{_REL_NS}}}id")
    relationships = _xml(archive.read("xl/_rels/workbook.xml.rels"))
    for relationship in relationships.findall(f"{{{_PKG_REL_NS}}}Relationship"):
        if relationship.attrib.get("Id") != relationship_id:
            continue
        target = relationship.attrib.get("Target", "")
        if target.startswith("/"):
            path = target.lstrip("/")
        else:
            path = posixpath.normpath(posixpath.join("xl", target))
        if not path.startswith("xl/") or ".." in path.split("/"):
            raise InvestorWorkbookError("В книге обнаружена небезопасная ссылка на лист.")
        return path
    raise InvestorWorkbookError("Не удалось найти первый лист книги.")


def _cell_value(cell, shared_strings):
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.iter(f"{{{_MAIN_NS}}}t"))
    value_node = cell.find(f"{{{_MAIN_NS}}}v")
    if value_node is None or value_node.text is None:
        return None
    raw = value_node.text
    if cell_type == "s":
        try:
            return shared_strings[int(raw)]
        except (IndexError, ValueError) as exc:
            raise InvestorWorkbookError("В книге повреждена таблица текстовых значений.") from exc
    if cell_type in {"str", "e"}:
        return raw
    if cell_type == "b":
        return raw == "1"
    try:
        number = float(raw)
    except ValueError:
        return raw
    return int(number) if number.is_integer() else number


def _read_cells(payload):
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except (zipfile.BadZipFile, OSError) as exc:
        raise InvestorWorkbookError("Загрузите корректный файл формата XLSX.") from exc
    with archive:
        total_size = sum(info.file_size for info in archive.infolist())
        if total_size > MAX_UNCOMPRESSED_BYTES:
            raise InvestorWorkbookError("Распакованный файл XLSX слишком большой.")
        try:
            sheet_path = _first_sheet_path(archive)
            shared_strings = _read_shared_strings(archive)
            sheet = _xml(archive.read(sheet_path))
        except KeyError as exc:
            raise InvestorWorkbookError("В книге отсутствуют обязательные части XLSX.") from exc

    cells = {}
    for cell in sheet.iter(f"{{{_MAIN_NS}}}c"):
        reference = cell.attrib.get("r", "").upper()
        if _CELL_RE.fullmatch(reference):
            cells[reference] = _cell_value(cell, shared_strings)
    return cells


def _as_number(value, label, warnings=None):
    if isinstance(value, bool):
        raise InvestorWorkbookError(f"Поле «{label}» должно содержать число.")
    if isinstance(value, (int, float)):
        return float(value)
    raw = str(value or "").strip().replace("\u00a0", "").replace(" ", "").replace(",", ".")
    if re.fullmatch(r"--\d+(?:\.\d+)?", raw):
        if warnings is not None:
            warnings.append(f"В поле «{label}» двойной минус исправлен на одинарный: {raw}.")
        raw = f"-{raw[2:]}"
    try:
        return float(raw)
    except ValueError as exc:
        raise InvestorWorkbookError(f"Поле «{label}» должно содержать число, получено: {value!s}.") from exc


def _excel_date(value, label):
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, (int, float)):
        return dt.date(1899, 12, 30) + dt.timedelta(days=int(value))
    raw = str(value or "").strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(raw, fmt).date()
        except ValueError:
            pass
    raise InvestorWorkbookError(f"Поле «{label}» содержит некорректную дату.")


def parse_legacy_workbook(file_or_bytes):
    """Return normalized investment terms, ledger entries and payouts."""
    if hasattr(file_or_bytes, "read"):
        payload = file_or_bytes.read(MAX_WORKBOOK_BYTES + 1)
    else:
        payload = bytes(file_or_bytes)
    if not payload:
        raise InvestorWorkbookError("Файл пуст.")
    if len(payload) > MAX_WORKBOOK_BYTES:
        raise InvestorWorkbookError("Файл XLSX должен быть не больше 5 МБ.")

    cells = _read_cells(payload)
    title = str(cells.get("A1") or "").strip()
    if "БОДРЫЙ ПЕРВЫЙ" not in title.upper():
        raise InvestorWorkbookError("Это не отчёт по катеру «Бодрый Первый».")

    warnings = []
    investment_date = _excel_date(cells.get("D1"), "дата приёма инвестиций")
    financial_year_days = int(_as_number(cells.get("D2"), "дней в финансовом году"))
    season_months = int(_as_number(cells.get("D4"), "месяцев в сезоне"))
    investor_share = _as_number(cells.get("F2"), "доля инвестора")
    investment_amount = _as_number(cells.get("F3"), "сумма инвестиций")
    if investment_amount <= 0:
        raise InvestorWorkbookError("Сумма инвестиций должна быть больше нуля.")
    if not 0 < investor_share <= 1:
        raise InvestorWorkbookError("Доля инвестора должна быть больше 0% и не больше 100%.")
    if financial_year_days <= 0:
        raise InvestorWorkbookError("Число дней в финансовом году должно быть больше нуля.")

    payouts = []
    for row in range(7, 12):
        date_value = cells.get(f"C{row}")
        amount_value = cells.get(f"D{row}")
        if date_value in (None, "") and amount_value in (None, ""):
            continue
        payout_date = _excel_date(date_value, f"дата выплаты, строка {row}")
        amount = abs(_as_number(amount_value, f"сумма выплаты, строка {row}"))
        payouts.append({"date": payout_date.isoformat(), "amount": amount, "source_row": row})

    entries = []
    for row in range(15, 10000):
        date_value = cells.get(f"A{row}")
        income_value = cells.get(f"B{row}")
        income_description = str(cells.get(f"C{row}") or "").strip()
        expense_value = cells.get(f"D{row}")
        expense_description = str(cells.get(f"E{row}") or "").strip()
        if all(value in (None, "") for value in (
            date_value, income_value, income_description, expense_value, expense_description
        )):
            if row > 100 and not any(f"A{later}" in cells for later in range(row + 1, row + 25)):
                break
            continue
        entry_date = _excel_date(date_value, f"дата операции, строка {row}")
        if income_value not in (None, ""):
            income = _as_number(income_value, f"доход, строка {row}", warnings)
            if income:
                entries.append({
                    "date": entry_date.isoformat(),
                    "kind": "income",
                    "description": income_description or "Рейс",
                    "amount": abs(income),
                    "source_row": row,
                })
        if expense_value not in (None, ""):
            expense = _as_number(expense_value, f"расход, строка {row}", warnings)
            if expense:
                entries.append({
                    "date": entry_date.isoformat(),
                    "kind": "expense",
                    "description": expense_description or "Расход",
                    "amount": abs(expense),
                    "source_row": row,
                })

    if not entries:
        raise InvestorWorkbookError("В отчёте не найден финансовый журнал.")
    history_through_date = max(entry["date"] for entry in entries)
    income_total = sum(entry["amount"] for entry in entries if entry["kind"] == "income")
    expense_total = sum(entry["amount"] for entry in entries if entry["kind"] == "expense")

    return {
        "investment_date": investment_date.isoformat(),
        "investment_amount": investment_amount,
        "investor_share": investor_share,
        "financial_year_days": financial_year_days,
        "season_months": season_months,
        "manager_commission_note": str(cells.get("F1") or "").strip(),
        "history_through_date": history_through_date,
        "entries": entries,
        "payouts": payouts,
        "income_total": income_total,
        "expense_total": expense_total,
        "warnings": warnings,
    }
