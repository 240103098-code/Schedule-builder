import io
import json
import os
import unittest
from unittest.mock import patch, MagicMock
from urllib.error import HTTPError, URLError
import telegram_delivery as telegram


class TelegramDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN':'123:secret_token'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def response(self, body):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(body).encode()
        return response

    def test_send_uses_private_chat_and_actual_message(self):
        with patch('telegram_delivery.urlopen', return_value=self.response({'ok':True,'result':{'message_id':42}})) as send:
            result = telegram.deliver('123456', 'A seat opened in A.L')
            self.assertEqual(result, {'status':'delivered','provider_message_id':'42'})
            fields = json.loads(send.call_args.args[0].data)
            self.assertEqual(fields, {'chat_id':'123456','text':'A seat opened in A.L'})
            self.assertNotIn('allow_paid_broadcast', fields)

    def test_provider_errors_are_safe_and_rate_limit_can_retry(self):
        for code, permanent in [(400,True), (401,True), (403,True), (429,False), (503,False)]:
            error = HTTPError('https://api.telegram.org/botSECRET', code, 'private recipient', {},
                io.BytesIO(json.dumps({'parameters':{'retry_after':40},'description':'secret_token'}).encode()))
            with patch('telegram_delivery.urlopen', side_effect=error):
                with self.assertRaises(telegram.TelegramError) as caught:
                    telegram.deliver('123456', 'hello')
                self.assertEqual(caught.exception.permanent, permanent)
                self.assertEqual(telegram.error_summary(caught.exception), 'Telegram ' + str(code))
                self.assertEqual(caught.exception.retry_after, 40)

    def test_invalid_configuration_and_network_do_not_expose_token(self):
        with patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN':''}), patch('telegram_delivery.urlopen') as send:
            with self.assertRaises(telegram.TelegramError):
                telegram.deliver('123456', 'hello')
            send.assert_not_called()
        with patch('telegram_delivery.urlopen', side_effect=URLError('secret_token')):
            with self.assertRaises(telegram.TelegramError) as caught:
                telegram.deliver('123456', 'hello')
            self.assertEqual(str(caught.exception), 'Telegram network')
        with self.assertRaises(telegram.TelegramError):
            telegram.deliver('-123456', 'hello')

    def test_api_failure_and_invalid_response_are_not_sent(self):
        for body in [{'ok':False,'error_code':403}, {'ok':True,'result':{}}, []]:
            with patch('telegram_delivery.urlopen', return_value=self.response(body)):
                with self.assertRaises(telegram.TelegramError):
                    telegram.deliver('123456', 'hello')
