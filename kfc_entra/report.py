"""Employee report parsing (CSV / TSV / XLSX) and Franchisee grouping.

The report is the standard "Employee Yammer Status" export. Headers vary in
case and punctuation between exports, so we normalise them before matching.
Rows with no Yammer ID (= no Entra object yet) are kept for the preview but
flagged so the apply step can skip them.
"""
from __future__ import annotations

import csv
import io
import re
import uuid
from dataclasses import dataclass, field

XLSX_MAGIC = b"PK\x03\x04"
UNKNOWN_CODES = {"", "UNK", "UNKNOWN"}

# normalised header -> EmployeeRow attribute
_HEADER_MAP = {
    "franchisee": "franchisee",
    "market": "market",
    "name": "name",
    "employee name": "name",
    "job role": "job_role",
    "role": "job_role",
    "email": "email",
    "e mail": "email",
    "email address": "email",
    "yammer id": "yammer_id",
    "yammerid": "yammer_id",
    "entra id": "yammer_id",
    "employee yammer status": "yammer_status",
    "yammer status": "yammer_status",
    "store": "store",
    "storeid": "store_id",
    "store id": "store_id",
}

_REQUIRED_FIELDS = ("franchisee", "email")


class ReportParseError(Exception):
    """Raised when an uploaded file cannot be understood as an employee report."""


@dataclass
class EmployeeRow:
    franchisee: str
    market: str
    name: str
    job_role: str
    email: str
    yammer_id: str
    yammer_status: str
    store: str = ""
    store_id: str = ""
    row_number: int = 0  # 1-based data row number, for error messages

    @property
    def is_unknown(self) -> bool:
        """Corporate / unattributed rows that should be skipped by default."""
        return self.franchisee.strip().upper() in UNKNOWN_CODES

    @property
    def in_entra(self) -> bool:
        """True when the Yammer ID looks like a real Entra object id."""
        return _is_uuid(self.yammer_id)

    def to_dict(self) -> dict:
        return {
            "franchisee": self.franchisee,
            "market": self.market,
            "name": self.name,
            "job_role": self.job_role,
            "email": self.email,
            "yammer_id": self.yammer_id,
            "yammer_status": self.yammer_status,
            "store": self.store,
            "store_id": self.store_id,
            "row_number": self.row_number,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EmployeeRow":
        return cls(
            franchisee=data.get("franchisee", ""),
            market=data.get("market", ""),
            name=data.get("name", ""),
            job_role=data.get("job_role", ""),
            email=data.get("email", ""),
            yammer_id=data.get("yammer_id", ""),
            yammer_status=data.get("yammer_status", ""),
            store=data.get("store", ""),
            store_id=data.get("store_id", ""),
            row_number=data.get("row_number", 0),
        )


@dataclass
class FranchiseeGroup:
    code: str
    market: str
    rows: list[EmployeeRow] = field(default_factory=list)

    @property
    def is_unknown(self) -> bool:
        return self.code.strip().upper() in UNKNOWN_CODES

    @property
    def ready_count(self) -> int:
        return sum(1 for r in self.rows if r.in_entra)

    @property
    def missing_entra_count(self) -> int:
        return len(self.rows) - self.ready_count

    @property
    def sample_names(self) -> list[str]:
        return [r.name for r in self.rows[:4] if r.name]


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value.strip())
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _normalise_header(header: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (header or "").strip().lower()).strip()


def _map_headers(headers: list[str]) -> dict[int, str]:
    """Map column index -> EmployeeRow attribute for recognised columns."""
    mapping: dict[int, str] = {}
    for idx, header in enumerate(headers):
        attr = _HEADER_MAP.get(_normalise_header(header))
        if attr and attr not in mapping.values():
            mapping[idx] = attr
    return mapping


def _rows_from_table(table: list[list[str]]) -> list[EmployeeRow]:
    if not table:
        raise ReportParseError("The file is empty.")
    headers = [str(c or "") for c in table[0]]
    col_map = _map_headers(headers)
    found = set(col_map.values())
    missing = [f for f in _REQUIRED_FIELDS if f not in found]
    if missing:
        raise ReportParseError(
            "Could not find required column(s): "
            + ", ".join(m.upper() for m in missing)
            + ". Found headers: "
            + ", ".join(h for h in headers if h.strip())[:300]
        )

    rows: list[EmployeeRow] = []
    for i, raw in enumerate(table[1:], start=1):
        values = {attr: str(raw[idx] or "").strip() if idx < len(raw) else ""
                  for idx, attr in col_map.items()}
        if not any(values.values()):
            continue  # fully blank line
        # Ragged rows (extra/missing cells) shift columns. Salvage the two
        # load-bearing fields by content: EMAIL must contain '@', Yammer ID
        # must be a UUID. Only override when the mapped value is wrong.
        cells = [str(c or "").strip() for c in raw]
        if values.get("email") and "@" not in values["email"]:
            values["email"] = next((c for c in cells if "@" in c), values["email"])
        if values.get("yammer_id") and not _is_uuid(values["yammer_id"]):
            values["yammer_id"] = next((c for c in cells if _is_uuid(c)), "")
        row = EmployeeRow(
            franchisee=values.get("franchisee", "").upper(),
            market=values.get("market", ""),
            name=values.get("name", ""),
            job_role=values.get("job_role", ""),
            email=values.get("email", "").lower(),
            yammer_id=values.get("yammer_id", ""),
            yammer_status=values.get("yammer_status", ""),
            store=values.get("store", ""),
            store_id=values.get("store_id", ""),
            row_number=i,
        )
        if not row.email and not row.yammer_id and not row.name:
            continue  # nothing actionable on this line
        rows.append(row)
    if not rows:
        raise ReportParseError("The file has headers but no data rows.")
    return rows


def _parse_xlsx(data: bytes) -> list[list[str]]:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover
        raise ReportParseError("openpyxl is not installed - cannot read .xlsx files.") from exc
    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise ReportParseError(f"Could not open the Excel file: {exc}") from exc
    sheet = workbook.active
    table = [
        ["" if cell is None else str(cell) for cell in row]
        for row in sheet.iter_rows(values_only=True)
    ]
    workbook.close()
    return table


def _parse_delimited(data: bytes) -> list[list[str]]:
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:  # pragma: no cover
        raise ReportParseError("Could not decode the file as text.")

    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
        delimiter = dialect.delimiter
    except csv.Error:
        # Fallback: tab when the first line contains tabs, else comma.
        first_line = sample.splitlines()[0] if sample.splitlines() else ""
        delimiter = "\t" if "\t" in first_line else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    return [list(row) for row in reader]


def parse_report(data: bytes, filename: str) -> list[EmployeeRow]:
    """Parse CSV / TSV / XLSX bytes into normalised EmployeeRows.

    Detection: file extension first, then content sniff (XLSX files start
    with the ZIP magic ``PK\\x03\\x04``).
    """
    if not data:
        raise ReportParseError("The uploaded file is empty.")
    name = (filename or "").lower()
    looks_xlsx = name.endswith((".xlsx", ".xlsm")) or data[:4] == XLSX_MAGIC
    if name.endswith((".csv", ".tsv", ".txt")) and data[:4] != XLSX_MAGIC:
        looks_xlsx = False
    table = _parse_xlsx(data) if looks_xlsx else _parse_delimited(data)
    return _rows_from_table(table)


def group_by_franchisee(rows: list[EmployeeRow]) -> dict[str, FranchiseeGroup]:
    """Group rows by Franchisee code, unknown/corporate codes last."""
    groups: dict[str, FranchiseeGroup] = {}
    for row in rows:
        code = row.franchisee or "UNK"
        group = groups.get(code)
        if group is None:
            group = FranchiseeGroup(code=code, market=row.market)
            groups[code] = group
        if not group.market and row.market:
            group.market = row.market
        group.rows.append(row)
    ordered = sorted(groups.values(), key=lambda g: (g.is_unknown, g.code))
    return {g.code: g for g in ordered}
