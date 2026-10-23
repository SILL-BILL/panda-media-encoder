import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from panda_media_encoder.paths import PathValidationError, plan_gif_output, validate_input_path


class PathTests(unittest.TestCase):
    def setUp(self):
        # Only self-created, non-media dummy data in the OS test temp directory.
        self.temp = tempfile.TemporaryDirectory(prefix="panda_core_test_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.output = self.root / "lab" / "output"
        self.output.mkdir(parents=True)
        self.source = self.root / "dummy.dat"
        self.source.write_bytes(b"synthetic path-test data; not media")

    def test_local_input_outside_lab_is_accepted(self):
        self.assertEqual(validate_input_path(self.source), self.source)

    def test_output_name_is_planned_without_writes(self):
        before = sorted(str(path) for path in self.root.rglob("*"))
        with patch.object(Path, "open", side_effect=AssertionError("must not open file contents")):
            result = plan_gif_output(self.source, self.root)
        self.assertEqual(result, self.output / "dummy_review.gif")
        self.assertFalse(result.exists())
        self.assertEqual(before, sorted(str(path) for path in self.root.rglob("*")))
        self.assertEqual(self.source.read_bytes(), b"synthetic path-test data; not media")

    def test_unicode_and_spaces(self):
        source = self.root / "確認 用.dat"
        source.write_bytes(b"dummy")
        self.assertEqual(plan_gif_output(source, self.root).name, "確認 用_review.gif")

    def test_existing_output_is_preserved(self):
        target = self.output / "dummy_review.gif"
        target.write_bytes(b"existing dummy")
        with self.assertRaisesRegex(PathValidationError, "already exists"):
            plan_gif_output(self.source, self.root)
        self.assertEqual(target.read_bytes(), b"existing dummy")

    def test_existing_case_alias_is_collision(self):
        (self.output / "DUMMY_REVIEW.GIF").write_bytes(b"existing dummy")
        with self.assertRaises(PathValidationError):
            plan_gif_output(self.source, self.root)

    def test_existing_directory_is_collision(self):
        (self.output / "dummy_review.gif").mkdir()
        with self.assertRaises(PathValidationError):
            plan_gif_output(self.source, self.root)

    def test_queue_collision(self):
        candidate = plan_gif_output(self.source, self.root)
        with self.assertRaisesRegex(PathValidationError, "queued candidate"):
            plan_gif_output(self.source, self.root, [candidate])

    def test_queue_case_alias_is_collision(self):
        with self.assertRaises(PathValidationError):
            plan_gif_output(self.source, self.root, [self.output / "DUMMY_REVIEW.GIF"])

    def test_different_extensions_same_stem_collide(self):
        second = self.root / "dummy.other"
        second.write_bytes(b"dummy")
        with self.assertRaises(PathValidationError):
            plan_gif_output(second, self.root, [plan_gif_output(self.source, self.root)])

    def test_noncolliding_queue(self):
        self.assertEqual(plan_gif_output(self.source, self.root, [self.output / "other.gif"]),
                         self.output / "dummy_review.gif")

    def test_queue_outside_approved_area(self):
        for target in (self.root / "outside.gif", self.output / ".." / "outside.gif"):
            with self.subTest(target=target), self.assertRaises(PathValidationError):
                plan_gif_output(self.source, self.root, [target])

    def test_same_file_hardlink_is_rejected(self):
        target = self.output / "dummy_review.gif"
        os.link(self.source, target)
        with self.assertRaisesRegex(PathValidationError, "same file"):
            plan_gif_output(self.source, self.root)
        self.assertTrue(self.source.samefile(target))

    def test_input_directory_is_rejected(self):
        with self.assertRaisesRegex(PathValidationError, "regular file"):
            validate_input_path(self.root)

    def test_missing_input_is_rejected(self):
        with self.assertRaises(PathValidationError):
            validate_input_path(self.root / "missing.dat")

    def test_output_directory_must_exist_without_creation(self):
        other = self.root / "empty_repository"
        other.mkdir()
        with self.assertRaises(PathValidationError):
            plan_gif_output(self.source, other)
        self.assertFalse((other / "lab").exists())

    def test_urls_unc_devices_and_drive_relative_rejected_before_filesystem(self):
        values = ("https://example.invalid/a.mov", "file:///C:/a.mov", "ftp://host/a",
                  "\\\\server\\share\\a.mov", "//server/share/a.mov",
                  "\\\\?\\C:\\a.mov", "\\\\.\\NUL", "C:relative.mov")
        with patch.object(Path, "lstat", side_effect=AssertionError("must not inspect remote paths")):
            for value in values:
                with self.subTest(value=value), self.assertRaises(PathValidationError):
                    validate_input_path(value)

    def test_mapped_network_drive_rejected_before_filesystem(self):
        with patch("panda_media_encoder.paths._is_remote_drive", return_value=True):
            with patch.object(Path, "lstat", side_effect=AssertionError("must not inspect network")):
                with self.assertRaisesRegex(PathValidationError, "network drives"):
                    validate_input_path(self.source)

    def test_invalid_windows_aliases(self):
        for value in ("", "bad\x00name", "NUL.dat", "CON", "bad:name.dat",
                      "trailing.dat.", "trailing.dat ", "wild*.dat", "question?.dat"):
            with self.subTest(value=value), self.assertRaises(PathValidationError):
                validate_input_path(value)

    def test_input_symlink_is_rejected(self):
        original = Path.lstat

        def fake_lstat(path):
            if path == self.source:
                return SimpleNamespace(st_mode=stat.S_IFLNK, st_file_attributes=0)
            return original(path)

        with patch.object(Path, "lstat", fake_lstat):
            with self.assertRaisesRegex(PathValidationError, "symlinks"):
                validate_input_path(self.source)

    def test_output_junction_rejected_before_following(self):
        original = Path.lstat
        junction = self.root / "lab"

        def fake_lstat(path):
            if path == junction:
                return SimpleNamespace(st_mode=stat.S_IFDIR,
                                       st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
            if path == self.output:
                raise AssertionError("must reject the junction before following it")
            return original(path)

        with patch.object(Path, "lstat", fake_lstat):
            with self.assertRaisesRegex(PathValidationError, "reparse"):
                plan_gif_output(self.source, self.root)

    def test_resolved_output_escape_is_rejected(self):
        original = Path.resolve

        def fake_resolve(path, strict=False):
            if path == self.output:
                return self.root / "outside"
            return original(path, strict=strict)

        with patch.object(Path, "resolve", fake_resolve):
            with self.assertRaisesRegex(PathValidationError, "escapes"):
                plan_gif_output(self.source, self.root)

    def test_permission_failure_is_validation_error(self):
        with patch.object(Path, "lstat", side_effect=PermissionError):
            with self.assertRaises(PathValidationError):
                validate_input_path(self.source)


if __name__ == "__main__":
    unittest.main()
