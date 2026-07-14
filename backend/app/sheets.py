"""Read tenant rows from a PRIVATE Google Sheet via a service account.

Only the backend holds the credentials; the browser never touches the sheet.
"""
import threading
import time
from dataclasses import dataclass

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from .config import get_settings
from .matching import normalize_address, normalize_name, normalize_phone

# Read+write: the backend now also appends accounts/links/requests to the sheet
# (see accounts.py), so the read-only scope is no longer enough.
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Column indices within the A..N range (0-based).
COL_ADDRESS = 1   # B  Адреса
COL_NAME = 2      # C  ПІБ боржника
COL_DEBT = 3      # D  Борг по оренді
COL_PHONE = 13    # N  Телефон

_CACHE_TTL = 60   # seconds; the sheet changes rarely


def _range_start_row(rng: str) -> int:
    """First sheet row covered by an A1 range like 'A2:N' (defaults to 2)."""
    head = rng.split(":", 1)[0]
    digits = "".join(ch for ch in head if ch.isdigit())
    return int(digits) if digits else 2

# --- shared Google Sheets service (one authenticated client for the whole app) ---
_service_lock = threading.Lock()
_service = None


def build_service():
    """Return a cached, authenticated Sheets v4 service (read+write scope)."""
    global _service
    with _service_lock:
        if _service is None:
            s = get_settings()
            creds = Credentials.from_service_account_file(s.google_credentials_file, scopes=SCOPES)
            _service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        return _service


@dataclass
class Tenant:
    name: str
    address: str
    debt: str
    phone: str
    row: int = 0  # 1-based sheet row number (0 = unknown)

    @property
    def key(self) -> str:
        """Stable-ish identifier for this row / property object.

        There is no id column in the source sheet, so we key an object by its
        normalized name+address. This distinguishes several objects rented by
        the same person, and tolerates minor formatting edits. If a name or
        address is edited substantively the link breaks — add a real id column
        to the range for bulletproof keys.
        """
        return f"{normalize_name(self.name)}|{normalize_address(self.address).lower()}"

    @property
    def label(self) -> str:
        """Human-readable one-liner for the owner's eyes (in the sheet)."""
        return " — ".join(p for p in (self.name.strip(), self.address.strip()) if p)


class SheetRepo:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: list[Tenant] = []
        self._fetched_at: float = 0.0

    def _build_service(self):
        return build_service()

    def _fetch(self) -> list[Tenant]:
        s = get_settings()
        result = (
            self._build_service()
            .spreadsheets()
            .values()
            .get(spreadsheetId=s.sheet_id, range=s.sheet_range, valueRenderOption="FORMATTED_VALUE")
            .execute()
        )
        values = result.get("values", [])
        first_row = _range_start_row(s.sheet_range)
        tenants: list[Tenant] = []
        for offset, row in enumerate(values):
            def cell(i: int) -> str:
                return str(row[i]).strip() if i < len(row) and row[i] is not None else ""

            name = cell(COL_NAME)
            address = cell(COL_ADDRESS)
            # An object is keyed by name+address. Keep a row if it has EITHER of
            # them: a debtor may be entered by address alone (ПІБ not filled yet)
            # and must still be loadable so it can be bound by address. Only a row
            # with neither a name nor an address is noise — skip that.
            if not name and not address:
                continue
            tenants.append(
                Tenant(
                    name=name,
                    address=address,
                    debt=cell(COL_DEBT),
                    phone=cell(COL_PHONE),
                    row=first_row + offset,
                )
            )
        return tenants

    def all(self, *, monotonic=time.monotonic) -> list[Tenant]:
        with self._lock:
            if not self._rows or (monotonic() - self._fetched_at) > _CACHE_TTL:
                self._rows = self._fetch()
                self._fetched_at = monotonic()
            return self._rows

    def find_by_key(self, key: str) -> Tenant | None:
        if not key:
            return None
        for t in self.all():
            if t.key == key:
                return t
        return None

    def find_by_phone(self, phone: str) -> list[Tenant]:
        """All rows whose col-N phone normalizes to `phone` (380XXXXXXXXX).

        One phone may own several objects, so this returns a list.
        """
        if not phone:
            return []
        return [t for t in self.all() if normalize_phone(t.phone) == phone]

    def set_phone(self, key: str, phone: str) -> Tenant | None:
        """Write `phone` into column N of the row identified by `key` — this IS
        the account↔object binding (owner-approved manual claims land here).

        Enforces 1 object -> 1 account: refuses to overwrite a col-N phone that
        already belongs to someone else (returns None). No-op if already bound to
        `phone`. On a write, invalidates the cache so the next read sees it.
        Returns the bound Tenant, or None if the row is missing / already taken.
        """
        t = self.find_by_key(key)
        if t is None or not t.row:
            return None
        existing = normalize_phone(t.phone)
        if existing == phone:
            return t
        if existing:  # bound to a different phone — do not reassign
            return None
        s = get_settings()
        self._build_service().spreadsheets().values().update(
            spreadsheetId=s.sheet_id,
            range=f"N{t.row}",  # unqualified => the first (debtor) sheet
            valueInputOption="RAW",
            body={"values": [[phone]]},
        ).execute()
        with self._lock:
            self._fetched_at = 0.0  # force a refetch so find_by_phone sees the write
        return t


repo = SheetRepo()
