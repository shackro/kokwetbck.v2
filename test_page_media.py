import base64
import io
import json
import os
import sqlite3
import urllib.error
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError

from app.catalog import check_daraja_sandbox
from app.culture import _daraja_access_token, _mpesa_settings
from app.impact import DEFAULT_IMPACT, ImpactContent
from app.roadmap import DEFAULT_ROADMAP, RoadmapContent
from app.site_content import DEFAULT_SITE_CONTENT, LEGACY_DEMO_PASS_DESCRIPTION, _read_site_content


class PageMediaCompatibilityTests(unittest.TestCase):
    def test_legacy_demo_pass_copy_is_replaced_for_existing_site_content(self):
        stored = DEFAULT_SITE_CONTENT.model_dump(mode="json")
        stored["home"]["pass_section"]["description"] = LEGACY_DEMO_PASS_DESCRIPTION
        stored["services"]["name_inspiration_eyebrow"] = "Interactive cultural sandbox"
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE site_content (content_id TEXT PRIMARY KEY, content_json TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO site_content (content_id, content_json) VALUES ('public', ?)",
            (json.dumps(stored),),
        )

        result = _read_site_content(connection)

        self.assertNotIn("demo", result["home"]["pass_section"]["description"].lower())
        self.assertEqual(
            result["home"]["pass_section"]["description"],
            "Every confirmed pass includes reserved inventory, secure checkout "
            "and a unique digital entry token.",
        )
        self.assertEqual(
            result["services"]["name_inspiration_eyebrow"],
            "Cultural name inspiration",
        )

    def test_impact_content_adds_new_component_fields_to_older_records(self):
        payload = DEFAULT_IMPACT.model_dump(mode="json")
        for key in (
            "pathways_eyebrow",
            "pathways_title_first",
            "pathways_title_highlight",
            "pathways_copy",
            "accountability_label",
            "unpublished_report_title",
            "unpublished_report_body",
            "report_link_label",
            "resource_reporting_label",
            "resource_empty_title",
            "resource_empty_body",
            "goals_eyebrow",
            "goals_body",
        ):
            payload.pop(key)
        payload["hero"].pop("image_url")
        for pathway in payload["pathways"]:
            pathway.pop("image_url")

        content = ImpactContent.model_validate(payload)

        self.assertEqual(content.hero.image_url, "")
        self.assertEqual(content.pathways[0].image_url, "")
        self.assertEqual(content.pathways_eyebrow, "A shared effort")

    def test_impact_media_rejects_non_https_external_urls(self):
        payload = DEFAULT_IMPACT.model_dump(mode="json")
        payload["hero"]["image_url"] = "http://example.test/impact.jpg"

        with self.assertRaises(ValidationError):
            ImpactContent.model_validate(payload)

    def test_roadmap_content_adds_new_component_fields_to_older_records(self):
        payload = DEFAULT_ROADMAP.model_dump(mode="json")
        for key in (
            "hero_image_url",
            "hero_cta_label",
            "journey_eyebrow",
            "journey_title_first",
            "journey_title_highlight",
            "feedback_eyebrow",
            "feedback_success_message",
            "feedback_button_label",
            "feedback_placeholder",
        ):
            payload.pop(key)
        for phase in payload["phases"]:
            phase.pop("image_url")

        content = RoadmapContent.model_validate(payload)

        self.assertEqual(content.hero_image_url, "")
        self.assertEqual(content.phases[0].image_url, "")
        self.assertEqual(content.journey_eyebrow, "A shared direction")

    def test_roadmap_media_rejects_non_https_external_urls(self):
        payload = DEFAULT_ROADMAP.model_dump(mode="json")
        payload["hero_image_url"] = "http://example.test/roadmap.jpg"

        with self.assertRaises(ValidationError):
            RoadmapContent.model_validate(payload)


class DarajaSandboxCheckTests(unittest.TestCase):
    @patch("app.culture._request_json", return_value={"access_token": "sandbox-token"})
    def test_oauth_check_uses_sandbox_and_returns_token_only_to_backend(self, request_json):
        token = _daraja_access_token("sandbox", "test-consumer-key", "test-consumer-secret")

        self.assertEqual(token, "sandbox-token")
        request_json.assert_called_once()
        request = request_json.call_args.args[0]
        self.assertTrue(request.startswith("https://sandbox.safaricom.co.ke/oauth/v1/generate?"))
        self.assertNotIn("api.safaricom.co.ke", request)
        authorization = request_json.call_args.kwargs["headers"]["Authorization"]
        encoded_credentials = authorization.removeprefix("Basic ")
        self.assertEqual(
            base64.b64decode(encoded_credentials),
            b"test-consumer-key:test-consumer-secret",
        )

    def test_mpesa_settings_trims_environment_values(self):
        with patch.dict(
            os.environ,
            {
                "MPESA_ENV": " sandbox ",
                "MPESA_CONSUMER_KEY": " consumer-key ",
                "MPESA_CONSUMER_SECRET": " consumer-secret ",
                "MPESA_SHORTCODE": " 174379 ",
                "MPESA_PASSKEY": " passkey ",
                "MPESA_CALLBACK_URL": " https://example.test/callback ",
                "CULTURE_WEBHOOK_SECRET": " webhook-secret ",
            },
            clear=True,
        ):
            settings = _mpesa_settings()

        self.assertEqual(
            settings,
            ("sandbox", "consumer-key", "consumer-secret", "174379", "passkey"),
        )

    @patch(
        "app.culture._request_json",
        side_effect=urllib.error.HTTPError(
            "https://sandbox.safaricom.co.ke/oauth/v1/generate",
            400,
            "Bad Request",
            {},
            io.BytesIO(
                b'{"errorCode":"InvalidAuthentication","errorMessage":"Invalid consumer credentials"}'
            ),
        ),
    )
    def test_oauth_error_reports_daraja_reason_without_credentials(self, _request_json):
        with self.assertRaises(HTTPException) as raised:
            _daraja_access_token("sandbox", "private-key", "private-secret")

        self.assertEqual(raised.exception.status_code, 502)
        self.assertIn("InvalidAuthentication", raised.exception.detail)
        self.assertIn("Invalid consumer credentials", raised.exception.detail)
        self.assertNotIn("private-key", raised.exception.detail)
        self.assertNotIn("private-secret", raised.exception.detail)

    @patch("app.catalog._daraja_access_token")
    @patch("app.catalog._mpesa_settings", return_value=(
        "sandbox",
        "test-consumer-key",
        "test-consumer-secret",
        "test-shortcode",
        "test-passkey",
    ))
    @patch("app.catalog._require_admin")
    def test_staff_check_reports_success_without_returning_access_token(
        self,
        _require_admin,
        _mpesa_settings,
        daraja_access_token,
    ):
        response = check_daraja_sandbox("Bearer test-session")

        self.assertEqual(response["status"], "connected")
        self.assertEqual(response["environment"], "sandbox")
        self.assertNotIn("access_token", response)
        daraja_access_token.assert_called_once_with(
            "sandbox",
            "test-consumer-key",
            "test-consumer-secret",
        )


if __name__ == "__main__":
    unittest.main()
