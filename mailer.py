"""Shared SMTP delivery. Missing configuration never counts as a sent email."""
from email.message import EmailMessage
import hashlib
import os
import smtplib
import ssl


def configured():
    return bool(os.getenv("SMTP_HOST") and (os.getenv("SMTP_FROM") or os.getenv("SMTP_USERNAME")))



def diagnostic(exc):
    # Never include raw provider responses, addresses, credentials or message content.
    if not configured():
        return 'Почта не настроена. Запустите start-with-telegram.ps1 и укажите Gmail и пароль приложения.'
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return 'Gmail отклонил вход. Проверьте адрес отправителя и пароль приложения Google.'
    if isinstance(exc, ssl.SSLError):
        return 'Не удалось установить защищённое соединение с почтовым сервером.'
    if isinstance(exc, smtplib.SMTPResponseException):
        code = exc.smtp_code
        if isinstance(code, int) and 100 <= code <= 599:
            return f'Почтовый сервер отклонил письмо (SMTP {code}).'
        return 'Почтовый сервер отклонил письмо.'
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return 'Почтовый сервер отклонил адрес получателя.'
    if isinstance(exc, smtplib.SMTPException) and not isinstance(exc, smtplib.SMTPServerDisconnected):
        return 'Ошибка протокола SMTP (' + type(exc).__name__ + ').'
    if isinstance(exc, (TimeoutError, ConnectionError, OSError, smtplib.SMTPServerDisconnected)):
        code = getattr(exc, 'errno', None)
        phase = getattr(exc, 'smtp_phase', '')
        allowed = {'соединение', 'шифрование', 'вход', 'отправка', 'завершение'}
        suffix = ' (' + type(exc).__name__ + (', этап: ' + phase if phase in allowed else '') + (', код ' + str(code) if isinstance(code, int) else '') + ').'
        return 'Не удалось подключиться к почтовому серверу' + suffix
    return 'Не удалось отправить письмо. Проверьте настройки почты и попробуйте снова.'


def send_email(recipient, subject, body, key=None):
    if not configured():
        raise RuntimeError("Configure SMTP_HOST and SMTP_FROM before sending email")
    message = EmailMessage()
    message["From"] = os.getenv("SMTP_FROM") or os.getenv("SMTP_USERNAME")
    message["To"], message["Subject"] = recipient, subject
    if key:
        message["Message-ID"] = "<" + hashlib.sha256(key.encode()).hexdigest() + "@schedule-builder>"
    message.set_content(body)
    use_ssl = os.getenv("SMTP_USE_SSL", "false").lower() == "true"
    use_tls = os.getenv("SMTP_USE_TLS", "true").lower() == "true"
    if not use_ssl and not use_tls and os.getenv("APP_ENV") == "production":
        raise RuntimeError("SMTP encryption is required in production")
    transport = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
    kwargs = {"timeout":10}
    if use_ssl:
        kwargs["context"] = ssl.create_default_context()
    phase = 'соединение'
    try:
        with transport(os.getenv("SMTP_HOST"), int(os.getenv("SMTP_PORT", "587")), **kwargs) as client:
            if use_tls and not use_ssl:
                phase = 'шифрование'
                client.starttls(context=ssl.create_default_context())
            if os.getenv("SMTP_USERNAME"):
                phase = 'вход'
                username, password = os.getenv("SMTP_USERNAME"), os.getenv("SMTP_PASSWORD", "")
                if os.getenv("SMTP_HOST", "").strip().lower() == "smtp.gmail.com":
                    # Gmail can close the connection after multiple failed AUTH methods,
                    # masking the original 535 response. Try exactly one advertised method.
                    client.ehlo_or_helo_if_needed()
                    methods = client.esmtp_features.get('auth', '').upper().split()
                    client.user, client.password = username, password
                    if 'PLAIN' in methods:
                        client.auth('PLAIN', client.auth_plain, initial_response_ok=False)
                    elif 'LOGIN' in methods:
                        client.auth('LOGIN', client.auth_login, initial_response_ok=False)
                    else:
                        raise smtplib.SMTPNotSupportedError('No supported authentication method')
                else:
                    client.login(username, password, initial_response_ok=False)
            phase = 'отправка'
            refused = client.send_message(message)
            if refused:
                raise RuntimeError("SMTP server rejected the recipient")
            phase = 'завершение'
    except Exception as exc:
        exc.smtp_phase = phase
        raise
    return True
