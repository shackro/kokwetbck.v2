import unittest
from unittest.mock import patch

from pydantic import ValidationError

from app.catalog import check_daraja_sandbox
from app.culture import _daraja_access_token
from app.impact import DEFAULT_IMPACT, ImpactContent
from app.roadmap import DEFAULT_ROADMAP, RoadmapContent


class PageMediaCompatibilityTests(unittest.TestCase):
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
