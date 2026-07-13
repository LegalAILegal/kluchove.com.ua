# backend/secrets/

Drop the Google service-account key here as `google_sa.json` before `docker compose up`.
This directory is gitignored — never commit real keys.

Steps:
1. Google Cloud Console → IAM & Admin → Service Accounts → create account.
2. Enable the **Google Sheets API** for the project.
3. Create a JSON key → save it here as `google_sa.json`.
4. Share the spreadsheet as **Editor** with the service account's email
   (client_email in the JSON), then make the sheet private.
   Editor (not just read-only) is required: the backend auto-creates the
   `accounts` and `requests` tabs, and — on owner approval of a manual claim —
   writes the verified phone into **column N** of the matched debtor row (the
   account↔object binding). It never touches the name/address/debt columns.
