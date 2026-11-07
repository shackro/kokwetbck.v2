import io
import os
import sqlite3
import tempfile
import unittest
import urllib.error
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app.catalog import CheckoutLine, CheckoutRequest, checkout_cart, initialize_catalog_store
from app.culture import (
    MpesaRequestError,
    _daraja_access_token,
    _daraja_token_cache,
    _daraja_token_cache_lock,
    _initiate_stk_push,
)


class MpesaCheckoutErrorTests(unittest.TestCase):
    def setUp(self):
        with _daraja_token_cache_lock:
            _daraja_token_cache.clear()
        self.temp_dir = tempfile.TemporaryDirectory(prefix="kokwet-mpesa-test-")
        self.database_path = Path(self.temp_dir.name) / "checkout.sqlite3"
        self.environment = patch.dict(
            os.environ,
            {
                "APP_ENV": "development",
                "KOKWET_REQUIRE_DATABASE_URL": "false",
                "KOKWET_USE_SQLITE": "true",
                "DATABASE_URL": "",
                "KOKWET_DB_PATH": str(self.database_path),
                "TICKET_ENCRYPTION_KEY": "12" * 32,
                "MPESA_ENV": "sandbox",
                "MPESA_CONSUMER_KEY": "test-key",
                "MPESA_CONSUMER_SECRET": "test-secret",
                "MPESA_SHORTCODE": "174379",
                "MPESA_PASSKEY": "test-passkey",
                "MPESA_CALLBACK_URL": "https://example.test/api/callback",
                "CULTURE_WEBHOOK_SECRET": "test-webhook-secret",
            },
            clear=True,
        )
        self.environment.start()
        initialize_catalog_store()

    def tearDown(self):
        self.environment.stop()
        self.temp_dir.cleanup()

    def read_catalog_state(self):
        with closing(sqlite3.connect(self.database_path)) as connection:
            order = connection.execute(
                "SELECT status, inventory_released FROM festival_orders"
            ).fetchone()
            stock = connection.execute(
                "SELECT stock FROM festival_passes WHERE pass_id = 'regular'"
            ).fetchone()[0]
            ticket_status = connection.execute(
                "SELECT status FROM festival_tickets"
            ).fetchone()[0]
        return order, stock, ticket_status

    def request(self):
        return CheckoutRequest(
            items=[CheckoutLine(pass_id="regular", quantity=1)],
            phone="0703412694",
        )

    def test_definite_rejection_reports_reason_and_releases_inventory(self):
        with patch(
            "app.catalog._initiate_stk_push",
            side_effect=MpesaRequestError(
                "Daraja STK Push failed (HTTP 400), code InvalidRequest",
                payment_may_have_started=False,
            ),
        ):
            with self.assertRaises(HTTPException) as raised:
                checkout_cart(self.request())

        self.assertEqual(raised.exception.status_code, 502)
        self.assertIn("InvalidRequest", raised.exception.detail)
        self.assertIn("reservation has been released", raised.exception.detail)
        self.assertEqual(self.read_catalog_state(), (("Payment failed", 1), 420, "Payment failed"))

    def test_ambiguous_gateway_failure_keeps_order_for_reconciliation(self):
        with patch(
            "app.catalog._initiate_stk_push",
            side_effect=MpesaRequestError(
                "Daraja STK Push endpoint could not be reached",
                payment_may_have_started=True,
            ),
        ):
            with self.assertRaises(HTTPException) as raised:
                checkout_cart(self.request())

        self.assertEqual(raised.exception.status_code, 502)
        self.assertIn("Do not retry", raised.exception.detail)
        self.assertEqual(self.read_catalog_state(), (("Payment review", 0), 419, "Payment review"))

    def test_stk_rejection_includes_bounded_sanitized_gateway_reason(self):
        response_body = (
            b'{"errorCode":"InvalidRequest","errorMessage":"bad request\\nwithout credentials"}'
        )
        error = urllib.error.HTTPError(
            "https://sandbox.safaricom.co.ke/mpesa/stkpush/v1/processrequest",
            400,
            "Bad Request",
            {},
            io.BytesIO(response_body),
        )

        with (
            patch("app.culture._mpesa_settings", return_value=(
                "sandbox", "key", "secret", "174379", "passkey"
            )),
            patch("app.culture._daraja_access_token", return_value="not-printed-token"),
            patch("app.culture._request_json", side_effect=error),
        ):
            with self.assertRaises(MpesaRequestError) as raised:
                _initiate_stk_push("254703412694", "KTTEST")

        self.assertFalse(raised.exception.payment_may_have_started)
        self.assertIn("InvalidRequest", raised.exception.detail)
        self.assertIn("bad request without credentials", raised.exception.detail)
        self.assertNotIn("secret", raised.exception.detail)
        self.assertNotIn("not-printed-token", raised.exception.detail)

    def test_oauth_rejection_includes_standard_oauth_error_fields(self):
        error = urllib.error.HTTPError(
            "https://sandbox.safaricom.co.ke/oauth/v1/generate",
            400,
            "Bad Request",
            {},
            io.BytesIO(
                b'{"error":"invalid_client","error_description":"Invalid client credentials"}'
            ),
        )

        with patch("app.culture._request_json", side_effect=error):
            with self.assertRaises(MpesaRequestError) as raised:
                _daraja_access_token("sandbox", "key-not-logged", "secret-not-logged")

        self.assertFalse(raised.exception.payment_may_have_started)
        self.assertIn("invalid_client", raised.exception.detail)
        self.assertIn("Invalid client credentials", raised.exception.detail)
        self.assertNotIn("key-not-logged", raised.exception.detail)
        self.assertNotIn("secret-not-logged", raised.exception.detail)

    def test_oauth_access_token_is_reused_until_refresh_buffer(self):
        with (
            patch(
                "app.culture.time.monotonic",
                side_effect=[100, 100, 159, 160, 160],
            ),
            patch(
                "app.culture._request_json",
                side_effect=[
                    {"access_token": "first-token", "expires_in": "120"},
                    {"access_token": "second-token", "expires_in": "120"},
                ],
            ) as request_json,
        ):
            first = _daraja_access_token("sandbox", "key", "secret")
            reused = _daraja_access_token("sandbox", "key", "secret")
            refreshed = _daraja_access_token("sandbox", "key", "secret")

        self.assertEqual((first, reused, refreshed), (
            "first-token",
            "first-token",
            "second-token",
        ))
        self.assertEqual(request_json.call_count, 2)

    def test_oauth_token_cache_is_isolated_by_environment_and_credentials(self):
        with patch(
            "app.culture._request_json",
            side_effect=[
                {"access_token": "sandbox-token", "expires_in": "3599"},
                {"access_token": "production-token", "expires_in": "3599"},
                {"access_token": "other-app-token", "expires_in": "3599"},
            ],
        ) as request_json:
            sandbox = _daraja_access_token("sandbox", "key", "secret")
            production = _daraja_access_token("production", "key", "secret")
            other_app = _daraja_access_token("sandbox", "other-key", "other-secret")

        self.assertEqual(
            (sandbox, production, other_app),
            ("sandbox-token", "production-token", "other-app-token"),
        )
        self.assertEqual(request_json.call_count, 3)

    def test_stk_server_error_is_marked_ambiguous(self):
        error = urllib.error.HTTPError(
            "https://sandbox.safaricom.co.ke/mpesa/stkpush/v1/processrequest",
            500,
            "Server Error",
            {},
            io.BytesIO(b'{"errorCode":"ServerError"}'),
        )

        with (
            patch("app.culture._mpesa_settings", return_value=(
                "sandbox", "key", "secret", "174379", "passkey"
            )),
            patch("app.culture._daraja_access_token", return_value="not-printed-token"),
            patch("app.culture._request_json", side_effect=error),
        ):
            with self.assertRaises(MpesaRequestError) as raised:
                _initiate_stk_push("254703412694", "KTTEST")

        self.assertTrue(raised.exception.payment_may_have_started)


if __name__ == "__main__":
    unittest.main()
