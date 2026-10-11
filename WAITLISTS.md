# Waitlist auto-enrollment — academic demonstration

The **Waitlists** tab implements this user story using a local database and simulated
University Payment/Billing notifications. The default `WAITLIST_MODE=academic`
registers no university courses and performs no real payments. Email notifications
are sent through real SMTP to verified addresses; see [email setup](EMAIL_SETUP.md).
The existing schedule planner continues to use the curriculum's fixed times and quotas.
Waitlist inventory is separate from schedule planning.

## Demonstration

1. Sign in, open **Waitlists**, and choose a course, lecture, and linked practice.
2. Click **Join waitlist / enroll**. New demonstration sections begin fully occupied.
3. Click **Simulate one seat opening**. This releases one occupied seat in each selected
   section without changing its quota or meeting times. The first student in the queue
   receives both seats atomically. Real email and simulated tuition-adjustment events appear below.
4. To demonstrate FIFO, join the same sections from another student account, then drop
   the first account's enrollment. The next queued account enrolls automatically.
5. To demonstrate holds, click **Add account hold** before opening the seat. Enrollment
   remains blocked, with a warning and the original position retained. Click
   **Clear account hold** to resume enrollment.
6. To demonstrate conflicts, enroll in one course, then join sections of another course
   with overlapping meetings. The warning is recorded and the student stays queued.
   Drop the conflicting enrollment to allow processing to resume.

Leave a waitlist with **Leave waitlist**. Use **Drop enrollment** to release seats.
Rejoining places the student at the back of the queue. A repeated join or drop is
idempotent. A blocked earlier student prevents later entries sharing either requested
section from bypassing that student; unrelated sections continue processing.

## Processing and delivery

Seat updates, joins, cancellations, and drops process the queue within their database
transaction. A background worker also checks pending entries every second. The visible
Waitlists tab refreshes every two seconds. SQLite uses `BEGIN IMMEDIATE`; PostgreSQL uses
a transaction advisory lock to serialize changes across workers.

Warnings, enrollment notifications, and billing events are written to a persistent
transactional outbox before commit. Billing delivery is labeled **simulated** in academic
mode; email is **delivered** only after SMTP acceptance. Failed
delivery attempts stay **pending**, with exponential retries and recoverable leases.
An enrollment emits a billing reconciliation event; a drop emits its corresponding
reversal event. No tuition amounts are invented or payments performed.

## Optional integration adapter

`WAITLIST_MODE=live` disables simulation controls. This is still local enrollment: an
actual university registration adapter and an approved API contract would be needed
before using it operationally. This mode is not required for the academic demo.

- `UNIVERSITY_TERM`: term identifier; defaults to `local-preview`.
- `UNIVERSITY_WEBHOOK_TOKEN`: shared bearer token for inventory and account-hold updates.
- `UNIVERSITY_BILLING_URL`, `UNIVERSITY_BILLING_TOKEN`: billing event receiver and token.
- `SMTP_HOST`, `SMTP_PORT`, `SMTP_FROM`, `SMTP_USERNAME`, `SMTP_PASSWORD`,
  `SMTP_USE_TLS`, `SMTP_USE_SSL`: existing SMTP delivery configuration.
- `HOLD_VERIFICATION_TTL_SECONDS`: maximum age of verified hold status in live mode;
  defaults to 900 seconds. Missing or expired verification blocks enrollment.

The adapter contract below belongs to this app; it is not a claimed university API.
Map it to the university's actual contract if one becomes available. Use a separate
database/term for live-mode experiments; demonstration inventory is persisted.

Authenticated student routes:

- `GET /api/waitlists`: sections, available inventory, positions, enrollments, notifications.
- `POST /api/waitlists`: `{"lecture":"CSS 105.01","practice":"CSS 105.02"}`.
- `DELETE /api/waitlists/{id}`: cancel the current student's waiting entry.
- `DELETE /api/enrollments/{id}`: drop the current student's enrollment.
- `POST /api/waitlists/demo/seat`: selected lecture/practice pair (academic mode only).
- `POST /api/waitlists/demo/hold`: `{"on_hold":true}` (own account, academic mode only).

Integration routes require `Authorization: Bearer <UNIVERSITY_WEBHOOK_TOKEN>`:

```json
POST /api/integrations/university/inventory
{
  "event_id": "inventory-update-1",
  "section": "CSS 105.01",
  "revision": 1,
  "quota": 60,
  "occupied": 59,
  "reserved": 0
}
```

`occupied` covers externally occupied seats and must exclude this app's enrollments,
which are counted separately. Revisions increase per section. Replayed event IDs with
identical payloads have no effect; stale revisions or reused IDs with changed payloads
are rejected. Updates that would overbook a section are rejected.

```json
POST /api/integrations/university/holds
{
  "event_id": "account-update-1",
  "user_id": 1,
  "revision": 1,
  "on_hold": false,
  "reason": ""
}
```

The billing receiver accepts a JSON event containing `event` (`auto_enrolled` or
`enrollment_dropped`), `term`, `course`, `lecture`, `practice`, `enrollment_id`,
`student_email`, and `message`. Requests include a stable `Idempotency-Key`. The receiver
must deduplicate by this key. HTTPS is required outside localhost development.
Delivery is at least once; SMTP can duplicate messages if a delivery acknowledgement
is lost. Stable email Message-IDs help recipients identify retries.

## Validation

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
node tests/schedule-generation.cjs
```

The API test uses FastAPI's TestClient and needs `httpx` in the development environment.
Python tests cover FIFO promotion, atomic linked seats, holds, conflict warnings,
concurrent requests using separate worker locks, event replay/revisions, ownership,
background delivery, and retries. All test databases are isolated and temporary.
