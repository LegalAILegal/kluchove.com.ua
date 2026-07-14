"""Accounts and claim requests — persisted in the SAME Google Sheet.

Two extra tabs (auto-created on first use, so the owner needs to do nothing):

  accounts       phone | created_at
  requests       created_at | phone | entered_name | entered_address |
                 suggested_label | suggested_key | status | resolved_key

The account↔object BINDING is NOT stored here — it lives in **column N** of the
debtor row (see sheets.py). A verified phone that already sits in col N is an
automatic match; the `requests` tab is only for debtors with no phone on file.

The `requests` tab doubles as the admin panel: a claim appears as a row with a
fuzzy-matched suggestion filled in; the owner reviews it in the spreadsheet and
sets status to `approved` (optionally overriding `resolved_key`). On the next
`/me` the backend materializes the approved claim by writing the verified phone
into the matched row's column N, then flips the request row to `done`. A claim
that can't be resolved yet stays `approved` and is retried on later `/me` calls.

Volumes are tiny (a handful of registrations), so reads go straight to the API
without caching — keeping account state correct immediately after a write.
"""
import threading
from datetime import datetime, timezone

from .sheets import build_service
from .config import get_settings

ACCOUNTS = "accounts"
REQUESTS = "requests"

_HEADERS = {
    ACCOUNTS: ["phone", "created_at"],
    REQUESTS: [
        "created_at", "phone", "entered_name", "entered_address",
        "suggested_label", "suggested_key", "status", "resolved_key",
    ],
}

# requests column letters (1-based) for targeted updates
_REQ_STATUS_COL = "G"

# request statuses
ST_PENDING = "pending"
ST_APPROVED = "approved"
ST_REJECTED = "rejected"
ST_DONE = "done"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class AccountsRepo:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tabs_ready = False

    # --- low-level sheet access -----------------------------------------
    def _svc(self):
        return build_service().spreadsheets()

    def _ensure_tabs(self) -> None:
        if self._tabs_ready:
            return
        with self._lock:
            if self._tabs_ready:
                return
            sid = get_settings().sheet_id
            meta = self._svc().get(spreadsheetId=sid).execute()
            existing = {s["properties"]["title"] for s in meta.get("sheets", [])}
            to_add = [t for t in _HEADERS if t not in existing]
            if to_add:
                self._svc().batchUpdate(
                    spreadsheetId=sid,
                    body={"requests": [
                        {"addSheet": {"properties": {"title": t}}} for t in to_add
                    ]},
                ).execute()
                for t in to_add:
                    self._svc().values().update(
                        spreadsheetId=sid,
                        range=f"{t}!A1",
                        valueInputOption="RAW",
                        body={"values": [_HEADERS[t]]},
                    ).execute()
            self._tabs_ready = True

    def _read(self, tab: str) -> list[list[str]]:
        """Return data rows (header excluded) as lists of strings."""
        self._ensure_tabs()
        sid = get_settings().sheet_id
        res = self._svc().values().get(
            spreadsheetId=sid, range=f"{tab}!A2:Z", valueRenderOption="FORMATTED_VALUE"
        ).execute()
        return [[str(c).strip() for c in row] for row in res.get("values", [])]

    def _append(self, tab: str, row: list) -> None:
        self._ensure_tabs()
        sid = get_settings().sheet_id
        self._svc().values().append(
            spreadsheetId=sid,
            range=f"{tab}!A1",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()

    def _set_status(self, row_number: int, status: str) -> None:
        sid = get_settings().sheet_id
        self._svc().values().update(
            spreadsheetId=sid,
            range=f"{REQUESTS}!{_REQ_STATUS_COL}{row_number}",
            valueInputOption="RAW",
            body={"values": [[status]]},
        ).execute()

    # --- accounts --------------------------------------------------------
    def ensure_account(self, phone: str) -> None:
        rows = self._read(ACCOUNTS)
        if any(r and r[0] == phone for r in rows):
            return
        self._append(ACCOUNTS, [phone, _now()])

    # --- claim requests --------------------------------------------------
    def request_add(self, phone: str, entered_name: str, entered_address: str,
                    suggested_label: str, suggested_key: str) -> None:
        self._append(REQUESTS, [
            _now(), phone, entered_name, entered_address,
            suggested_label, suggested_key, ST_PENDING, "",
        ])

    def requests_for_phone(self, phone: str) -> list[dict]:
        """All (non-done) claim rows for a phone, for surfacing status to the user."""
        out = []
        for r in self._read(REQUESTS):
            if len(r) >= 2 and r[1] == phone:
                status = r[6] if len(r) >= 7 else ""
                out.append({
                    "entered_name": r[2] if len(r) >= 3 else "",
                    "entered_address": r[3] if len(r) >= 4 else "",
                    "status": status,
                })
        return out

    def materialize_approved(self, phone: str, bind) -> int:
        """Bind this phone's owner-approved claims; flip to `done` only on success.

        `bind(text)` takes an owner-approved value — either a normalized
        object_key OR free text like an address the owner typed into
        `resolved_key` — resolves it to a debtor row and writes `phone` into that
        row's column N; it returns True when a row was bound. Returns the number
        of rows newly bound.
        """
        created = 0
        rows = self._read(REQUESTS)
        for i, r in enumerate(rows):
            row_number = i + 2  # header is row 1
            phone_cell = r[1] if len(r) >= 2 else ""
            status = r[6] if len(r) >= 7 else ""
            if phone_cell != phone or status != ST_APPROVED:
                continue
            resolved = (r[7] if len(r) >= 8 else "").strip()
            text = resolved or (r[5] if len(r) >= 6 else "").strip()
            if text and bind(text):
                created += 1
                self._set_status(row_number, ST_DONE)
            # If the bind failed (key not resolvable yet — e.g. the debtor row has
            # no ПІБ, or the address was mistyped), DO NOT flip to `done`. Leaving
            # it `approved` means the next /me retries and it self-heals the moment
            # the owner fixes the row; the tenant meanwhile still sees "на розгляді"
            # instead of being silently dropped back to the empty claim form.
        return created


repo = AccountsRepo()
