from copy import deepcopy
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from panda_media_encoder.presets import (
    MAX_PRESET_BYTES, PresetValidationError, load_gif_preset, parse_gif_preset,
)


PRESET_PATH = Path(__file__).resolve().parents[1] / "presets" / "gif_review.json"


class PresetTests(unittest.TestCase):
    def setUp(self):
        self.valid = json.loads(PRESET_PATH.read_text(encoding="utf-8"))

    def load_bytes(self, payload):
        with patch("panda_media_encoder.presets.Path.open", return_value=io.BytesIO(payload)):
            return load_gif_preset("not-an-actual-file.json")

    def test_initial_preset_loads(self):
        preset = load_gif_preset(PRESET_PATH)
        self.assertEqual((preset.id, preset.name, preset.format), ("gif_review", "GIF確認用", "gif"))
        self.assertEqual((preset.max_width, preset.max_height, preset.fps), (640, 640, 10))
        self.assertTrue(preset.keep_aspect_ratio)
        self.assertFalse(preset.allow_upscale)
        self.assertEqual(preset.loop, 0)
        self.assertEqual((preset.palette.stats_mode, preset.palette.dither), ("full", "sierra2_4a"))

    def test_each_required_key(self):
        for key in self.valid:
            with self.subTest(key=key):
                data = deepcopy(self.valid)
                del data[key]
                with self.assertRaisesRegex(PresetValidationError, "missing keys"):
                    parse_gif_preset(data)

    def test_unknown_command_filter_and_path_keys(self):
        for key in ("command", "ffmpeg_args", "filter", "output_path", "case_id"):
            with self.subTest(key=key):
                with self.assertRaisesRegex(PresetValidationError, "unknown keys"):
                    parse_gif_preset({**self.valid, key: "untrusted"})

    def test_top_level_must_be_object(self):
        for value in (None, [], True, 1, "gif"):
            with self.subTest(value=value), self.assertRaises(PresetValidationError):
                parse_gif_preset(value)

    def test_schema_is_exact_integer_one(self):
        for value in (0, 2, True, False, 1.0, "1", None):
            with self.subTest(value=value), self.assertRaises(PresetValidationError):
                parse_gif_preset({**self.valid, "schema_version": value})

    def test_integer_fields_reject_bool_float_string_and_null(self):
        for key in ("max_width", "max_height", "fps", "loop"):
            for value in (True, False, 1.0, "1", None, []):
                with self.subTest(key=key, value=value), self.assertRaises(PresetValidationError):
                    parse_gif_preset({**self.valid, key: value})

    def test_numeric_ranges(self):
        for key, values in {
            "max_width": (-1, 0, 641), "max_height": (-1, 0, 641),
            "fps": (-1, 0, 11), "loop": (-2, 65536),
        }.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(PresetValidationError):
                    parse_gif_preset({**self.valid, key: value})

    def test_numeric_boundaries(self):
        for key, values in {
            "max_width": (1, 640), "max_height": (1, 640),
            "fps": (1, 10), "loop": (-1, 0, 1, 65535),
        }.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    self.assertEqual(getattr(parse_gif_preset({**self.valid, key: value}), key), value)

    def test_fixed_boolean_settings(self):
        for key, values in {
            "keep_aspect_ratio": (False, 1, "true", None),
            "allow_upscale": (True, 0, "false", None),
        }.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(PresetValidationError):
                    parse_gif_preset({**self.valid, key: value})

    def test_unsupported_formats(self):
        for value in ("mp4", "bmp", "GIF", 1, None):
            with self.subTest(value=value), self.assertRaises(PresetValidationError):
                parse_gif_preset({**self.valid, "format": value})

    def test_identifier_validation(self):
        for value in ("", "../gif", "GIF", "a;b", "a" * 65, 7, None):
            with self.subTest(value=value), self.assertRaises(PresetValidationError):
                parse_gif_preset({**self.valid, "id": value})

    def test_name_validation(self):
        for value in ("", " ", " padded", "padded ", "x\ny", "x\x00y", "x" * 81, False):
            with self.subTest(value=value), self.assertRaises(PresetValidationError):
                parse_gif_preset({**self.valid, "name": value})

    def test_palette_required_and_unknown_keys(self):
        for palette in (None, [], {}, {"stats_mode": "full"},
                        {"dither": "sierra2_4a"},
                        {**self.valid["palette"], "filter": "arbitrary"}):
            with self.subTest(palette=palette), self.assertRaises(PresetValidationError):
                parse_gif_preset({**self.valid, "palette": palette})

    def test_unsupported_palette_values(self):
        for key, value in (("stats_mode", "diff"), ("stats_mode", True),
                           ("dither", "bayer"), ("dither", "sierra2_4a;other")):
            with self.subTest(key=key, value=value), self.assertRaises(PresetValidationError):
                parse_gif_preset({**self.valid, "palette": {**self.valid["palette"], key: value}})

    def test_parser_does_not_mutate_document(self):
        before = deepcopy(self.valid)
        parse_gif_preset(self.valid)
        self.assertEqual(self.valid, before)

    def test_exact_size_limit_is_accepted(self):
        raw = json.dumps(self.valid).encode("utf-8")
        self.assertEqual(self.load_bytes(raw + b" " * (MAX_PRESET_BYTES - len(raw))).id, "gif_review")

    def test_oversized_read_is_bounded(self):
        stream = io.BytesIO(b" " * (MAX_PRESET_BYTES + 20))
        with patch.object(stream, "read", wraps=stream.read) as read:
            with patch("panda_media_encoder.presets.Path.open", return_value=stream):
                with self.assertRaisesRegex(PresetValidationError, "64 KiB"):
                    load_gif_preset("not-an-actual-file.json")
            read.assert_called_once_with(MAX_PRESET_BYTES + 1)

    def test_invalid_json_and_encoding(self):
        for payload in (b"", b"{", b"\xff", b"[]", b"null", b"{} trailing"):
            with self.subTest(payload=payload), self.assertRaises(PresetValidationError):
                self.load_bytes(payload)

    def test_duplicate_keys_and_nonstandard_constants(self):
        for payload in (b'{"id":"first","id":"second"}',
                        b'{"palette":{"dither":1,"dither":2}}',
                        b'{"fps":NaN}', b'{"fps":Infinity}', b'{"fps":-Infinity}'):
            with self.subTest(payload=payload), self.assertRaises(PresetValidationError):
                self.load_bytes(payload)

    def test_excessive_nesting_is_validation_error(self):
        with self.assertRaises(PresetValidationError):
            self.load_bytes(b"[" * 2000 + b"0" + b"]" * 2000)

    def test_excessive_integer_is_validation_error(self):
        with self.assertRaises(PresetValidationError):
            self.load_bytes(b'{"fps":' + b"9" * 5000 + b"}")

    def test_read_failure_is_clear(self):
        with patch("panda_media_encoder.presets.Path.open", side_effect=PermissionError):
            with self.assertRaisesRegex(PresetValidationError, "could not be read"):
                load_gif_preset("not-an-actual-file.json")

    def test_nonstring_mapping_key_is_rejected(self):
        with self.assertRaises(PresetValidationError):
            parse_gif_preset({**self.valid, 1: "unexpected"})


if __name__ == "__main__":
    unittest.main()
