import unittest
from contextlib import nullcontext
from unittest.mock import patch

from fastapi import HTTPException

from app.legacy_compat import (
    get_legacy_activation_summary,
    get_legacy_site_settings,
    list_legacy_event_passes,
    list_legacy_culture_posts,
    list_legacy_live_messages,
    list_legacy_profiles,
)


class _Result:
    def __init__(self, *, row=None, rows=None):
        self._row = row
        self._rows = rows or []

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class _LegacyConnection:
    def __init__(self, result_by_table):
        self.result_by_table = result_by_table
        self.statements = []

    def execute(self, statement, parameters=()):
        self.statements.append((statement, parameters))
        if "information_schema.tables" in statement:
            return _Result(row={"available": True})
        for table_name, result in self.result_by_table.items():
            if f"public.{table_name}" in statement:
                if table_name == "site_settings":
                    allowed_keys = set(parameters)
                    return _Result(
                        rows=[
                            row
                            for row in result.fetchall()
                            if row["setting_key"] in allowed_keys
                        ]
                    )
                return result
        raise AssertionError(f"Unexpected legacy query: {statement}")


class LegacyCompatibilityTests(unittest.TestCase):
    def _connection_context(self, connection):
        return (
            patch("app.legacy_compat.postgres_mode", return_value=True),
            patch(
                "app.legacy_compat._database_connect",
                return_value=nullcontext(connection),
            ),
        )

    def test_legacy_culture_feed_only_returns_visible_fields_and_https_audio(self):
        connection = _LegacyConnection(
            {
                "open_stream_interactions": _Result(
                    rows=[
                        {
                            "post_id": "unsafe-audio",
                            "text_message": "Another story",
                            "audio_clip_url": "http://media.example/audio.mp3",
                            "retention_lifespan": "7_DAYS",
                            "created_at": "2026-01-02T00:00:00Z",
                        },
                        {
                            "post_id": "visible",
                            "text_message": "A story",
                            "audio_clip_url": "https://media.example/audio.mp3",
                            "retention_lifespan": "24_HOURS",
                            "created_at": "2026-01-01T00:00:00Z",
                        },
                    ]
                )
            }
        )
        postgres_patch, connect_patch = self._connection_context(connection)
        with postgres_patch, connect_patch:
            result = list_legacy_culture_posts(limit=10, offset=20)
        self.assertEqual(result["posts"][0]["audio_clip_url"], "https://media.example/audio.mp3")
        self.assertIsNone(result["posts"][1]["audio_clip_url"])
        self.assertIn("WHERE is_visible = TRUE", connection.statements[-1][0])
        self.assertNotIn("client_ip_hash", connection.statements[-1][0])
        self.assertEqual(connection.statements[-1][1], (10, 20))

    def test_legacy_settings_are_allowlisted(self):
        connection = _LegacyConnection(
            {
                "site_settings": _Result(
                    rows=[
                        {"setting_key": "footer_tagline", "setting_value": "Community first"},
                        {"setting_key": "database_password", "setting_value": "must not leak"},
                    ]
                )
            }
        )
        postgres_patch, connect_patch = self._connection_context(connection)
        with postgres_patch, connect_patch:
            result = get_legacy_site_settings()
        query, parameters = connection.statements[-1]
        self.assertEqual(result, {"footer_tagline": "Community first"})
        self.assertNotIn("database_password", parameters)
        self.assertIn("setting_key IN", query)

    def test_legacy_live_messages_preserve_only_https_audio_urls(self):
        connection = _LegacyConnection(
            {
                "live_chat_messages": _Result(
                    rows=[
                        {
                            "message_id": "message-2",
                            "session_id": "session-1",
                            "author_name": "Member",
                            "message_text": "",
                            "message_type": "voice",
                            "audio_url": "javascript:alert(1)",
                            "tag_name": None,
                            "created_at": "2026-01-02T00:00:00Z",
                        },
                        {
                            "message_id": "message-1",
                            "session_id": "session-1",
                            "author_name": "Member",
                            "message_text": "",
                            "message_type": "voice",
                            "audio_url": "https://media.example/voice.mp3",
                            "tag_name": None,
                            "created_at": "2026-01-01T00:00:00Z",
                        },
                    ]
                )
            }
        )
        postgres_patch, connect_patch = self._connection_context(connection)
        with postgres_patch, connect_patch:
            result = list_legacy_live_messages()
        self.assertEqual(result["messages"][0]["audio_url"], "https://media.example/voice.mp3")
        self.assertIsNone(result["messages"][1]["audio_url"])

    def test_legacy_profiles_require_admin_and_omit_password_hash(self):
        connection = _LegacyConnection(
            {
                "user_service_profiles": _Result(
                    rows=[
                        {
                            "profile_id": "profile-1",
                            "phone_number": "0712345678",
                            "full_name": "Community member",
                            "audience_cohort": "Campus Comrade",
                            "role": "user",
                            "is_super_admin": False,
                            "is_menjo_verified": False,
                            "is_active": True,
                            "manager_id": None,
                            "created_at": "2026-01-01T00:00:00Z",
                        }
                    ]
                )
            }
        )
        postgres_patch, connect_patch = self._connection_context(connection)
        with patch("app.legacy_compat._staff_from_authorization") as auth, postgres_patch, connect_patch:
            result = list_legacy_profiles(authorization="Bearer staff-token", limit=20)
        auth.assert_called_once_with("Bearer staff-token", ("admin",))
        self.assertEqual(result["profiles"][0]["phone_number"], "0712345678")
        self.assertNotIn("password_hash", connection.statements[-1][0])

    def test_legacy_event_passes_are_admin_only_and_hide_encrypted_qr(self):
        connection = _LegacyConnection(
            {
                "secure_event_passes": _Result(
                    rows=[
                        {
                            "pass_id": "pass-1",
                            "buyer_phone": "0712345678",
                            "pass_tier": "regular",
                            "status": "PAID",
                            "scanned_at": None,
                            "mpesa_receipt": "receipt-1",
                            "created_at": "2026-01-01T00:00:00Z",
                        }
                    ]
                )
            }
        )
        postgres_patch, connect_patch = self._connection_context(connection)
        with patch("app.legacy_compat._staff_from_authorization") as auth, postgres_patch, connect_patch:
            result = list_legacy_event_passes(
                authorization="Bearer staff-token",
                limit=20,
            )
        auth.assert_called_once_with("Bearer staff-token", ("admin",))
        self.assertEqual(result["passes"][0]["status"], "PAID")
        self.assertNotIn("encrypted_qr_string", connection.statements[-1][0])
        self.assertNotIn("encrypted_qr_string", result["passes"][0])

    def test_activation_summary_never_selects_codes_or_phone_numbers(self):
        connection = _LegacyConnection(
            {
                "activation_codes": _Result(
                    rows=[
                        {
                            "assigned_role": "creator",
                            "is_used": False,
                            "expired": False,
                            "code_count": 2,
                        }
                    ]
                )
            }
        )
        postgres_patch, connect_patch = self._connection_context(connection)
        with patch("app.legacy_compat._staff_from_authorization"), postgres_patch, connect_patch:
            result = get_legacy_activation_summary(authorization="Bearer staff-token")
        query = connection.statements[-1][0]
        self.assertEqual(result["summary"][0]["code_count"], 2)
        self.assertNotIn("activation_code,", query)
        self.assertNotIn("code_hash", query)
        self.assertNotIn("phone_number", query)

    def test_legacy_routes_report_unavailable_outside_postgres(self):
        with patch("app.legacy_compat.postgres_mode", return_value=False):
            with self.assertRaises(HTTPException) as raised:
                get_legacy_site_settings()
        self.assertEqual(raised.exception.status_code, 503)


if __name__ == "__main__":
    unittest.main()
