import unittest

from main import _cors_settings


class LocalCorsSettingsTests(unittest.TestCase):
    def test_production_vercel_site_is_allowed(self) -> None:
        origins, origin_regex = _cors_settings(None, "production")

        self.assertIn("https://kokwet2.vercel.app", origins)
        self.assertNotRegex("https://kokwet2.vercel.app.evil.test", origin_regex)

    def test_local_origins_remain_allowed_with_custom_origins(self) -> None:
        origins, origin_regex = _cors_settings(
            "https://admin.example.test/", "development"
        )

        self.assertIn("https://admin.example.test", origins)
        self.assertIn("http://localhost:5173", origins)
        self.assertRegex("http://192.168.1.20:5173", origin_regex)

    def test_production_does_not_allow_local_origins(self) -> None:
        origins, origin_regex = _cors_settings(
            "https://admin.example.test/", "production"
        )

        self.assertIn("https://admin.example.test", origins)
        self.assertNotIn("http://localhost:5173", origins)
        self.assertNotRegex("http://localhost:5173", origin_regex)


if __name__ == "__main__":
    unittest.main()
