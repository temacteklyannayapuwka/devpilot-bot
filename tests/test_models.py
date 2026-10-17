import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from codex_limits import LimitsUnavailable
from models import ModelSettings, SettingsError, available_models, model_command


class ModelCommandTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings = ModelSettings(Path(self.directory.name) / "settings.json")

    def test_selection_survives_restart(self):
        with patch("models.available_models", AsyncMock(return_value=[{"model": "test-model"}])):
            text = model_command("/model", "test-model", self.settings)
        self.assertIn("Сохранена", text)
        self.assertEqual(ModelSettings(self.settings.path).current(), "test-model")

    def test_unknown_model_does_not_replace_selection(self):
        self.settings.save("existing")
        with patch("models.available_models", AsyncMock(return_value=[{"model": "existing"}])):
            model_command("/model", "unknown", self.settings)
        self.assertEqual(self.settings.current(), "existing")

    def test_failed_catalog_does_not_replace_selection(self):
        self.settings.save("existing")
        with patch("models.available_models", AsyncMock(side_effect=LimitsUnavailable("Недоступно"))):
            self.assertEqual(model_command("/model", "other", self.settings), "Недоступно")
        self.assertEqual(self.settings.current(), "existing")

    def test_reset_does_not_need_codex(self):
        self.settings.save("existing")
        with patch("models.available_models", AsyncMock()) as fetch:
            model_command("/model", "default", self.settings)
        fetch.assert_not_called()
        self.assertIsNone(self.settings.current())

    def test_invalid_settings_are_reported(self):
        self.settings.path.write_text("broken json")
        with self.assertRaises(SettingsError):
            self.settings.current()

    def test_failed_write_preserves_previous_selection(self):
        self.settings.save("existing")
        with patch("models.os.replace", side_effect=OSError):
            with self.assertRaises(SettingsError):
                self.settings.save("other")
        self.assertEqual(self.settings.current(), "existing")
        self.assertEqual(len(list(self.settings.path.parent.iterdir())), 1)


class ModelCatalogTests(unittest.IsolatedAsyncioTestCase):
    async def test_pages_are_loaded_and_hidden_models_excluded(self):
        with patch("models.codex_request", AsyncMock(side_effect=[
            {"data": [{"model": "first"}, {"model": "hidden", "hidden": True}], "nextCursor": "next"},
            {"data": [{"model": "second"}], "nextCursor": None},
        ])) as request:
            result = await available_models()
        self.assertEqual([item["model"] for item in result], ["first", "second"])
        self.assertEqual(request.call_args.args[1]["cursor"], "next")

    async def test_repeated_cursor_is_rejected(self):
        with patch("models.codex_request", AsyncMock(return_value={"data": [], "nextCursor": "same"})):
            with self.assertRaises(LimitsUnavailable):
                await available_models()
