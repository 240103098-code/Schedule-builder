# User stories 9 and 10

Implemented from the User Story 9 / US9QATest and User Story 10 / US10QATest tabs of the supplied workbook.

## Course section swap

Open **Waitlists**, select an enrolled course and a new lecture with its linked practice, then select **Swap enrolled section**. The result appears immediately in the status line and enrollment list.

`POST /api/enrollments/{id}/swap` accepts `{"lecture":"...","practice":"..."}`. Ownership, course identity, linked practice, account holds, advisor policy, meeting conflicts and target capacity are validated inside the existing serialized database transaction. The existing enrollment and queue entry are updated together. A full target or any later failure rolls back the transaction and preserves the original seat. Released seats are processed by the existing waitlist system. An unchanged lecture seat can be reused when only the practice changes.

## Calendar export

On **Waitlists**, enter the semester start and end dates, then select **Export Schedule (.ics)**. Import the downloaded file into the student's personal calendar using that calendar's import function. This is a file export; it does not connect an account, directly write to a calendar provider, or continuously synchronize it.

`POST /api/enrollments/calendar` accepts `{"start":"2026-11-16","end":"2027-02-28"}` and returns an authenticated, non-cacheable calendar download. Only current active enrollments are exported. An empty registration or any waiting/blocked course returns HTTP 409 with `Schedule must be fully confirmed before export`. Planner drafts are not a source for calendar events.

The catalog does not publish semester dates, so the student supplies them. The start date defines academic week 1 for odd/even week patterns; use the official first day of the semester. The maximum range is 367 inclusive days. Events include course code/name, section, date, start/end times and meeting `location` or `room`. Missing locations are explicitly shown as `Not specified`, because the current catalog contains no room assignments. Times follow the catalog's Asia/Qyzylorda (UTC+05:00) convention and are encoded as UTC. Stable event UIDs, escaping and UTF-8 line folding follow the iCalendar format.

The success message confirms that the file was exported. Import completion is controlled by the personal calendar application. A calendar provider outage does not prevent downloading the file.

## Verification

Install project requirements plus `httpx` for the existing FastAPI test clients, then run:

```console
python -m unittest discover -s tests -v
```

`tests/test_schedule_actions.py` covers successful swap and promotion, full targets, rollback after an injected write failure, ownership, course validation, account holds, meeting conflicts, reuse of the existing lecture seat, concurrent competition for the last seat, calendar content, week parity, unconfirmed schedule rejection, download headers and date validation.

Changed application files: `schedule_actions.py`, `waitlist_service.py`, `public/schedule-builder.html`. No schema migration or additional production dependency is required. Start the application using the existing project setup instructions.
