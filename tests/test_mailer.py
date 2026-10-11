from email import policy
from email.parser import BytesParser
import os
import socketserver
import threading
import unittest
from unittest.mock import patch

import mailer


class MailerTests(unittest.TestCase):
    def test_real_smtp_transport_delivers_message_to_local_receiver(self):
        received = []

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.wfile.write(b'220 local-test SMTP\r\n')
                while True:
                    line = self.rfile.readline()
                    if not line:
                        return
                    command = line.split(b' ',1)[0].strip().upper()
                    if command in (b'EHLO', b'HELO'):
                        self.wfile.write(b'250 local-test\r\n')
                    elif command == b'DATA':
                        self.wfile.write(b'354 send message\r\n')
                        lines = []
                        while True:
                            data = self.rfile.readline()
                            if data == b'.\r\n' or not data:
                                break
                            lines.append(data[1:] if data.startswith(b'..') else data)
                        received.append(BytesParser(policy=policy.default).parsebytes(b''.join(lines)))
                        self.wfile.write(b'250 message accepted\r\n')
                    elif command == b'QUIT':
                        self.wfile.write(b'221 goodbye\r\n')
                        return
                    else:
                        self.wfile.write(b'250 OK\r\n')

        server = socketserver.ThreadingTCPServer(('127.0.0.1',0),Handler)
        thread = threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        try:
            with patch.dict(os.environ, {'SMTP_HOST':'127.0.0.1','SMTP_PORT':str(server.server_address[1]),
                'SMTP_FROM':'sender@example.test','SMTP_USERNAME':'','SMTP_USE_TLS':'false',
                'SMTP_USE_SSL':'false','APP_ENV':'development'}):
                self.assertTrue(mailer.send_email('recipient@example.test','Verification',
                    'One-time link: http://127.0.0.1/#verify-token=test', 'test-message'))
            self.assertEqual(len(received),1)
            self.assertEqual(received[0]['To'],'recipient@example.test')
            self.assertIn('#verify-token=test',received[0].get_content())
            self.assertTrue(received[0]['Message-ID'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_gmail_starttls_authentication_and_rejected_recipient(self):
        with patch.dict(os.environ, {'SMTP_HOST':'smtp.gmail.com','SMTP_PORT':'587',
            'SMTP_FROM':'sender@gmail.com','SMTP_USERNAME':'sender@gmail.com','SMTP_PASSWORD':'test-only',
            'SMTP_USE_TLS':'true','SMTP_USE_SSL':'false'}), patch('mailer.smtplib.SMTP') as smtp:
            client = smtp.return_value.__enter__.return_value
            client.send_message.return_value = {}
            mailer.send_email('recipient@example.test','Subject','Body')
            client.starttls.assert_called_once()
            client.login.assert_called_once_with('sender@gmail.com','test-only')
            client.send_message.return_value = {'recipient@example.test':(550,b'rejected')}
            with self.assertRaises(RuntimeError):
                mailer.send_email('recipient@example.test','Subject','Body')

    def test_missing_configuration_is_not_a_delivery(self):
        with patch.dict(os.environ, {'SMTP_HOST':'','SMTP_FROM':'','SMTP_USERNAME':''}):
            with self.assertRaises(RuntimeError):
                mailer.send_email('recipient@example.test','Subject','Body')


if __name__ == '__main__':
    unittest.main()
