"""Handwritten synthetic JSON only: no real media or external tools."""

from copy import deepcopy
from decimal import Decimal
from fractions import Fraction
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from panda_media_encoder.paths import PathValidationError
from panda_media_encoder.process_runner import (
    DEFAULT_FFPROBE, PROBE_OPTIONS, CommandValidationError, ProcessResult, RunStatus,
)
from panda_media_encoder.probe import (
    MAX_JSON_BYTES, ProbeError, ProbeErrorCode, build_probe_command,
    parse_probe_json, probe_input,
)


def video(index=4, **changes):
    return {
        "index": index, "codec_type": "video", "codec_name": "mpeg4",
        "width": 1920, "height": 1080, "avg_frame_rate": "30000/1001",
        "sample_aspect_ratio": "1:1", "display_aspect_ratio": "16:9",
        "duration": "2.5", "disposition": {"attached_pic": 0}, **changes,
    }


def document(*streams):
    return {"streams": list(streams) or [video()],
            "format": {"format_name": "avi", "duration": "2.5"}}


def payload(data):
    return json.dumps(data).encode("utf-8")


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.popen = self.enterContext(patch("subprocess.Popen", side_effect=AssertionError("real child forbidden")))
        self.path_check = self.enterContext(patch(
            "panda_media_encoder.probe.validate_input_path", side_effect=lambda value: Path(value)))
        self.runner = self.enterContext(patch("panda_media_encoder.probe.run_process"))
        self.runner.return_value = ProcessResult(RunStatus.SUCCEEDED, 0, payload(document()))
        self.source = Path(r"C:\synthetic\input.avi")

    def assert_error(self, code, data):
        with self.assertRaises(ProbeError) as caught:
            parse_probe_json(payload(data))
        self.assertEqual(caught.exception.code, code)

    def test_normal_metadata_and_real_stream_index(self):
        info = parse_probe_json(payload(document()))
        self.assertEqual(info.selected_stream_index, 4)
        self.assertEqual(info.duration, Decimal("2.5"))
        stream = info.video_streams[0]
        self.assertEqual((stream.index, stream.codec, stream.width, stream.height), (4, "mpeg4", 1920, 1080))
        self.assertEqual((stream.fps, stream.sar, stream.dar), (Fraction(30000, 1001), Fraction(1), Fraction(16, 9)))
        self.assertFalse(stream.attached_pic)

    def test_audio_first_does_not_change_actual_index(self):
        info = parse_probe_json(payload(document({"index": 0, "codec_type": "audio", "codec_name": "aac"}, video(7))))
        self.assertEqual(info.selected_stream_index, 7)

    def test_nonvideo_fields_are_still_validated(self):
        for fields in ({"tags": []}, {"codec_name": True}, {"duration": []},
                       {"disposition": "bad"}, {"width": True}, {"avg_frame_rate": "1/0"}):
            with self.subTest(fields=fields):
                self.assert_error(ProbeErrorCode.INVALID_DATA,
                                  document({"index": 0, "codec_type": "audio", **fields}, video()))
        data = document({"index": 0, "codec_type": "audio", "avg_frame_rate": "0/0"}, video())
        self.assertEqual(parse_probe_json(payload(data)).selected_stream_index, 4)

    def test_first_valid_video_not_lowest_numeric_index(self):
        info = parse_probe_json(payload(document(video(9), video(2))))
        self.assertEqual(info.selected_stream_index, 9)
        self.assertEqual([stream.index for stream in info.video_streams], [9, 2])

    def test_cover_is_retained_but_not_selected(self):
        info = parse_probe_json(payload(document(video(1, disposition={"attached_pic": 1}), video(5))))
        self.assertEqual(info.selected_stream_index, 5)
        self.assertTrue(info.video_streams[0].attached_pic)

    def test_no_video_and_empty_streams(self):
        for streams in ([], [{"index": 0, "codec_type": "audio"}]):
            data = document()
            data["streams"] = streams
            self.assert_error(ProbeErrorCode.NO_VIDEO, data)

    def test_cover_only(self):
        self.assert_error(ProbeErrorCode.COVER_ONLY, document(video(disposition={"attached_pic": 1})))

    def test_unknown_duration_is_not_a_failure(self):
        for value in (None, "N/A"):
            data = document(video(duration=value))
            data["format"]["duration"] = value
            info = parse_probe_json(payload(data))
            self.assertIsNone(info.duration)
            self.assertIsNone(info.video_streams[0].duration)
        data = document(video())
        del data["streams"][0]["duration"]
        del data["format"]["duration"]
        self.assertIsNone(parse_probe_json(payload(data)).duration)

    def test_invalid_durations(self):
        for value in (True, -1, 2.5, "-1", "NaN", "Infinity", "1e9", "", "10000000001"):
            with self.subTest(value=value):
                self.assert_error(ProbeErrorCode.INVALID_DATA, document(video(duration=value)))

    def test_width_height_reject_zero_negative_huge_bool_string_float(self):
        for field in ("width", "height"):
            for value in (0, -1, 32769, True, "1920", 1920.0, None, [], {}):
                with self.subTest(field=field, value=value):
                    self.assert_error(ProbeErrorCode.INVALID_DATA, document(video(**{field: value})))

    def test_dimension_boundaries(self):
        info = parse_probe_json(payload(document(video(width=1, height=32768))))
        self.assertEqual((info.video_streams[0].width, info.video_streams[0].height), (1, 32768))

    def test_bad_fps_is_rejected(self):
        for value in ("1/0", "0/0", "0/1", "-1/1", "NaN", "1/x", "1/1/1", "1001/1", True, 30, "30"):
            with self.subTest(value=value):
                self.assert_error(ProbeErrorCode.INVALID_DATA, document(video(avg_frame_rate=value)))

    def test_unknown_fps_and_aspect_are_not_assumed(self):
        info = parse_probe_json(payload(document(video(avg_frame_rate="N/A", sample_aspect_ratio="0:1",
                                                       display_aspect_ratio="N/A"))))
        self.assertIsNone(info.video_streams[0].fps)
        self.assertIsNone(info.video_streams[0].sar)
        self.assertIsNone(info.video_streams[0].dar)

    def test_non_square_sar_is_preserved_without_dimension_conversion(self):
        info = parse_probe_json(payload(document(video(width=720, height=576, sample_aspect_ratio="16:15"))))
        self.assertEqual(info.video_streams[0].sar, Fraction(16, 15))
        self.assertEqual((info.video_streams[0].width, info.video_streams[0].height), (720, 576))

    def test_bad_sar_and_dar(self):
        for field in ("sample_aspect_ratio", "display_aspect_ratio"):
            for value in ("1:0", "-1:1", "NaN", True, "one", "1001:1"):
                with self.subTest(field=field, value=value):
                    self.assert_error(ProbeErrorCode.INVALID_DATA, document(video(**{field: value})))

    def test_rotation_conventions_remain_separate(self):
        info = parse_probe_json(payload(document(video(tags={"rotate": "90"}, side_data_list=[
            {"side_data_type": "Display Matrix", "rotation": -90}]))))
        rotation = info.video_streams[0].rotation
        self.assertEqual(rotation.tag_degrees, Decimal(90))
        self.assertEqual(rotation.display_matrix_degrees, Decimal(-90))

    def test_invalid_rotation(self):
        for value in (True, "NaN", "Infinity", "bad", "36001"):
            with self.subTest(value=value):
                self.assert_error(ProbeErrorCode.INVALID_DATA, document(video(tags={"rotate": value})))
        for value in (True, "90", 36001):
            with self.subTest(matrix=value):
                self.assert_error(ProbeErrorCode.INVALID_DATA, document(video(side_data_list=[
                    {"side_data_type": "Display Matrix", "rotation": value}])))

    def test_unsupported_side_data_and_duplicate_matrix(self):
        self.assert_error(ProbeErrorCode.UNSUPPORTED_METADATA, document(video(side_data_list=[
            {"side_data_type": "Unknown", "rotation": 90}])))
        self.assert_error(ProbeErrorCode.UNSUPPORTED_METADATA, document(video(side_data_list=[
            {"side_data_type": "Display Matrix", "rotation": 90},
            {"side_data_type": "Display Matrix", "rotation": 90}])))

    def test_stream_index_type_and_duplicates(self):
        for value in (True, -1, 2**31, "4", None):
            with self.subTest(value=value):
                self.assert_error(ProbeErrorCode.INVALID_DATA, document(video(index=value)))
        self.assert_error(ProbeErrorCode.INVALID_DATA, document(video(4), video(4)))

    def test_attached_pic_must_be_explicit_and_numeric(self):
        for value in (None, {}, {"attached_pic": True}, {"attached_pic": "0"}, {"attached_pic": 2}):
            with self.subTest(value=value):
                self.assert_error(ProbeErrorCode.INVALID_DATA, document(video(disposition=value)))

    def test_unknown_fields_are_not_silently_ignored(self):
        examples = [
            {**document(), "unexpected": "data"},
            document(video(unexpected="data")),
            document(video(tags={"title": "do not execute"})),
            document(video(disposition={"attached_pic": 0, "default": 1})),
            {**document(), "format": {"format_name": "avi", "filename": "private"}},
        ]
        for data in examples:
            with self.subTest(data=data):
                self.assert_error(ProbeErrorCode.UNKNOWN_FIELD, data)

    def test_invalid_structures(self):
        for data in ([], None, True, {}, {"streams": {}, "format": {}},
                     {**document(), "streams": [None]}, {**document(), "format": []},
                     document(video(tags=[])), document(video(side_data_list={})),
                     document(video(side_data_list=["bad"]))):
            with self.subTest(data=data):
                self.assert_error(ProbeErrorCode.INVALID_DATA, data)

    def test_stream_count_limit(self):
        self.assert_error(ProbeErrorCode.INVALID_DATA, document(*(video(i) for i in range(257))))

    def test_unknown_codec_type_and_invalid_codec_name(self):
        self.assert_error(ProbeErrorCode.UNSUPPORTED_METADATA, document(video(codec_type="unknown")))
        for value in (True, "bad;command", "x" * 65):
            with self.subTest(value=value):
                self.assert_error(ProbeErrorCode.INVALID_DATA, document(video(codec_name=value)))
        data = document(video())
        del data["streams"][0]["codec_name"]
        self.assertIsNone(parse_probe_json(payload(data)).video_streams[0].codec)

    def test_malformed_json_duplicate_fields_and_constants(self):
        for raw in (b"", b"{", b"\xff", b'{"streams":[],"streams":[]}', b'{"x":NaN}',
                    b"[" * 2000 + b"0" + b"]" * 2000, b'{"x":' + b"9" * 5000 + b"}"):
            with self.subTest(raw=raw[:50]), self.assertRaises(ProbeError) as caught:
                parse_probe_json(raw)
            self.assertEqual(caught.exception.code, ProbeErrorCode.MALFORMED_JSON)

    def test_json_payload_type_and_byte_limits(self):
        with self.assertRaises(ProbeError) as caught:
            parse_probe_json("not bytes")
        self.assertEqual(caught.exception.code, ProbeErrorCode.INVALID_DATA)
        for raw, truncated in ((b" " * (MAX_JSON_BYTES + 1), False), (payload(document()), True)):
            with self.subTest(truncated=truncated), self.assertRaises(ProbeError) as caught:
                parse_probe_json(raw, stdout_truncated=truncated)
            self.assertEqual(caught.exception.code, ProbeErrorCode.OUTPUT_LIMIT)

    def test_reported_format_must_match_forced_demuxer(self):
        data = document()
        data["format"]["format_name"] = "mov,mp4,m4a,3gp,3g2,mj2"
        self.assert_error(ProbeErrorCode.UNSUPPORTED_FORMAT, data)

    def test_exact_arguments_and_no_raw_metadata_requests(self):
        command = build_probe_command(self.source)
        self.assertEqual(command.executable, DEFAULT_FFPROBE)
        self.assertEqual(command.arguments, (*PROBE_OPTIONS, "-i", str(self.source)))
        for flag, value in (("-protocol_whitelist", "file"), ("-format_whitelist", "avi"), ("-f", "avi")):
            self.assertEqual(command.arguments[command.arguments.index(flag) + 1], value)
        entries = command.arguments[command.arguments.index("-show_entries") + 1]
        self.assertNotIn("filename", entries)
        self.assertIn("stream_tags=rotate", entries)
        self.popen.assert_not_called()

    def test_unsupported_extensions_never_invoke_runner(self):
        for extension in (".mov", ".mp4", ".m3u8", ".ffconcat", ".txt", ".mkv", ".bmp"):
            with self.subTest(extension=extension), self.assertRaises(ProbeError) as caught:
                probe_input(self.source.with_suffix(extension))
            self.assertEqual(caught.exception.code, ProbeErrorCode.UNSUPPORTED_FORMAT)
        self.runner.assert_not_called()

    def test_url_unc_or_invalid_input_uses_existing_validator(self):
        self.path_check.side_effect = PathValidationError("secret path")
        for value in ("https://example.invalid/a.avi", r"\\server\share\file.avi", "concat:a|b"):
            with self.subTest(value=value), self.assertRaises(ProbeError) as caught:
                probe_input(value)
            self.assertEqual(caught.exception.code, ProbeErrorCode.INVALID_INPUT)
            self.assertNotIn("secret", str(caught.exception))
        self.runner.assert_not_called()

    def test_probe_entry_point_passes_typed_command(self):
        info = probe_input(self.source)
        self.assertEqual(info.selected_stream_index, 4)
        self.assertEqual(self.runner.call_args.args[0].input_path, self.source)
        self.popen.assert_not_called()

    def test_process_failure_codes_remain_distinct_and_redacted(self):
        statuses = {
            RunStatus.FAILED: ProbeErrorCode.PROCESS_FAILED,
            RunStatus.START_FAILED: ProbeErrorCode.START_FAILED,
            RunStatus.TIMED_OUT: ProbeErrorCode.TIMED_OUT,
            RunStatus.CANCELLED: ProbeErrorCode.CANCELLED,
            RunStatus.IO_FAILED: ProbeErrorCode.IO_FAILED,
            RunStatus.CLEANUP_FAILED: ProbeErrorCode.CLEANUP_FAILED,
        }
        for status, code in statuses.items():
            self.runner.return_value = ProcessResult(status, stderr=b"secret filename")
            with self.subTest(status=status), self.assertRaises(ProbeError) as caught:
                probe_input(self.source)
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn("secret", str(caught.exception))

    def test_truncated_stdout_refuses_otherwise_valid_json(self):
        self.runner.return_value = ProcessResult(RunStatus.SUCCEEDED, 0, payload(document()), stdout_truncated=True)
        with self.assertRaises(ProbeError) as caught:
            probe_input(self.source)
        self.assertEqual(caught.exception.code, ProbeErrorCode.OUTPUT_LIMIT)

    def test_stderr_truncation_does_not_invalidate_complete_json(self):
        self.runner.return_value = ProcessResult(RunStatus.SUCCEEDED, 0, payload(document()), stderr_truncated=True)
        self.assertEqual(probe_input(self.source).selected_stream_index, 4)

    def test_runner_validation_failure_is_classified(self):
        self.runner.side_effect = CommandValidationError("private argument")
        with self.assertRaises(ProbeError) as caught:
            probe_input(self.source)
        self.assertEqual(caught.exception.code, ProbeErrorCode.INVALID_COMMAND)
        self.assertNotIn("private", str(caught.exception))

    def test_parser_does_not_change_fixture(self):
        data = document()
        original = deepcopy(data)
        parse_probe_json(payload(data))
        self.assertEqual(data, original)


if __name__ == "__main__":
    unittest.main()
