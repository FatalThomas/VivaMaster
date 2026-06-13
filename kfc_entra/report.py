"""Employee report parsing (CSV / TSV / XLSX) and Franchisee grouping.

Supports two report shapes:

  * Legacy "Employee Yammer Status" export with columns like FRANCHISEE,
    EMAIL, YAMMER ID, STORE, etc.
  * Current Yum! payroll export with USERID, FIRSTNAME, LASTNAME, EMAIL,
    STATUS, PRIMARY_BRAND, COUNTRY, JOBROLE, FRANCHISEID, STOREID, ...

Headers vary in case and punctuation between exports, so we normalise
them before matching. When the new-format filter columns are present
(STATUS, PRIMARY_BRAND, COUNTRY, JOBROLE), rows that don't match the
KFC-Australia-active-allowlisted-role criteria get dropped at parse
time and counted in the returned ``RowFilterStats`` so the preview can
report how much was filtered out.
"""
from __future__ import annotations

import csv
import io
import re
import uuid
from dataclasses import dataclass, field

XLSX_MAGIC = b"PK\x03\x04"
# OLE-2 Compound Document magic - shared by the binary BIFF .xls format,
# .doc, .ppt, etc. If we see this in a file labelled .xls (or any file
# with no extension) we try xlrd before giving up.
XLS_MAGIC = b"\xd0\xcf\x11\xe0"
UNKNOWN_CODES = {"", "UNK", "UNKNOWN"}

# normalised header -> EmployeeRow attribute
_HEADER_MAP = {
    "franchisee": "franchisee",
    "franchiseid": "franchisee",
    "franchise id": "franchisee",
    "market": "market",
    "bmu": "market",
    "name": "name",
    "employee name": "name",
    "firstname": "first_name",
    "first name": "first_name",
    "lastname": "last_name",
    "last name": "last_name",
    "job role": "job_role",
    "role": "job_role",
    "jobrole": "job_role",
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
    "status": "status",
    "primary brand": "primary_brand",
    "primarybrand": "primary_brand",
    "country": "country",
}

# Only the franchisee bucket and an email are non-negotiable. STATUS /
# PRIMARY_BRAND / COUNTRY / JOBROLE are only used when present.
_REQUIRED_FIELDS = ("franchisee", "email")

# Job-role allowlist (new-format reports). Compared case-insensitively
# with whitespace normalised, so "Owner-RGM" / "owner-rgm" / "Owner-RGM "
# all match. Rows whose JOBROLE is anything outside this set are dropped
# before they ever reach the preview.
JOB_ROLE_ALLOWLIST = frozenset(
    {
        "owner",
        "franchise business coach",
        "restaurant excellence leader",
        "training coordinator",
        "foh tm",
        "key operator",
        "operations (ar)",
        "region coach",
        "shift supervisor",
        "boh tm",
        "shift supervisor trainee",
        "assistant manager",
        "assistant manager trainee",
        "area coach trainee",
        "moh tm",
        "rgm trainee",
        "rgm",
        "supported worker",
        "delivery tm",
        "team trainer",
        "market coach",
        "owner-rgm",
        "rsc",
    }
)


def _norm_role(role: str) -> str:
    return " ".join((role or "").split()).lower()


def is_allowed_job_role(role: str) -> bool:
    """True when the role is on the active allowlist of accepted job roles."""
    return _norm_role(role) in JOB_ROLE_ALLOWLIST


def _lookup_store_name(store_id: str) -> str:
    """Resolve a numeric STOREID to its human-readable store name.

    Wrapped so the import-time cost of loading store_directory's 862-entry
    dict is only paid by reports that actually parse - tests / tools that
    just import EmployeeRow don't.
    """
    from .store_directory import lookup_store_name
    return lookup_store_name(store_id)


class ReportParseError(Exception):
    """Raised when an uploaded file cannot be understood as an employee report."""


@dataclass
class RowFilterStats:
    """How many rows came in, what survived, what got dropped and why."""
    total_raw: int = 0
    kept: int = 0
    dropped_status: int = 0
    dropped_brand: int = 0
    dropped_country: int = 0
    dropped_job_role: int = 0
    dropped_blank: int = 0
    # Which filter columns were actually present in the upload's headers,
    # so the preview can say "STATUS filter applied" vs "no STATUS column".
    has_status_column: bool = False
    has_brand_column: bool = False
    has_country_column: bool = False
    has_job_role_column: bool = False

    @property
    def total_dropped(self) -> int:
        return (
            self.dropped_status + self.dropped_brand + self.dropped_country
            + self.dropped_job_role + self.dropped_blank
        )


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


# Job-role labels that promote a user to community admin (group owner) when
# adding by Store. Compared case-insensitively, with whitespace normalised.
COMMUNITY_ADMIN_ROLES = frozenset(
    {
        "rgm",
        "rgm trainee",
        "assistant manager",
        "assistant manager trainee",
    }
)


def is_community_admin_role(job_role: str) -> bool:
    """True when this job role should make the user a group owner."""
    if not job_role:
        return False
    return " ".join(job_role.split()).lower() in COMMUNITY_ADMIN_ROLES


@dataclass
class StoreGroup:
    name: str
    franchisee: str
    rows: list[EmployeeRow] = field(default_factory=list)

    @property
    def is_unknown(self) -> bool:
        # Stores like "UNKNOWN" or blank correspond to corporate rows that
        # shouldn't be auto-mapped to a Store group.
        return self.name.strip().upper() in UNKNOWN_CODES

    @property
    def ready_count(self) -> int:
        return sum(1 for r in self.rows if r.in_entra)

    @property
    def missing_entra_count(self) -> int:
        return len(self.rows) - self.ready_count

    @property
    def admin_count(self) -> int:
        return sum(1 for r in self.rows if is_community_admin_role(r.job_role))

    @property
    def sample_names(self) -> list[str]:
        return [r.name for r in self.rows[:4] if r.name]

    @property
    def expected_group_name(self) -> str:
        """The Entra group name we'd expect for this store - 'KFC <store>'."""
        return f"KFC {self.name}".strip()


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


def _rows_from_table(table: list[list[str]]) -> tuple[list[EmployeeRow], RowFilterStats]:
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

    stats = RowFilterStats(
        has_status_column="status" in found,
        has_brand_column="primary_brand" in found,
        has_country_column="country" in found,
        has_job_role_column="job_role" in found,
    )

    rows: list[EmployeeRow] = []
    for i, raw in enumerate(table[1:], start=1):
        values = {attr: str(raw[idx] or "").strip() if idx < len(raw) else ""
                  for idx, attr in col_map.items()}
        if not any(values.values()):
            continue  # fully blank line
        stats.total_raw += 1
        # Ragged rows (extra/missing cells) shift columns. Salvage the two
        # load-bearing fields by content: EMAIL must contain '@', Yammer ID
        # must be a UUID. Only override when the mapped value is wrong.
        cells = [str(c or "").strip() for c in raw]
        if values.get("email") and "@" not in values["email"]:
            values["email"] = next((c for c in cells if "@" in c), values["email"])
        if values.get("yammer_id") and not _is_uuid(values["yammer_id"]):
            values["yammer_id"] = next((c for c in cells if _is_uuid(c)), "")

        # ---- new-format filters (only apply when the column is present) ----
        # STATUS must be exactly "Active" (case-insensitive).
        if stats.has_status_column:
            status_val = values.get("status", "").strip().lower()
            if status_val and status_val != "active":
                stats.dropped_status += 1
                continue
        if stats.has_brand_column:
            brand_val = values.get("primary_brand", "").strip().lower()
            if brand_val and brand_val != "kfc":
                stats.dropped_brand += 1
                continue
        if stats.has_country_column:
            country_val = values.get("country", "").strip().lower()
            if country_val and country_val != "australia":
                stats.dropped_country += 1
                continue
        if stats.has_job_role_column:
            role_val = values.get("job_role", "")
            if role_val and not is_allowed_job_role(role_val):
                stats.dropped_job_role += 1
                continue

        # ---- compose the display name ----
        # New format gives FIRSTNAME + LASTNAME separately. Build "First
        # Last" for Entra. Falls back to the legacy single "name" column
        # if FIRSTNAME wasn't present.
        first = values.get("first_name", "").strip()
        last = values.get("last_name", "").strip()
        combined = f"{first} {last}".strip() if (first or last) else ""
        display_name = combined or values.get("name", "")

        # ---- resolve STOREID -> store name ----
        # The new payroll export only has STOREID (numeric), not STORE.
        # Apply-by-Store still wants "KFC <name>" group names, so look the
        # id up in the baked-in directory. Falls back to whatever the row
        # already had (legacy reports), and ultimately to "" so the
        # Apply-by-Store flow can flag it as UNKNOWN.
        store_name = values.get("store", "").strip()
        store_id = values.get("store_id", "").strip()
        if not store_name and store_id:
            store_name = _lookup_store_name(store_id)

        row = EmployeeRow(
            franchisee=values.get("franchisee", "").upper(),
            market=values.get("market", ""),
            name=display_name,
            job_role=values.get("job_role", ""),
            email=values.get("email", "").lower(),
            yammer_id=values.get("yammer_id", ""),
            yammer_status=values.get("yammer_status", ""),
            store=store_name,
            store_id=store_id,
            row_number=i,
        )
        if not row.email and not row.yammer_id and not row.name:
            stats.dropped_blank += 1
            continue  # nothing actionable on this line
        rows.append(row)
    stats.kept = len(rows)
    if not rows:
        # Distinguish "no data" from "everything was filtered out" for the
        # error message - the latter is more useful when the user expects
        # to see rows after a 60k-row upload.
        if stats.total_raw == 0:
            raise ReportParseError("The file has headers but no data rows.")
        raise ReportParseError(
            f"All {stats.total_raw} data rows were filtered out "
            f"({stats.dropped_status} by STATUS, {stats.dropped_brand} by PRIMARY_BRAND, "
            f"{stats.dropped_country} by COUNTRY, {stats.dropped_job_role} by JOBROLE). "
            "Check the report's column values match Active / KFC / Australia."
        )
    return rows, stats


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


def _parse_xls(data: bytes) -> list[list[str]]:
    """Read a legacy .xls (BIFF / OLE-2 Compound Document) workbook via xlrd.

    xlrd 2.0+ dropped .xls support, so we pin to 1.2.0 in requirements.
    """
    try:
        import xlrd
    except ImportError as exc:  # pragma: no cover
        raise ReportParseError(
            "xlrd is not installed - cannot read .xls files. "
            "Re-save the file as .xlsx in Excel and re-upload."
        ) from exc
    try:
        book = xlrd.open_workbook(file_contents=data, formatting_info=False)
    except Exception as exc:
        raise ReportParseError(f"Could not open the .xls file: {exc}") from exc
    sheet = book.sheet_by_index(0)
    table: list[list[str]] = []
    for r in range(sheet.nrows):
        row = []
        for c in range(sheet.ncols):
            cell = sheet.cell(r, c)
            value = cell.value
            # xlrd returns dates as floats - keep them as-is; the parser
            # downstream only cares about text columns. Numbers become
            # their string form so "1300" stays "1300" not "1300.0".
            if isinstance(value, float) and value.is_integer():
                value = str(int(value))
            elif value is None:
                value = ""
            else:
                value = str(value)
            row.append(value)
        table.append(row)
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


def parse_report(data: bytes, filename: str) -> tuple[list[EmployeeRow], RowFilterStats]:
    """Parse CSV / TSV / XLS / XLSX bytes into normalised EmployeeRows + filter stats.

    Detection: file extension first, then content sniff:
      - ``PK\\x03\\x04`` -> XLSX / XLSM (ZIP-based OOXML)
      - ``\\xd0\\xcf\\x11\\xe0`` -> legacy .xls (OLE-2 compound document)
      - everything else -> delimited text
    """
    if not data:
        raise ReportParseError("The uploaded file is empty.")
    name = (filename or "").lower()
    head = data[:4]
    looks_xlsx = name.endswith((".xlsx", ".xlsm")) or head == XLSX_MAGIC
    looks_xls = name.endswith(".xls") or head == XLS_MAGIC
    if name.endswith((".csv", ".tsv", ".txt")) and head not in (XLSX_MAGIC, XLS_MAGIC):
        looks_xlsx = looks_xls = False
    if looks_xlsx:
        table = _parse_xlsx(data)
    elif looks_xls:
        table = _parse_xls(data)
    else:
        table = _parse_delimited(data)
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


def group_by_store(rows: list[EmployeeRow]) -> dict[str, StoreGroup]:
    """Group rows by Store name, blank/UNKNOWN stores last.

    The Store name is preserved verbatim - case and spacing - because the
    expected Entra group name is just "KFC " + the Store. Two rows with
    the same Store value end up in the same bucket; rows with no Store
    value are bucketed under "" so the caller can choose to skip them.
    """
    groups: dict[str, StoreGroup] = {}
    for row in rows:
        store = (row.store or "").strip()
        key = store or "UNKNOWN"
        group = groups.get(key)
        if group is None:
            group = StoreGroup(name=store, franchisee=row.franchisee or "")
            groups[key] = group
        if not group.franchisee and row.franchisee:
            group.franchisee = row.franchisee
        group.rows.append(row)
    ordered = sorted(groups.values(), key=lambda g: (g.is_unknown, g.name.lower()))
    return {(g.name or "UNKNOWN"): g for g in ordered}
