# User stories 5–6

## Implemented behavior

**US5 — Advisor Approval Routing.** A student submits a reason for a restricted
course or a credit overload. The advisor dashboard refreshes every two seconds
while visible. Advisors see and decide requests only in their assigned department.
The student sees the decision and advisor comment. Approval immediately retries
waiting enrollment; rejection keeps the course blocked. Backend enforcement runs
on every enrollment attempt and worker promotion. Approval is scoped to the term,
course, selected lecture/practice, department, flags and approved credit total.
Seat capacity, account holds and schedule conflicts continue to apply.

**US6 — Course Seat Alert Notifications.** Students subscribe to a full lecture
or practice section with any combination of Push, Telegram and Email. Telegram requires a linked private bot chat. A transition from zero to positive free seats queues
one event per subscribed channel in the same transaction as the seat update.
Inventory webhooks, simulation and dropped enrollments all trigger this check.
Unchanged inventory, a still-full section and replayed webhook events do not send
new alerts. Each subsequent full-to-open transition may send a new alert.
Unsubscribe stops future events and cancels pending deliveries.

Events are recorded before automatic waitlist promotion: opening a seat does not
reserve it for an alert subscriber or bypass FIFO. Availability may change before
the recipient acts. The existing worker wakes immediately, retries transient
failures and uses stable idempotency keys. No external message is sent by tests.

## First local demonstration

The **Request an extra seat** form now has its own searchable course selector
containing every subject in the official catalog, including courses without
published sections. Choose a subject and explain why you need an additional seat.
No selection on Waitlists is needed. POST /api/registration/seat-requests accepts
course and reason, stores an additional_seat request, and displays it to the
student's department advisor with the course name and reason. Approval records the
advisor's decision. The advisor chooses a lecture and linked practice and approves
with **Approve & grant 1 seat**. Both local section quotas increase by one, and
that place is reserved for the requesting student in a transactional grant record.
The student uses **Enroll using reserved seat** or joins the matching waitlist.
The personal grant cannot be consumed by another student; ordinary FIFO still
applies to other places. Holds and conflicts retain the reservation until resolved.
Repeated approvals cannot create more grants. Rejection changes no quotas.
Course sections must be published before a physical seat can be allocated.
Existing section-specific override API and its authorization checks remain
available. Seat alert subscriptions still use the sections selected on Waitlists.

Run these commands **from the project directory**, using the same Python and
DATABASE_URL as the application. Register and verify two accounts using the
existing UI first. Substitute their real account addresses:

```powershell
python manage_registration.py profile student@example.com --role student --department CS
python manage_registration.py profile advisor@example.com --role advisor --department CS
python manage_registration.py course "CSS 105" --department CS --restricted yes
```

Advisor roles and changes to existing department assignments are administrator
operations through a local CLI. In academic simulation mode, accounts without a
profile automatically receive the student role and REGISTRATION_DEFAULT_DEPARTMENT
(CS by default) when opening Advisor & Alerts or submitting a request. Existing
assignments remain unchanged. Outside academic mode, department assignment still
requires an administrator. HTTP clients cannot grant themselves advisor rights.
The default course catalog has no imposed restrictions until configured. Existing
users and saved schedules are preserved; new tables are created automatically.
Students may submit any selected, not-yet-enrolled course for an additional seat
even when it has no configured restriction. Additional-seat requests preserve
the student's reason and are routed to the department advisor.
Form errors and submission status appear next to the submit button. A finalized
or pending request disables duplicate submission for the same course.

1. Restart the existing server, or let its development reloader reload the change.
2. As the student, choose a course on **Requests & Alerts** and submit a reason
   for an extra seat.
3. Sign in as the advisor in another browser session. Advisor workspace opens
   automatically; student tabs are hidden. Choose a lecture/practice pair and
   select **Approve & grant 1 seat**, or **Reject**.
4. As the student, use **Enroll using reserved seat**. If already queued for that
   pair, approval retries enrollment immediately. No simulation of a dropped seat
   is required for the personal grant.
5. The account badge shows Student or Advisor and its department. Role checks
   are enforced on the server, including student-only enrollment mutations.
6. Test rejection with another student/request. A second decision on a finalized
   request is refused. Requests are final for that course and term.

Credit overload uses active enrollments for the current term plus the requested
course and the official catalog ECTS. Default limit: 30; configure
REGISTRATION_CREDIT_LIMIT to change it. This story covers restricted-course and
overload approvals; existing prerequisite guidance is unchanged.

## University Notification Gateway

Set process environment variables before launching the application:

```powershell
$env:UNIVERSITY_NOTIFICATION_URL = 'https://your-university-gateway/notifications'
$env:UNIVERSITY_NOTIFICATION_TOKEN = 'your-gateway-token'
python main.py
```

The sample .env file documents settings; the application does not automatically
load it. The gateway receives an HTTP POST with Content-Type application/json,
Authorization Bearer token when configured, and an Idempotency-Key. JSON fields:

```json
{
  "event": "seat_available",
  "term": "local-preview",
  "section": "CSS 105.01",
  "subscription_id": 1,
  "channel": "push",
  "student_email": "student@example.com",
  "message": "A seat opened ..."
}
```

Telegram alerts use the Bot API directly, with a private chat linked to each account.
Email uses configured SMTP, with the gateway as fallback. Push uses the university
gateway, which must map student_email to registered device endpoints and deduplicate
the supplied key. See NOTIFICATIONS_SETUP.md for Telegram setup.

Without a configured gateway, Push alerts remain visibly pending. Retry delays grow
up to five minutes during outages, including Telegram API outages. Under a healthy
gateway the worker wakes immediately (otherwise checks each second); queuing is
verified within 30 seconds. End-to-end delivery within 30 seconds depends on the
gateway, load and provider, and is not guaranteed offline. A delivery already
claimed by the worker may finish after unsubscribe.

## Verification

```powershell
python -m unittest discover -s tests -p "test_*.py"
node tests/schedule-generation.cjs
node --check public/registration.js
```

The new tests exercise department isolation and student access denial, immediate
approval enrollment, rejection enforcement, credit overload and account holds,
all notification channels, transition and webhook deduplication, unsubscribe,
gateway payload and retry after a provider failure.
