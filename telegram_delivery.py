"""Telegram Bot API transport; errors never contain request URLs or tokens."""
import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class TelegramError(RuntimeError):
    def __init__(self, code='network', permanent=False, retry_after=0):
        self.code = str(code) if re.fullmatch(r'[a-z_0-9]{1,30}', str(code)) else 'unknown'
        self.permanent = permanent
        self.retry_after = min(86400, max(0, int(retry_after)))
        super().__init__('Telegram ' + self.code)


def configured():
    return bool(os.getenv('TELEGRAM_BOT_TOKEN', '').strip())


def error_summary(exc):
    if isinstance(exc, TelegramError):
        return str(exc)
    if isinstance(exc, HTTPError):
        return 'HTTP ' + str(exc.code)
    return type(exc).__name__


def call(method, fields=None):
    token = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
    if not token:
        raise TelegramError('not_configured')
    if not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]+', token):
        raise TelegramError('invalid_token', permanent=True)
    request = Request('https://api.telegram.org/bot' + token + '/' + method,
        data=json.dumps(fields or {}).encode(), headers={'Content-Type':'application/json'}, method='POST')
    try:
        with urlopen(request, timeout=10) as response:
            body = json.loads(response.read())
    except HTTPError as exc:
        code, retry = exc.code, 0
        try:
            body = json.loads(exc.read(65536))
            retry = body.get('parameters', {}).get('retry_after', 0)
        except (ValueError, AttributeError, TypeError, OSError):
            pass
        raise TelegramError(code, code in (400, 401, 403, 404), retry) from None
    except (URLError, TimeoutError, OSError):
        raise TelegramError('network') from None
    except (ValueError, TypeError):
        raise TelegramError('invalid_response') from None
    if not isinstance(body, dict):
        raise TelegramError('invalid_response')
    if body.get('ok') is not True:
        code = body.get('error_code', 'unknown')
        raise TelegramError(code, code in (400, 401, 403, 404),
            body.get('parameters', {}).get('retry_after', 0))
    return body.get('result')


def deliver(chat_id, message):
    if not re.fullmatch(r'[1-9][0-9]{0,19}', str(chat_id)):
        raise TelegramError('not_linked', permanent=True)
    result = call('sendMessage', {'chat_id':str(chat_id), 'text':message})
    if not isinstance(result, dict) or not isinstance(result.get('message_id'), int):
        raise TelegramError('invalid_response')
    return {'status':'delivered', 'provider_message_id':str(result['message_id'])}
