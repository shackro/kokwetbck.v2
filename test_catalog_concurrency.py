import unittest
from unittest.mock import Mock, patch

from app.catalog import get_events, get_passes, initialize_catalog_store


class CatalogConcurrencyTests(unittest.TestCase):
    def test_public_catalog_reads_do_not_reinitialize_database(self):
        connection = Mock()
        connection.execute.return_value.fetchall.return_value = []

        with (
            patch("app.catalog._connect") as connect,
            patch("app.catalog.initialize_catalog_store") as initialize,
        ):
            connect.return_value.__enter__.return_value = connection

            self.assertEqual(get_events(), [])
            self.assertEqual(get_passes(), [])

        initialize.assert_not_called()

    def test_postgres_catalog_initialization_takes_advisory_lock_first(self):
        connection = Mock()
        connection.executescript.side_effect = RuntimeError("stop after lock")

        with (
            patch("app.catalog._connect") as connect,
            patch("app.catalog.postgres_mode", return_value=True),
        ):
            connect.return_value.__enter__.return_value = connection
            with self.assertRaisesRegex(RuntimeError, "stop after lock"):
                initialize_catalog_store()

        connection.execute.assert_called_once_with("BEGIN IMMEDIATE")
        connection.executescript.assert_called_once()


if __name__ == "__main__":
    unittest.main()
