# Real email delivery

Registration, email confirmation, password reset, and waitlist notifications share
the SMTP sender in `mailer.py`. Email is no longer simulated, including in academic
waitlist mode. Billing and university enrollment remain academic simulations.

## Start with Gmail

1. Enable Google 2-Step Verification and create an app password. See
   [Google's app-password instructions](https://support.google.com/accounts/answer/185833).
   Google may not offer app passwords for some account/security configurations;
   another SMTP provider can use the same environment settings if necessary.
2. Run `./start-with-email.ps1` from PowerShell. It privately prompts for your Gmail
   address and app password, configures `smtp.gmail.com:587` with STARTTLS, and starts
   the app. The password is not written to a file or printed.
3. Register with an email address you can access. Open the confirmation message,
   follow its link, click **Подтвердить адрес**, then sign in.

Gmail settings are documented in [Google's SMTP setup help](https://support.google.com/mail/answer/7104828).
If the project uses a virtual environment, activate it before running the script.
The application's existing Python dependencies must be installed.

Alternatively, set these variables in your process/service environment:

```text
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=your-address@gmail.com
SMTP_PASSWORD=your-google-app-password
SMTP_FROM=your-address@gmail.com
SMTP_USE_TLS=true
SMTP_USE_SSL=false
APP_BASE_URL=http://127.0.0.1:8000
```

Use your site's reachable address for `APP_BASE_URL` when opening links from another
device or deploying the app. `.env.example` contains example settings; `.env` is not
automatically loaded. Never commit a real password.

## Confirmation and recovery

- New accounts cannot sign in until email ownership is confirmed. Existing accounts
  also need confirmation after this update: choose **Подтвердить почту / повторить
  отправку** on the login screen to request a link.
- Verification links expire after 24 hours by default (`AUTH_VERIFY_TTL_SECONDS`).
  Requesting a replacement invalidates earlier links. Resends have a one-minute cooldown.
- **Восстановить пароль по почте** sends an email-only reset link. Opening the link and
  submitting a new password proves mailbox access. Links expire after 30 minutes
  (`AUTH_RESET_TTL_SECONDS`) and can be used once. A successful reset ends all sessions
  and also confirms the email address.
- Tokens are hashed in the database and are never returned in API responses or logs.
  Missing SMTP or a rejected recipient produces an error, not a development bypass.

## Waitlists

Selected sections, queued registrations, and enrollments show their fixed meeting
times and available seats / quota. Waitlist emails are delivered to verified addresses.
Failed attempts stay **pending** and retry; **delivered** means the SMTP server accepted
the message, not a guarantee that it reached the inbox. Check spam folders and sender
provider limits when diagnosing delivery. Academic billing events remain **simulated**.

Validation covers mailbox confirmation, expired/reused links, session revocation,
delivery failures, and actual SMTP protocol delivery to an isolated local test server.
Gmail inbox delivery still requires your sender credentials; no Gmail credentials are
included in this repository.
