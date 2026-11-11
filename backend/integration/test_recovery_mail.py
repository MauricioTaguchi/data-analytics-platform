"""Exercise durable SMTP delivery across real API/scheduler restarts."""

from email.parser import BytesParser
from email.policy import default as email_policy
from socketserver import StreamRequestHandler, ThreadingTCPServer
from threading import Lock, Thread
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from sqlalchemy import text

from integration.conftest import eventually


def test_recovery_mail_survives_restart_and_retries_without_changing_link(runtime):
    accepted = []
    attempted = []
    lock = Lock()

    class LocalSMTPHandler(StreamRequestHandler):
        def handle(self):
            self.wfile.write(b"220 localhost recovery integration\r\n")
            while line := self.rfile.readline():
                command = line.split(b" ", 1)[0].strip().upper()
                if command in {b"EHLO", b"HELO"}:
                    self.wfile.write(b"250 localhost\r\n")
                elif command in {b"MAIL", b"RCPT", b"RSET"}:
                    self.wfile.write(b"250 OK\r\n")
                elif command == b"DATA":
                    self.wfile.write(b"354 Send message\r\n")
                    body = []
                    while (part := self.rfile.readline()) != b".\r\n":
                        if not part:
                            return
                        body.append(part[1:] if part.startswith(b"..") else part)
                    message = BytesParser(policy=email_policy).parsebytes(b"".join(body))
                    with lock:
                        attempted.append(message)
                        fail = len(attempted) == 1
                        if not fail:
                            accepted.append(message)
                    self.wfile.write(b"451 Try again later\r\n" if fail else b"250 Accepted\r\n")
                elif command == b"QUIT":
                    self.wfile.write(b"221 Goodbye\r\n")
                    return
                else:
                    self.wfile.write(b"502 Unsupported command\r\n")

    with ThreadingTCPServer(("127.0.0.1", 0), LocalSMTPHandler) as server:
        server.daemon_threads = True
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            runtime.stop_schedulers()
            runtime.stop("api")
            runtime.environment.update({
                "SMTP_HOST": "127.0.0.1", "SMTP_PORT": str(server.server_address[1]),
                "SMTP_TLS_MODE": "plain", "SMTP_USERNAME": "", "SMTP_PASSWORD": "",
                "SMTP_FROM_EMAIL": "accounts@example.com", "PASSWORD_RESET_URL": "https://app.example.com/",
            })
            runtime.start_api()
            email = f"recovery-{uuid4().hex}@example.com"
            runtime.request("POST", "/auth/register", expected=201, json={
                "name": "Recovery integration", "email": email, "password": "OriginalPassword123",
            })
            runtime.request("POST", "/auth/password-reset/request", expected=202, json={"email": email})
            queued = runtime.rows("SELECT id, state, encrypted_payload FROM password_reset_deliveries")
            assert len(queued) == 1 and queued[0]["state"] == "pending"
            assert email not in queued[0]["encrypted_payload"]
            assert not attempted
            # The API process can disappear after its 202 without losing mail.
            runtime.stop("api", kill=True)
            runtime.start_api()
            runtime.start_schedulers()
            eventually(
                lambda: runtime.rows("SELECT id FROM password_reset_deliveries WHERE state='pending' AND attempts=1"),
                description="failed SMTP attempt retained for retry",
            )
            runtime.stop_schedulers()
            with lock:
                assert len(attempted) == 1 and not accepted
                first_link = next(line for line in attempted[0].get_content().splitlines() if line.startswith("https://"))
            token = parse_qs(urlsplit(first_link).fragment)["reset_token"][0]
            saved = runtime.rows("SELECT state, encrypted_payload FROM password_reset_deliveries")[0]
            assert saved["state"] == "pending" and token not in saved["encrypted_payload"]
            assert runtime.rows("SELECT delivered_at FROM password_reset_tokens")[0]["delivered_at"] is None
            with runtime.database.begin() as connection:
                connection.execute(text("UPDATE password_reset_deliveries SET next_attempt_at=now()-interval '1 second' WHERE id=:id"), {"id": queued[0]["id"]})
            runtime.start_schedulers()
            eventually(
                lambda: runtime.rows("SELECT id FROM password_reset_deliveries WHERE state='sent' AND attempts=2"),
                description="same durable message accepted after scheduler restart",
            )
            with lock:
                assert len(accepted) == 1
                second_link = next(line for line in accepted[0].get_content().splitlines() if line.startswith("https://"))
            assert second_link == first_link
            runtime.request("POST", "/auth/password-reset/confirm", expected=204, json={
                "token": token, "new_password": "ReplacementPassword456",
            })
            eventually(
                lambda: runtime.rows("SELECT id FROM password_reset_deliveries WHERE kind='changed' AND state='sent'"),
                description="durable password change notification",
            )
            runtime.request("POST", "/auth/password-reset/confirm", expected=400, json={
                "token": token, "new_password": "AnotherPassword789",
            })
            runtime.request("POST", "/auth/login", json={"email": email, "password": "ReplacementPassword456"})
            assert all(row["encrypted_payload"] is None for row in runtime.rows("SELECT encrypted_payload FROM password_reset_deliveries"))
        finally:
            runtime.stop_schedulers()
            server.shutdown()
            thread.join(timeout=5)
