import base64
import hashlib
import json
import os
import socket
import sqlite3
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="kokwet-e2e-") as temp_dir:
        database_path = Path(temp_dir) / "journey.sqlite3"
        os.environ.update(
            {
                "KOKWET_DB_PATH": str(database_path),
                "DATABASE_URL": "",
                "KOKWET_REQUIRE_DATABASE_URL": "false",
                "PYTHON_DOTENV_DISABLED": "1",
                "TICKET_ENCRYPTION_KEY": "11" * 32,
                "KOKWET_STAFF_ACTIVATION_SECRET": "qa-only-staff-activation-secret-123",
                "CULTURE_IP_HASH_SECRET": "isolated-qa-only-secret",
                "KOKWET_STAFF_ADMIN_EMAIL": "qa-admin@example.test",
                "KOKWET_STAFF_ADMIN_PASSWORD": "qa-only-password-1234",
                "KOKWET_SUPER_ADMIN_EMAIL": "qa-root@example.test",
                "KOKWET_SUPER_ADMIN_PHONE": "0700000002",
                "KOKWET_SUPER_ADMIN_PASSWORD": "qa-only-root-password-1234",
                "KOKWET_PUBLIC_SITE_URL": "https://kokwet.example.test",
                "MPESA_ENV": "sandbox",
                "MPESA_CONSUMER_KEY": "qa-not-a-real-key",
                "MPESA_CONSUMER_SECRET": "qa-not-a-real-secret",
                "MPESA_SHORTCODE": "174379",
                "MPESA_PASSKEY": "qa-not-a-real-passkey",
                "MPESA_CALLBACK_URL": "https://callback.invalid/api/v1/culture/webhook/extend-lifespan",
                "CULTURE_WEBHOOK_SECRET": "qa-webhook-secret",
            }
        )
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                """
                CREATE TABLE staff_users (
                    staff_id TEXT PRIMARY KEY,
                    email TEXT NOT NULL UNIQUE,
                    full_name TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('admin', 'moderator', 'creator')),
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                )
                """
            )

        import uvicorn
        import app.catalog as catalog
        import app.culture as culture
        from app.publishing import create_super_admin_account
        from app.live import initialize_live_store
        from main import app

        legacy_ticket_database = Path(temp_dir) / "legacy-tickets.sqlite3"
        legacy_ticket_token = "a" * 32
        with sqlite3.connect(legacy_ticket_database) as connection:
            connection.executescript(
                """
                CREATE TABLE festival_orders (
                    order_id TEXT PRIMARY KEY,
                    phone TEXT NOT NULL,
                    total INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    simulated INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE festival_tickets (
                    ticket_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT NOT NULL REFERENCES festival_orders(order_id),
                    pass_id TEXT NOT NULL,
                    pass_name TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    token TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    simulated INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    used_at TEXT
                );
                INSERT INTO festival_orders
                    (order_id, phone, total, status, created_at)
                VALUES ('legacy-order', '0712345678', 1200, 'Paid', '2026-01-01T00:00:00+00:00');
                """
            )
            connection.execute(
                """
                INSERT INTO festival_tickets
                    (order_id, pass_id, pass_name, phone, token, status, created_at)
                VALUES ('legacy-order', 'regular', 'Festival Regular', '0712345678', ?, 'Paid', ?)
                """,
                (legacy_ticket_token, "2026-01-01T00:00:00+00:00"),
            )
        os.environ["KOKWET_DB_PATH"] = str(legacy_ticket_database)
        try:
            catalog.initialize_catalog_store()
            assert not legacy_ticket_database.with_name("ticket-encryption.key").exists()
            with sqlite3.connect(legacy_ticket_database) as connection:
                migrated_ticket = connection.execute(
                    """
                    SELECT ticket_id, token, token_hash, encrypted_token
                    FROM festival_tickets WHERE order_id = 'legacy-order'
                    """
                ).fetchone()
            assert migrated_ticket[1] != legacy_ticket_token
            assert migrated_ticket[2] == hashlib.sha256(legacy_ticket_token.encode()).hexdigest()
            with sqlite3.connect(legacy_ticket_database) as connection:
                preserved_ticket = connection.execute(
                    """
                    SELECT status FROM festival_tickets
                    WHERE token_hash = ?
                    """,
                    (hashlib.sha256(legacy_ticket_token.encode()).hexdigest(),),
                ).fetchone()
            assert preserved_ticket == ("Paid",)
            assert catalog._decrypt_stored_ticket_token(
                migrated_ticket[0], migrated_ticket[3], catalog._ticket_encryption_key()
            ) == legacy_ticket_token
        finally:
            os.environ["KOKWET_DB_PATH"] = str(database_path)
        print("PASS existing paid-ticket QR migration preserves the original QR while hashing its lookup")

        legacy_live_database = Path(temp_dir) / "legacy-live.sqlite3"
        with sqlite3.connect(legacy_live_database) as connection:
            connection.executescript(
                """
                CREATE TABLE live_sessions (
                    session_id TEXT PRIMARY KEY,
                    creator_name TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    stream_url TEXT NOT NULL,
                    topic_title TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    created_by TEXT
                );
                CREATE TABLE live_messages (
                    message_id TEXT PRIMARY KEY,
                    session_id TEXT,
                    channel TEXT NOT NULL,
                    author_name TEXT NOT NULL,
                    message_text TEXT NOT NULL,
                    client_ip_hash TEXT NOT NULL,
                    is_hidden INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                INSERT INTO live_messages
                    (message_id, channel, author_name, message_text, client_ip_hash, created_at)
                VALUES ('legacy-message', 'public', 'Legacy User', 'Still here', 'legacy-hash', '2026-01-01T00:00:00+00:00');
                """
            )
        os.environ["KOKWET_DB_PATH"] = str(legacy_live_database)
        try:
            initialize_live_store()
        finally:
            os.environ["KOKWET_DB_PATH"] = str(database_path)
        with sqlite3.connect(legacy_live_database) as connection:
            migrated_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(live_messages)")
            }
            legacy_message = connection.execute(
                "SELECT message_text, message_type FROM live_messages WHERE message_id = ?",
                ("legacy-message",),
            ).fetchone()
        assert {"message_type", "tag_name", "audio_content_type", "audio_content"} <= migrated_columns
        assert legacy_message == ("Still here", "text")
        print("PASS live-message schema migration preserves older text chat")

        cli_database_path = Path(temp_dir) / "cli-bootstrap.sqlite3"
        configured_environment = {
            key: os.environ.pop(key)
            for key in (
                "KOKWET_STAFF_ADMIN_EMAIL",
                "KOKWET_STAFF_ADMIN_PASSWORD",
                "KOKWET_SUPER_ADMIN_EMAIL",
                "KOKWET_SUPER_ADMIN_PHONE",
                "KOKWET_SUPER_ADMIN_PASSWORD",
            )
        }
        journey_database_path = os.environ["KOKWET_DB_PATH"]
        os.environ["KOKWET_DB_PATH"] = str(cli_database_path)
        try:
            cli_staff_id = create_super_admin_account(
                "cli-root@example.test",
                "0712345679",
                "qa-cli-root-password-1234",
            )
            with sqlite3.connect(cli_database_path) as connection:
                cli_root = connection.execute(
                    """
                    SELECT staff_id, phone, role, is_super_admin, password_hash
                    FROM staff_users WHERE staff_id = ?
                    """,
                    (cli_staff_id,),
                ).fetchone()
                cli_bootstrap_audit = connection.execute(
                    """
                    SELECT method, path, actor_staff_id
                    FROM staff_audit_events WHERE actor_staff_id = ?
                    """,
                    (cli_staff_id,),
                ).fetchone()
            assert cli_root is not None
            assert cli_root[1:4] == ("+254712345679", "admin", 1)
            assert cli_root[4].startswith("pbkdf2_sha256$")
            assert cli_root[4] != "qa-cli-root-password-1234"
            assert cli_bootstrap_audit is not None
            assert tuple(cli_bootstrap_audit) == ("BOOTSTRAP", "interactive-cli", cli_staff_id)
            try:
                create_super_admin_account(
                    "second-root@example.test",
                    "0712345670",
                    "qa-second-root-password-1234",
                )
            except ValueError as error:
                assert "already configured" in str(error)
            else:
                raise AssertionError("CLI provisioning must reject a second Super Admin")
            print("PASS Super Admin CLI provisioning stores a normalized phone and password hash")
        finally:
            os.environ["KOKWET_DB_PATH"] = journey_database_path
            os.environ.update(configured_environment)

        stk_payloads: list[dict[str, object]] = []
        original_request_json = culture._request_json

        def fake_request_json(
            url: str,
            *,
            headers: dict[str, str],
            body: dict[str, object] | None = None,
        ) -> dict[str, object]:
            if "/oauth/" in url:
                return {"access_token": "qa-access-token"}
            if body is not None:
                stk_payloads.append(body)
            return {"ResponseCode": "0", "CheckoutRequestID": "ws_CO_qa_payload"}

        culture._request_json = fake_request_json
        try:
            culture._initiate_stk_push(
                "254712345678",
                "KTQA123456",
                amount=1200,
                description="Kokwet Festival ticket",
                callback_path="/api/v1/tickets/webhook/mpesa",
            )
        finally:
            culture._request_json = original_request_json
        assert len(stk_payloads) == 1
        assert stk_payloads[0]["Amount"] == 1200
        assert stk_payloads[0]["TransactionDesc"] == "Kokwet Festival ticket"
        callback_value = stk_payloads[0]["CallBackURL"]
        assert isinstance(callback_value, str)
        callback_parts = urllib.parse.urlsplit(callback_value)
        assert callback_parts.path == "/api/v1/tickets/webhook/mpesa"
        assert urllib.parse.parse_qs(callback_parts.query)["token"] == ["qa-webhook-secret"]
        print("PASS Daraja payload: correct amount, ticket callback URL, and test-only gateway stub")

        checkout_ids = iter((
            "ws_CO_qa_extension",
            "ws_CO_qa_ticket",
            "ws_CO_qa_expired_ticket",
            "ws_CO_qa_review_ticket",
        ))

        stk_requests: list[dict[str, object]] = []

        def fake_stk_push(phone: str, account_reference: str, **kwargs: object) -> dict[str, str]:
            checkout_request_id = next(checkout_ids)
            stk_requests.append({
                "phone": phone,
                "account_reference": account_reference,
                "checkout_request_id": checkout_request_id,
                **kwargs,
            })
            return {"ResponseCode": "0", "CheckoutRequestID": checkout_request_id}

        culture._initiate_stk_push = fake_stk_push
        catalog._initiate_stk_push = fake_stk_push
        ticket_emails: list[dict[str, str]] = []

        def fake_ticket_email(email: str, order_id: str, ticket_url: str) -> None:
            ticket_emails.append({
                "email": email,
                "order_id": order_id,
                "ticket_url": ticket_url,
            })

        catalog._send_ticket_email = fake_ticket_email

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{port}"

        def call(
            method: str,
            path: str,
            payload: dict[str, object] | None = None,
            headers: dict[str, str] | None = None,
            raw_body: bytes | None = None,
        ) -> tuple[int, dict[str, object]]:
            request_headers = dict(headers or {})
            body = raw_body
            if payload is not None:
                body = json.dumps(payload).encode()
                request_headers["Content-Type"] = "application/json"
            request = urllib.request.Request(
                base_url + path,
                data=body,
                headers=request_headers,
                method=method,
            )
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    return response.status, json.loads(response.read())
            except urllib.error.HTTPError as error:
                response_body = error.read()
                return error.code, json.loads(response_body) if response_body else {}

        def call_bytes(
            path: str,
            headers: dict[str, str] | None = None,
        ) -> tuple[int, str, bytes]:
            request = urllib.request.Request(base_url + path, headers=headers or {})
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    return response.status, response.headers.get_content_type(), response.read()
            except urllib.error.HTTPError as error:
                return error.code, error.headers.get_content_type(), error.read()

        def call_multipart(
            path: str,
            *,
            file_name: str,
            file_field_name: str = "file",
            content_type: str,
            content: bytes,
            fields: dict[str, str],
            headers: dict[str, str] | None = None,
        ) -> tuple[int, dict[str, object]]:
            boundary = "----kokwet-e2e-boundary"
            parts = []
            for name, value in fields.items():
                parts.extend((
                    f"--{boundary}\r\n".encode(),
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                    value.encode(),
                    b"\r\n",
                ))
            parts.extend((
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{file_field_name}"; filename="{file_name}"\r\n'.encode(),
                f"Content-Type: {content_type}\r\n\r\n".encode(),
                content,
                b"\r\n",
                f"--{boundary}--\r\n".encode(),
            ))
            request_headers = dict(headers or {})
            request_headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
            request = urllib.request.Request(
                base_url + path,
                data=b"".join(parts),
                headers=request_headers,
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    return response.status, json.loads(response.read())
            except urllib.error.HTTPError as error:
                response_body = error.read()
                return error.code, json.loads(response_body) if response_body else {}

        def mpesa_callback(checkout_id: str, amount: int, result_code: int = 0) -> dict[str, object]:
            return {
                "Body": {
                    "stkCallback": {
                        "CheckoutRequestID": checkout_id,
                        "ResultCode": result_code,
                        "CallbackMetadata": {
                            "Item": [{"Name": "Amount", "Value": amount}]
                        },
                    }
                }
            }

        callback_url = "/api/v1/tickets/webhook/mpesa?" + urllib.parse.urlencode(
            {"token": "qa-webhook-secret"}
        )

        try:
            for _ in range(100):
                try:
                    if call("GET", "/api/health")[0] == 200:
                        break
                except (urllib.error.URLError, TimeoutError):
                    time.sleep(0.1)
            else:
                raise RuntimeError("The isolated Kokwet API failed to start")

            status, registration = call(
                "POST",
                "/api/v1/users/register",
                {
                    "full_name": "QA Visitor",
                    "phone_number": "0712345678",
                    "audience_cohort": "Campus Comrade",
                    "active_service_seeking": "Career Mentorship",
                },
            )
            assert status == 201 and registration["profile"]["verified"] is False
            with sqlite3.connect(database_path) as connection:
                persisted_profile = connection.execute(
                    "SELECT phone FROM community_profiles WHERE phone = ?",
                    ("0712345678",),
                ).fetchone()
            assert persisted_profile is not None
            duplicate_status, _ = call(
                "POST",
                "/api/profiles",
                {
                    "full_name": "QA Visitor",
                    "phone": "0712345678",
                    "cohort": "Campus Comrade",
                },
            )
            assert duplicate_status == 409
            print("PASS 1/6: registration is persisted and duplicate phone numbers are rejected")

            boundary = "----kokwet-e2e-boundary"
            multipart = (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"text_message\"\r\n\r\n"
                "QA journey post\r\n"
                f"--{boundary}--\r\n"
            ).encode()
            status, post = call(
                "POST",
                "/api/v1/culture/post",
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                raw_body=multipart,
            )
            assert status == 201 and isinstance(post.get("post_id"), str)
            post_id = post["post_id"]
            status, feed = call("GET", "/api/v1/culture/feed")
            assert status == 200 and any(item["post_id"] == post_id for item in feed["posts"])
            print("PASS 2/6: anonymous post is created and visible in Open Hearth")

            status, tagged_message = call(
                "POST",
                "/api/v1/live/messages",
                {
                    "channel": "public",
                    "author_name": "QA Listener",
                    "tag_name": "YouthAction",
                },
            )
            assert status == 201
            assert tagged_message["message"]["message_type"] == "tag"
            assert tagged_message["message"]["tag_name"] == "YouthAction"

            status, voice_message = call_multipart(
                "/api/v1/live/messages/voice",
                file_name="qa-voice.webm",
                file_field_name="audio_file",
                content_type="audio/webm",
                content=b"\x1a\x45\xdf\xa3qa voice note",
                fields={"channel": "public", "author_name": "QA Listener"},
            )
            assert status == 201
            assert voice_message["message"]["message_type"] == "voice"
            audio_url = voice_message["message"]["audio_url"]
            assert isinstance(audio_url, str)
            audio_status, audio_type, audio = call_bytes(audio_url)
            assert audio_status == 200 and audio_type == "audio/webm"
            assert audio.startswith(b"\x1a\x45\xdf\xa3")
            status, _ = call_multipart(
                "/api/v1/live/messages/voice",
                file_name="invalid.webm",
                file_field_name="audio_file",
                content_type="audio/webm",
                content=b"not an audio file",
                fields={"channel": "public", "author_name": "QA Listener"},
            )
            assert status == 415
            status, _ = call_multipart(
                "/api/v1/live/messages/voice",
                file_name="oversized.webm",
                file_field_name="audio_file",
                content_type="audio/webm",
                content=b"\x1a\x45\xdf\xa3" + b"x" * (2 * 1024 * 1024),
                fields={"channel": "public", "author_name": "QA Listener"},
            )
            assert status == 413
            status, live_feed = call("GET", "/api/v1/live/messages?channel=public")
            assert status == 200
            assert any(item["message_id"] == voice_message["message"]["message_id"] for item in live_feed["messages"])
            print("PASS: live lounge stores topic tags and validated, playable voice notes")

            status, extension = call(
                "POST",
                "/api/v1/culture/extend",
                {"post_id": post_id, "phone_number": "0712345678", "amount": 30},
            )
            assert status == 200 and extension["status"] == "pending_payment"
            extension_callback_url = "/api/v1/culture/webhook/extend-lifespan?" + urllib.parse.urlencode(
                {"token": "qa-webhook-secret"}
            )
            status, callback = call(
                "POST",
                extension_callback_url,
                mpesa_callback(extension["checkout_request_id"], 30),
            )
            assert status == 200 and callback["status"] == "success"
            status, feed = call("GET", "/api/v1/culture/feed")
            preserved = next(item for item in feed["posts"] if item["post_id"] == post_id)
            assert preserved["retention_lifespan"] == "7_DAYS"
            print("PASS 3/6: mocked M-Pesa callback verifies KSh 30 and preserves the post")

            status, order = call(
                "POST",
                "/api/checkout/cart",
                {"items": [{"pass_id": "regular", "quantity": 1}], "phone": "0712345678"},
            )
            assert status == 200 and order["status"] == "Payment pending"
            assert order["simulated"] is False and order["tickets"] == []
            assert stk_requests[-1]["phone"] == "254712345678"
            assert stk_requests[-1]["amount"] == order["total"]
            assert stk_requests[-1]["callback_path"] == "/api/v1/tickets/webhook/mpesa"
            unpaid_email_status, _ = call(
                "POST",
                f"/api/v1/tickets/orders/{order['order_id']}/email",
                {"email": "visitor@example.test"},
            )
            assert unpaid_email_status == 409
            first_ticket_checkout_id = stk_requests[-1]["checkout_request_id"]
            assert isinstance(first_ticket_checkout_id, str)
            mismatch_status, mismatch = call(
                "POST",
                callback_url,
                mpesa_callback(first_ticket_checkout_id, order["total"] - 1),
            )
            assert mismatch_status == 200 and mismatch["status"] == "review"
            paid_status, paid_callback = call(
                "POST",
                callback_url,
                mpesa_callback(first_ticket_checkout_id, order["total"]),
            )
            assert paid_status == 200 and paid_callback["status"] == "success"
            status, paid_order = call("GET", f"/api/v1/tickets/orders/{order['order_id']}")
            assert status == 200 and paid_order["status"] == "Paid"
            assert len(paid_order["tickets"]) == 1
            ticket_token = paid_order["tickets"][0]["ticket_id"]
            assert paid_order["tickets"][0]["status"] == "Paid"
            assert ticket_token.startswith("KWT1.")
            assert "0712345678" not in ticket_token and "regular" not in ticket_token
            decoded_qr = catalog._decode_ticket_qr(ticket_token, catalog._ticket_encryption_key())
            assert decoded_qr == (
                paid_order["tickets"][0]["id"],
                catalog._ticket_owner_binding("0712345678", catalog._ticket_encryption_key()),
            )
            email_status, email_result = call(
                "POST",
                f"/api/v1/tickets/orders/{order['order_id']}/email",
                {"email": "visitor@example.test"},
            )
            assert email_status == 200 and email_result["status"] == "sent"
            assert email_result["email"] == "visitor@example.test"
            assert email_result["ticket_url"] == (
                f"https://kokwet.example.test/tickets/{order['order_id']}"
            )
            assert ticket_emails == [{
                "email": "visitor@example.test",
                "order_id": order["order_id"],
                "ticket_url": email_result["ticket_url"],
            }]
            invalid_email_status, _ = call(
                "POST",
                f"/api/v1/tickets/orders/{order['order_id']}/email",
                {"email": "not-an-email"},
            )
            assert invalid_email_status == 422
            _, emailed_order = call("GET", f"/api/v1/tickets/orders/{order['order_id']}")
            assert emailed_order["email_sent"] is True
            with sqlite3.connect(database_path) as connection:
                stored_email = connection.execute(
                    "SELECT receipt_email, receipt_email_sent_at FROM festival_orders WHERE order_id = ?",
                    (order["order_id"],),
                ).fetchone()
            assert stored_email[0] == "visitor@example.test" and stored_email[1]
            with sqlite3.connect(database_path) as connection:
                stored_ticket = connection.execute(
                    "SELECT token, token_hash, encrypted_token FROM festival_tickets WHERE ticket_id = ?",
                    (paid_order["tickets"][0]["id"],),
                ).fetchone()
            assert stored_ticket[0] != ticket_token
            assert stored_ticket[1] == hashlib.sha256(ticket_token.encode()).hexdigest()
            assert stored_ticket[2] and stored_ticket[2] != ticket_token
            print("PASS confirmed tickets can be privately linked and emailed; unpaid orders cannot request email")

            expiry_status, expiring_order = call(
                "POST",
                "/api/checkout/cart",
                {"items": [{"pass_id": "regular", "quantity": 1}], "phone": "0712345678"},
            )
            assert expiry_status == 200 and expiring_order["status"] == "Payment pending"
            expired_checkout_id = stk_requests[-1]["checkout_request_id"]
            assert isinstance(expired_checkout_id, str)
            with sqlite3.connect(database_path) as connection:
                connection.execute(
                    "UPDATE festival_orders SET reservation_expires_at = ? WHERE order_id = ?",
                    ("2000-01-01T00:00:00+00:00", expiring_order["order_id"]),
                )
            _, passes_after_expiry = call("GET", "/api/passes")
            stock_after_release = next(item["stock"] for item in passes_after_expiry if item["id"] == "regular")
            _, passes_after_repeat = call("GET", "/api/passes")
            assert next(item["stock"] for item in passes_after_repeat if item["id"] == "regular") == stock_after_release
            late_status, late_callback = call(
                "POST",
                callback_url,
                mpesa_callback(expired_checkout_id, expiring_order["total"]),
            )
            assert late_status == 200 and late_callback["status"] == "success", (
                late_status,
                late_callback,
            )
            _, late_order = call(
                "GET", f"/api/v1/tickets/orders/{expiring_order['order_id']}"
            )
            assert late_order["status"] == "Paid"
            late_ticket_token = late_order["tickets"][0]["ticket_id"]

            _, review_order = call(
                "POST",
                "/api/checkout/cart",
                {"items": [{"pass_id": "regular", "quantity": 1}], "phone": "0712345678"},
            )
            review_checkout_id = stk_requests[-1]["checkout_request_id"]
            assert isinstance(review_checkout_id, str)
            review_status, review_callback = call(
                "POST",
                callback_url,
                mpesa_callback(review_checkout_id, review_order["total"] - 1),
            )
            assert review_status == 200 and review_callback["status"] == "review"
            print("PASS 4/6: payment callback issues a ticket; expired reservations release stock once")

            admin_status, admin_login = call(
                "POST",
                "/api/v1/staff/auth/login",
                {"email": "qa-admin@example.test", "password": "qa-only-password-1234"},
            )
            assert admin_status == 200
            admin_auth = {"Authorization": f"Bearer {admin_login['token']}"}
            room_status, room_result = call(
                "POST",
                "/api/v1/staff/live",
                {"topic_title": "Youth mentorship group"},
                admin_auth,
            )
            assert room_status == 201
            assert room_result["session"]["is_active"] is True
            assert room_result["session"]["stream_url"] is None
            assert room_result["session"]["platform"] is None
            current_room_status, current_room = call("GET", "/api/v1/live/current")
            assert current_room_status == 200
            assert current_room["session"]["session_id"] == room_result["session"]["session_id"]
            print("PASS: moderators can create an active chat-only topic room")

            root_status, root_login = call(
                "POST",
                "/api/v1/staff/auth/login",
                {"identifier": "0700000002", "password": "qa-only-root-password-1234"},
            )
            assert root_status == 200
            assert root_login["staff"]["role"] == "super_admin"
            assert root_login["staff"]["is_super_admin"] is True
            root_auth = {"Authorization": f"Bearer {root_login['token']}"}
            with sqlite3.connect(database_path) as connection:
                root_count = connection.execute(
                    "SELECT COUNT(*) FROM staff_users WHERE is_super_admin = 1"
                ).fetchone()[0]
            assert root_count == 1
            root_users_status, root_users = call(
                "GET", "/api/v1/staff/users", headers=root_auth
            )
            assert root_users_status == 200
            root_account = next(user for user in root_users["users"] if user["is_super_admin"])
            protected_role_status, _ = call(
                "PATCH",
                f"/api/v1/staff/users/{root_account['staff_id']}",
                {"role": "moderator"},
                admin_auth,
            )
            protected_delete_status, _ = call(
                "DELETE",
                f"/api/v1/staff/users/{root_account['staff_id']}",
                headers=admin_auth,
            )
            assert protected_role_status == 403 and protected_delete_status == 409
            forged_invite_status, _ = call(
                "POST",
                "/api/v1/staff/users",
                {"phone": "0700000003", "role": "super_admin"},
                admin_auth,
            )
            assert forged_invite_status == 422
            denied_audit_status, _ = call(
                "GET", "/api/v1/staff/super-admin/audit", headers=admin_auth
            )
            assert denied_audit_status == 403
            root_content_status, _ = call(
                "GET", "/api/v1/staff/site-content", headers=root_auth
            )
            assert root_content_status == 200
            image_bytes = b"\x89PNG\r\n\x1a\n" + b"uploaded-test-image"
            unauthorized_upload_status, _ = call_multipart(
                "/api/v1/staff/media",
                file_name="cover.png",
                content_type="image/png",
                content=image_bytes,
                fields={"media_kind": "image"},
            )
            assert unauthorized_upload_status == 401
            upload_status, upload_result = call_multipart(
                "/api/v1/staff/media",
                file_name="cover.png",
                content_type="image/png",
                content=image_bytes,
                fields={"media_kind": "image"},
                headers=root_auth,
            )
            assert upload_status == 201 and upload_result["content_type"] == "image/png"
            uploaded_media_url = upload_result["url"]
            assert isinstance(uploaded_media_url, str) and uploaded_media_url.startswith("/api/media/")
            media_status, media_type, served_media = call_bytes(uploaded_media_url)
            assert media_status == 200 and media_type == "image/png" and served_media == image_bytes
            ranged_media_status, ranged_media_type, ranged_media = call_bytes(
                uploaded_media_url, {"Range": "bytes=0-7"}
            )
            assert ranged_media_status == 206 and ranged_media_type == "image/png"
            assert ranged_media == image_bytes[:8]
            invalid_type_status, _ = call_multipart(
                "/api/v1/staff/media",
                file_name="cover.png",
                content_type="image/png",
                content=image_bytes,
                fields={
                    "media_kind": "image",
                    "expected_content_type": "image/webp",
                },
                headers=root_auth,
            )
            assert invalid_type_status == 415
            image_data = "data:image/png;base64,iVBORw0KGgo="
            wav_data = "data:audio/wav;base64," + base64.b64encode(
                b"RIFF\x04\x00\x00\x00WAVE"
            ).decode()
            homepage_story_status, homepage_story = call(
                "POST",
                "/api/v1/staff/stories",
                {
                    "title": "House voices in every story",
                    "body": "",
                    "cover_image_url": image_data,
                    "audio_data": wav_data,
                    "is_published": True,
                },
                root_auth,
            )
            assert homepage_story_status == 201
            assert homepage_story["story"]["body"] == ""
            public_stories_status, public_stories = call("GET", "/api/v1/stories")
            published_home_story = next(
                item for item in public_stories["stories"]
                if item["story_id"] == homepage_story["story"]["story_id"]
            )
            assert public_stories_status == 200 and published_home_story["audio_clip_url"]
            homepage_audio_status, homepage_audio_type, homepage_audio = call_bytes(
                published_home_story["audio_clip_url"]
            )
            assert homepage_audio_status == 200
            assert homepage_audio_type == "audio/wav" and homepage_audio.startswith(b"RIFF")

            house_story_payload = {
                "title": "Kipsigis voice",
                "body": "",
                "cover_image_url": image_data,
                "audio_data": wav_data,
                "speaker_name": "QA Speaker",
                "dialect": "Kipsigis",
            }
            house_story_status, house_story_result = call(
                "PUT",
                "/api/v1/staff/house-voice-stories/Kipsigis/1",
                house_story_payload,
                root_auth,
            )
            assert house_story_status == 200
            house_story_id = house_story_result["story"]["story_id"]
            for update_number in range(1, 5):
                house_story_payload["title"] = f"Kipsigis voice update {update_number}"
                house_story_status, house_story_result = call(
                    "PUT",
                    "/api/v1/staff/house-voice-stories/Kipsigis/1",
                    house_story_payload,
                    root_auth,
                )
                assert house_story_status == 200
            limited_update_status, _ = call(
                "PUT",
                "/api/v1/staff/house-voice-stories/Kipsigis/1",
                house_story_payload,
                root_auth,
            )
            assert limited_update_status == 429
            public_house_status, public_house = call(
                "GET", "/api/v1/house-voice-stories/Kipsigis"
            )
            assert public_house_status == 200 and len(public_house["stories"]) == 1
            assert public_house["stories"][0]["story_id"] == house_story_id
            house_audio_status, house_audio_type, house_audio = call_bytes(
                public_house["stories"][0]["audio_clip_url"]
            )
            assert house_audio_status == 200
            assert house_audio_type == "audio/wav" and house_audio.startswith(b"RIFF")
            slot_status, slot_data = call(
                "GET",
                "/api/v1/staff/house-voice-stories/Kipsigis",
                headers=root_auth,
            )
            assert slot_status == 200 and slot_data["slots"][0]["publishes_remaining"] == 0
            with sqlite3.connect(database_path) as connection:
                connection.execute(
                    "UPDATE house_voice_stories SET expires_at = ? WHERE story_id = ?",
                    ("2000-01-01T00:00:00+00:00", house_story_id),
                )
            expired_house_status, expired_house = call(
                "GET", "/api/v1/house-voice-stories/Kipsigis"
            )
            expired_house_audio_status, _, _ = call_bytes(
                f"/api/v1/house-voice-stories/{house_story_id}/audio"
            )
            assert expired_house_status == 200 and expired_house["stories"] == []
            assert expired_house_audio_status == 404

            text_story_status, text_story_result = call(
                "PUT",
                "/api/v1/staff/house-voice-stories/Kipsigis/2",
                {
                    "title": "House memory",
                    "body": "A written-only story opened in the reading window.",
                },
                root_auth,
            )
            assert text_story_status == 200
            text_story = text_story_result["story"]
            assert text_story["cover_image_url"] is None
            assert text_story["audio_clip_url"] is None
            text_public_status, text_public_data = call(
                "GET", "/api/v1/house-voice-stories/Kipsigis"
            )
            assert text_public_status == 200
            public_text_story = next(
                story for story in text_public_data["stories"]
                if story["story_id"] == text_story["story_id"]
            )
            assert public_text_story["body"] == text_story["body"]
            assert public_text_story["cover_image_url"] is None
            assert public_text_story["audio_clip_url"] is None
            text_audio_status, _, _ = call_bytes(
                f"/api/v1/house-voice-stories/{text_story['story_id']}/audio"
            )
            assert text_audio_status == 404
            audio_only_story_status, audio_only_story = call(
                "PUT",
                "/api/v1/staff/house-voice-stories/Kipsigis/3",
                {
                    "title": "Elder greeting",
                    "audio_data": wav_data,
                    "cover_image_url": "https://images.example.test/kipsigis-cover.jpg",
                    "speaker_name": "QA Speaker",
                },
                root_auth,
            )
            assert audio_only_story_status == 200
            assert audio_only_story["story"]["body"] == ""
            assert audio_only_story["story"]["audio_clip_url"]
            assert audio_only_story["story"]["cover_image_url"] == "https://images.example.test/kipsigis-cover.jpg"
            audio_only_public_status, audio_only_public = call(
                "GET", "/api/v1/house-voice-stories/Kipsigis"
            )
            assert audio_only_public_status == 200
            public_audio_only = next(
                story for story in audio_only_public["stories"]
                if story["story_id"] == audio_only_story["story"]["story_id"]
            )
            assert public_audio_only["audio_clip_url"]

            root_invite_status, _ = call(
                "POST",
                "/api/v1/staff/users",
                {"phone": "0700000004", "role": "creator"},
                root_auth,
            )
            assert root_invite_status == 201
            audit_status, audit = call(
                "GET", "/api/v1/staff/super-admin/audit", headers=root_auth
            )
            assert audit_status == 200 and any(
                event["path"] == "/api/v1/staff/users"
                and event["actor_staff_id"] == root_account["staff_id"]
                for event in audit["events"]
            )
            print("PASS Super Admin: server seed, phone sign-in, protected identity, scoped access and audit trail")

            reviews_status, reviews = call(
                "GET", "/api/v1/staff/ticket-orders/review", headers=admin_auth
            )
            assert reviews_status == 200 and any(
                item["order_id"] == review_order["order_id"] for item in reviews["orders"]
            )
            moderator_status, moderator = call(
                "POST",
                "/api/v1/staff/users",
                {
                    "phone": "0711111111",
                    "role": "moderator",
                    "can_scan_tickets": True,
                },
                admin_auth,
            )
            assert moderator_status == 201
            moderator_invite = moderator["activation"]
            assert isinstance(moderator_invite, dict)
            assert len(moderator_invite["code"]) == 6
            activation_key_path = database_path.with_name("staff-activation.key")
            assert not activation_key_path.exists()
            activation_payload = {
                "phone": "0711111111",
                "activation_code": f"{(int(moderator_invite['code']) + 1) % 1_000_000:06d}",
                "full_name": "Gate Moderator",
                "email": "gate-moderator@example.test",
                "password": "qa-gate-password-1234",
            }
            invalid_activation_status, _ = call(
                "POST", "/api/v1/staff/auth/activate", activation_payload
            )
            assert invalid_activation_status == 400
            activation_payload["activation_code"] = moderator_invite["code"]
            activation_status, activated_moderator = call(
                "POST", "/api/v1/staff/auth/activate", activation_payload
            )
            assert activation_status == 201
            assert activated_moderator["staff"]["phone"] == "+254711111111"
            assert activated_moderator["staff"]["can_scan_tickets"] is True
            moderator_auth = {"Authorization": f"Bearer {activated_moderator['token']}"}
            assignment_status, _ = call(
                "PATCH",
                f"/api/v1/staff/users/{activated_moderator['staff']['staff_id']}",
                {"house_names": ["Nandi"]},
                admin_auth,
            )
            assert assignment_status == 200
            assigned_house_status, _ = call(
                "GET",
                "/api/v1/staff/house-voice-stories/Nandi",
                headers=moderator_auth,
            )
            unassigned_house_status, _ = call(
                "GET",
                "/api/v1/staff/house-voice-stories/Kipsigis",
                headers=moderator_auth,
            )
            assert assigned_house_status == 200 and unassigned_house_status == 403
            assigned_story_payload = {
                **house_story_payload,
                "title": "Nandi staff voice",
            }
            assigned_story_status, _ = call(
                "PUT",
                "/api/v1/staff/house-voice-stories/Nandi/1",
                assigned_story_payload,
                moderator_auth,
            )
            assert assigned_story_status == 200
            replay_status, _ = call(
                "POST", "/api/v1/staff/auth/activate", activation_payload
            )
            assert replay_status == 400
            phone_login_status, phone_login = call(
                "POST",
                "/api/v1/staff/auth/login",
                {"identifier": "0711111111", "password": "qa-gate-password-1234"},
            )
            assert phone_login_status == 200 and phone_login["staff"]["role"] == "moderator"
            invitation_list_status, invitation_list = call(
                "GET", "/api/v1/staff/activation-codes", headers=admin_auth
            )
            assert invitation_list_status == 200
            assert all(
                "code" not in item and "code_hash" not in item
                for item in invitation_list["invitations"]
            )
            duplicate_invite_status, _ = call(
                "POST",
                "/api/v1/staff/users",
                {"phone": "0711111111", "role": "creator"},
                admin_auth,
            )
            assert duplicate_invite_status == 409
            expired_status, expired_invite = call(
                "POST",
                "/api/v1/staff/users",
                {"phone": "0744444444", "role": "creator"},
                admin_auth,
            )
            assert expired_status == 201 and isinstance(expired_invite["activation"], dict)
            expired_code = expired_invite["activation"]["code"]
            with sqlite3.connect(database_path) as connection:
                connection.execute(
                    "UPDATE staff_activation_codes SET expires_at = ? WHERE phone = ?",
                    ("2000-01-01T00:00:00+00:00", "+254744444444"),
                )
            expired_activation_status, _ = call(
                "POST",
                "/api/v1/staff/auth/activate",
                {
                    "phone": "0744444444",
                    "activation_code": expired_code,
                    "full_name": "Expired Invite",
                    "email": "expired-invite@example.test",
                    "password": "qa-expired-password-1234",
                },
            )
            assert expired_activation_status == 400
            revoked_status, revoked_invite = call(
                "POST",
                "/api/v1/staff/users",
                {"phone": "0755555555", "role": "creator"},
                admin_auth,
            )
            assert revoked_status == 201 and isinstance(revoked_invite["activation"], dict)
            activation_id = revoked_invite["activation"]["activation_id"]
            revoke_status, _ = call(
                "DELETE",
                f"/api/v1/staff/activation-codes/{activation_id}",
                headers=admin_auth,
            )
            assert revoke_status == 200
            revoked_activation_status, _ = call(
                "POST",
                "/api/v1/staff/auth/activate",
                {
                    "phone": "0755555555",
                    "activation_code": revoked_invite["activation"]["code"],
                    "full_name": "Revoked Invite",
                    "email": "revoked-invite@example.test",
                    "password": "qa-revoked-password-1234",
                },
            )
            assert revoked_activation_status == 400
            blocked_status, blocked = call(
                "POST",
                "/api/v1/staff/users",
                {
                    "phone": "0722222222",
                    "role": "moderator",
                },
                admin_auth,
            )
            assert blocked_status == 201
            blocked_invite = blocked["activation"]
            assert isinstance(blocked_invite, dict)
            blocked_activation_status, _ = call(
                "POST",
                "/api/v1/staff/auth/activate",
                {
                    "phone": "0722222222",
                    "activation_code": blocked_invite["code"],
                    "full_name": "Other Moderator",
                    "email": "other-moderator@example.test",
                    "password": "qa-other-password-1234",
                },
            )
            assert blocked_activation_status == 201
            _, denied_login = call(
                "POST",
                "/api/v1/staff/auth/login",
                {"identifier": "0722222222", "password": "qa-other-password-1234"},
            )
            moderator_invite_denied_status, _ = call(
                "POST",
                "/api/v1/staff/users",
                {"phone": "0733333333", "role": "creator"},
                {"Authorization": f"Bearer {denied_login['token']}"},
            )
            assert moderator_invite_denied_status == 403
            denied_status, _ = call(
                "POST",
                "/api/v1/tickets/validate",
                {"ticket_id": ticket_token},
                {"Authorization": f"Bearer {denied_login['token']}"},
            )
            assert denied_status == 403
            print("PASS 5/6: one-time invites activate phone-login staff, and scan permissions stay role-checked")
            _, scanner_login = call(
                "POST",
                "/api/v1/staff/auth/login",
                {"email": "gate-moderator@example.test", "password": "qa-gate-password-1234"},
            )
            scanner_auth = {"Authorization": f"Bearer {scanner_login['token']}"}
            tampered_ticket_token = ticket_token[:6] + (
                "A" if ticket_token[6] != "A" else "B"
            ) + ticket_token[7:]
            tampered_status, tampered_scan = call(
                "POST",
                "/api/v1/tickets/validate",
                {"ticket_id": tampered_ticket_token},
                scanner_auth,
            )
            assert tampered_status == 200 and tampered_scan["status"] == "invalid"
            with sqlite3.connect(database_path) as connection:
                connection.execute(
                    "UPDATE festival_tickets SET phone = ? WHERE token_hash = ?",
                    ("0700000000", hashlib.sha256(ticket_token.encode()).hexdigest()),
                )
            owner_mismatch_status, owner_mismatch = call(
                "POST",
                "/api/v1/tickets/validate",
                {"ticket_id": ticket_token},
                scanner_auth,
            )
            assert owner_mismatch_status == 200 and owner_mismatch["status"] == "invalid"
            with sqlite3.connect(database_path) as connection:
                connection.execute(
                    "UPDATE festival_tickets SET phone = ? WHERE token_hash = ?",
                    ("0712345678", hashlib.sha256(ticket_token.encode()).hexdigest()),
                )
            first_scan_status, first_scan = call(
                "POST", "/api/v1/tickets/validate", {"ticket_id": ticket_token}, scanner_auth
            )
            second_scan_status, second_scan = call(
                "POST", "/api/v1/tickets/validate", {"ticket_id": ticket_token}, scanner_auth
            )
            assert first_scan_status == second_scan_status == 200
            assert first_scan["status"] == "valid" and first_scan["owner_phone_last4"] == "5678"
            assert second_scan["status"] == "duplicate"
            assert second_scan["scanned_at"] == first_scan["scanned_at"]
            with ThreadPoolExecutor(max_workers=2) as executor:
                concurrent_scans = list(executor.map(
                    lambda _: call(
                        "POST",
                        "/api/v1/tickets/validate",
                        {"ticket_id": late_ticket_token},
                        scanner_auth,
                    ),
                    range(2),
                ))
            assert {scan[1]["status"] for scan in concurrent_scans} == {"valid", "duplicate"}
            disabled_status, _ = call(
                "PATCH",
                f"/api/v1/staff/users/{activated_moderator['staff']['staff_id']}",
                {"is_active": False},
                admin_auth,
            )
            assert disabled_status == 200
            revoked_session_status, _ = call(
                "GET", "/api/v1/staff/auth/me", headers=scanner_auth
            )
            assert revoked_session_status == 401
            print("PASS 6/6: duplicate scans are rejected and disabling staff revokes the active session")
            print("RESULT: isolated end-to-end journey passed (M-Pesa network was mocked; no real charge was made).")
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            listener.close()


if __name__ == "__main__":
    main()
