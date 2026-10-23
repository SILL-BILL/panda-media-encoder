from dataclasses import FrozenInstanceError, fields, replace
import unittest

from panda_media_encoder.models import GifPreset, PaletteSettings


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.preset = GifPreset(
            schema_version=1, id="gif_review", name="GIF確認用", format="gif",
            max_width=640, max_height=640, fps=10, keep_aspect_ratio=True,
            allow_upscale=False, palette=PaletteSettings("full", "sierra2_4a"), loop=0,
        )

    def test_fields_preserve_settings(self):
        self.assertEqual(self.preset.max_width, 640)
        self.assertEqual(self.preset.fps, 10)
        self.assertEqual(self.preset.palette.dither, "sierra2_4a")
        self.assertFalse(self.preset.allow_upscale)

    def test_preset_is_immutable(self):
        with self.assertRaises(FrozenInstanceError):
            self.preset.fps = 20

    def test_nested_palette_is_immutable(self):
        with self.assertRaises(FrozenInstanceError):
            self.preset.palette.dither = "bayer"

    def test_value_equality_and_explicit_copy(self):
        self.assertEqual(self.preset, replace(self.preset))
        self.assertNotEqual(self.preset, replace(self.preset, fps=5))

    def test_model_has_no_speculative_history_or_case_fields(self):
        self.assertEqual({field.name for field in fields(GifPreset)}, {
            "schema_version", "id", "name", "format", "max_width", "max_height",
            "fps", "keep_aspect_ratio", "allow_upscale", "palette", "loop",
        })


if __name__ == "__main__":
    unittest.main()
