"""Pure geometry and virtual-filesystem tests: no files/media/processes created."""

from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
import errno
from fractions import Fraction
import io
import os
from pathlib import Path
import stat
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from panda_media_encoder.gif_engine import (
    DEFAULT_FFMPEG, FFmpegCommandPlan, GifPass, GifPlanError, GifPlanErrorCode,
    build_gif_plan, calculate_gif_geometry, validate_gif_command,
)
from panda_media_encoder.models import GifPreset, PaletteSettings
from panda_media_encoder.probe import ProbeInfo, RotationInfo, VideoStreamInfo, parse_probe_json
from panda_media_encoder.process_runner import CommandValidationError, RunStatus, run_process


def preset():
    return GifPreset(1, "gif_review", "GIF確認用", "gif", 640, 640, 10,
                     True, False, PaletteSettings("full", "sierra2_4a"), 0)


def probe(width=1920, height=1080, index=2, **changes):
    stream = VideoStreamInfo(index, "mpeg4", width, height, Fraction(24), Fraction(1),
                             None, RotationInfo(Decimal(0), None), Decimal(2), False)
    stream = replace(stream, **changes)
    return ProbeInfo("avi", Decimal(2), (stream,), index)


class GifEngineTests(unittest.TestCase):
    def setUp(self):
        self.settings = preset()
        self.info = probe()
        self.root = Path(r"C:\synthetic\repository")
        self.temp = self.root / "lab" / "temp"
        self.job = self.temp / "job_001"
        self.source = Path(r"C:\synthetic\input.avi")
        self.nodes = {}
        self.links = set()
        self.add_node(self.temp, directory=True)
        self.add_node(self.source)
        self.add_node(DEFAULT_FFMPEG)
        # Existing path validation is exercised, with ALL filesystem metadata
        # supplied by a virtual map. No real synthetic directory is necessary.
        self.enterContext(patch.object(Path, "stat", lambda path, **kwargs: self.fake_stat(path, **kwargs)))
        self.enterContext(patch.object(Path, "lstat", lambda path: self.fake_lstat(path)))
        self.enterContext(patch.object(Path, "resolve", lambda path, strict=False: self.fake_resolve(path, strict)))
        self.enterContext(patch.object(Path, "iterdir", lambda path: self.fake_iterdir(path)))
        self.enterContext(patch("panda_media_encoder.paths._is_remote_drive", return_value=False))
        self.guards = []
        for target in (
            "subprocess.Popen", "subprocess.run", "subprocess.call", "os.system",
            "socket.socket", "builtins.open", "pathlib.Path.open", "pathlib.Path.mkdir",
            "pathlib.Path.touch", "pathlib.Path.write_bytes", "pathlib.Path.write_text",
            "pathlib.Path.rename", "pathlib.Path.replace", "pathlib.Path.unlink",
            "os.mkdir", "os.remove", "os.unlink", "os.rename", "os.replace",
        ):
            self.guards.append(self.enterContext(patch(target, side_effect=AssertionError(
                f"forbidden external access or mutation: {target}"))))

    def add_node(self, path, *, directory=False):
        path = Path(path)
        self.nodes[path] = stat.S_IFDIR if directory else stat.S_IFREG
        for parent in path.parents:
            self.nodes.setdefault(parent, stat.S_IFDIR)

    def fake_stat(self, path, **_kwargs):
        path = Path(path)
        if path not in self.nodes:
            raise FileNotFoundError(errno.ENOENT, "virtual path does not exist")
        return SimpleNamespace(st_mode=self.nodes[path], st_file_attributes=0,
                               st_dev=1, st_ino=hash(str(path).casefold()))

    def fake_lstat(self, path):
        if path in self.links:
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        return self.fake_stat(path)

    def fake_resolve(self, path, strict=False):
        absolute = Path(os.path.abspath(path))
        if strict:
            self.fake_stat(absolute)
        return absolute

    def fake_iterdir(self, path):
        self.fake_stat(path)
        return iter(child for child in self.nodes if child != path and child.parent == path)

    def plan(self, **changes):
        arguments = dict(input_path=self.source, probe=self.info, preset=self.settings,
                         repository_root=self.root, job_id="job_001")
        arguments.update(changes)
        return build_gif_plan(**arguments)

    def assert_code(self, code, callback):
        with self.assertRaises(GifPlanError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_representative_dimensions(self):
        for source, expected in (
            ((1920, 1080), (640, 360)), ((1080, 1920), (360, 640)),
            ((320, 240), (320, 240)), ((1000, 1000), (640, 640)),
            ((640, 640), (640, 640)), ((1, 1), (1, 1)),
            ((32768, 1), (640, 1)), ((1, 32768), (1, 640)),
        ):
            with self.subTest(source=source):
                geometry = calculate_gif_geometry(probe(*source), self.settings)
                self.assertEqual((geometry.width, geometry.height), expected)

    def test_rounding_is_floor_and_each_axis_at_least_one(self):
        geometry = calculate_gif_geometry(probe(1001, 500), self.settings)
        self.assertEqual((geometry.width, geometry.height), (640, 319))
        self.assertEqual(calculate_gif_geometry(probe(32768, 1), self.settings).height, 1)

    def test_bounds_aspect_and_no_upscaling_across_dimension_grid(self):
        sizes = (1, 2, 239, 240, 639, 640, 641, 1080, 1920, 32768)
        for width in sizes:
            for height in sizes:
                with self.subTest(width=width, height=height):
                    result = calculate_gif_geometry(probe(width, height), self.settings)
                    self.assertIs(type(result.width), int)
                    self.assertIs(type(result.height), int)
                    self.assertTrue(1 <= result.width <= min(width, 640))
                    self.assertTrue(1 <= result.height <= min(height, 640))
                    self.assertLessEqual(abs(result.width * height - result.height * width), max(width, height))

    def test_preset_bounds_smaller_than_640_are_respected(self):
        settings = replace(self.settings, max_width=320, max_height=200)
        result = calculate_gif_geometry(self.info, settings)
        self.assertEqual((result.width, result.height), (320, 180))

    def test_fps_comes_only_from_validated_preset(self):
        info = probe(fps=Fraction(120))
        self.assertEqual(calculate_gif_geometry(info, self.settings).fps, 10)
        self.assertEqual(calculate_gif_geometry(info, replace(self.settings, fps=5)).fps, 5)
        self.assertEqual(calculate_gif_geometry(probe(fps=None), self.settings).fps, 10)

    def test_geometry_is_pure_without_metadata_lookups(self):
        with patch.object(Path, "stat", side_effect=AssertionError("not pure")), \
                patch.object(Path, "lstat", side_effect=AssertionError("not pure")), \
                patch.object(Path, "resolve", side_effect=AssertionError("not pure")):
            self.assertEqual(calculate_gif_geometry(self.info, self.settings).width, 640)

    def test_invalid_dimensions(self):
        for field in ("width", "height"):
            for value in (0, -1, True, 1.0, "1920", None, 32769):
                with self.subTest(field=field, value=value):
                    self.assert_code(GifPlanErrorCode.INVALID_DIMENSIONS,
                                     lambda: calculate_gif_geometry(probe(**{field: value}), self.settings))

    def test_unknown_invalid_and_nonsquare_sar(self):
        for value in (None, True, 1, "1:1", Fraction(0), Fraction(-1), Fraction(16, 15)):
            with self.subTest(value=value):
                self.assert_code(GifPlanErrorCode.UNSUPPORTED_SAR,
                                 lambda: calculate_gif_geometry(probe(sar=value), self.settings))

    def test_dar_must_not_contradict_square_pixels(self):
        self.assertEqual(calculate_gif_geometry(probe(dar=Fraction(16, 9)), self.settings).width, 640)
        for value in (True, "16:9", Fraction(4, 3)):
            with self.subTest(value=value):
                self.assert_code(GifPlanErrorCode.INVALID_PROBE,
                                 lambda: calculate_gif_geometry(probe(dar=value), self.settings))

    def test_absent_rotation_is_unknown_not_zero(self):
        for value in (None, RotationInfo()):
            with self.subTest(value=value):
                self.assert_code(GifPlanErrorCode.UNKNOWN_ROTATION,
                                 lambda: calculate_gif_geometry(probe(rotation=value), self.settings))

    def test_explicit_zero_tag_matrix_or_both_are_accepted(self):
        for rotation in (RotationInfo(Decimal(0)), RotationInfo(None, Decimal(0)),
                         RotationInfo(Decimal(0), Decimal("-0"))):
            with self.subTest(rotation=rotation):
                self.assertEqual(calculate_gif_geometry(probe(rotation=rotation), self.settings).width, 640)

    def test_nonzero_and_conflicting_rotation(self):
        for value in (Decimal(90), Decimal(-90), Decimal(180), Decimal(360)):
            with self.subTest(value=value):
                self.assert_code(GifPlanErrorCode.UNSUPPORTED_ROTATION,
                                 lambda: calculate_gif_geometry(probe(rotation=RotationInfo(value)), self.settings))
        self.assert_code(GifPlanErrorCode.CONFLICTING_ROTATION,
                         lambda: calculate_gif_geometry(probe(rotation=RotationInfo(Decimal(0), Decimal(90))),
                                                        self.settings))

    def test_invalid_rotation_values_are_not_coerced(self):
        for value in (True, 0, 0.0, "0", Decimal("NaN"), Decimal("Infinity")):
            with self.subTest(value=value):
                self.assert_code(GifPlanErrorCode.UNKNOWN_ROTATION,
                                 lambda: calculate_gif_geometry(probe(rotation=RotationInfo(value)), self.settings))

    def test_known_resolution_changes_are_rejected(self):
        self.assert_code(GifPlanErrorCode.VARIABLE_RESOLUTION,
                         lambda: self.plan(known_variable_resolution=True))
        for value in (1, "false", None):
            with self.subTest(value=value):
                self.assert_code(GifPlanErrorCode.INVALID_PROBE,
                                 lambda: self.plan(known_variable_resolution=value))

    def test_invalid_preset_values_are_revalidated(self):
        examples = (
            {"fps": True}, {"fps": 11}, {"fps": "10"}, {"fps": 0},
            {"max_width": 641}, {"max_height": 0}, {"max_width": False},
            {"keep_aspect_ratio": False}, {"allow_upscale": True}, {"loop": True},
            {"loop": 65536}, {"schema_version": 2}, {"format": "mp4"},
            {"id": "bad;filter"}, {"palette": PaletteSettings("diff", "sierra2_4a")},
            {"palette": PaletteSettings("full", "bayer")}, {"palette": None},
        )
        for changes in examples:
            with self.subTest(changes=changes):
                self.assert_code(GifPlanErrorCode.INVALID_PRESET,
                                 lambda: calculate_gif_geometry(self.info, replace(self.settings, **changes)))
        self.assert_code(GifPlanErrorCode.INVALID_PRESET,
                         lambda: calculate_gif_geometry(self.info, {}))

    def test_unknown_probe_structure_and_non_avi_format(self):
        self.assert_code(GifPlanErrorCode.INVALID_PROBE, lambda: calculate_gif_geometry({}, self.settings))
        for name in ("mov", "mp4", "AVI", None):
            with self.subTest(name=name):
                self.assert_code(GifPlanErrorCode.UNSUPPORTED_FORMAT,
                                 lambda: calculate_gif_geometry(replace(self.info, format_name=name), self.settings))

    def test_invalid_stream_selection(self):
        examples = (
            replace(self.info, selected_stream_index=True),
            replace(self.info, selected_stream_index=99),
            replace(self.info, video_streams=()),
            replace(self.info, video_streams=(self.info.video_streams[0],) * 2),
            replace(self.info, video_streams=[self.info.video_streams[0]]),
            probe(index=-1), probe(index=True), probe(attached_pic=True), probe(attached_pic=0),
        )
        for info in examples:
            with self.subTest(info=info):
                self.assert_code(GifPlanErrorCode.INVALID_STREAM,
                                 lambda: calculate_gif_geometry(info, self.settings))

    def test_exact_real_stream_index_is_used_in_both_passes(self):
        result = self.plan(probe=probe(index=7))
        for command in (result.palette_command, result.gif_command):
            graph = command.arguments[command.arguments.index("-filter_complex") + 1]
            self.assertTrue(graph.startswith("[0:7]"))
            self.assertNotIn("0:v:", graph)
        self.assertEqual(result.geometry.stream_index, 7)

    def test_audio_first_and_cover_image_do_not_change_real_index(self):
        info = parse_probe_json(b'{"streams":['
            b'{"index":0,"codec_type":"audio"},'
            b'{"index":1,"codec_type":"video","width":100,"height":100,"disposition":{"attached_pic":1}},'
            b'{"index":5,"codec_type":"video","width":1920,"height":1080,"sample_aspect_ratio":"1:1",'
            b'"tags":{"rotate":"0"},"disposition":{"attached_pic":0}}],"format":{"format_name":"avi"}}')
        result = self.plan(probe=info)
        self.assertEqual(result.geometry.stream_index, 5)
        self.assertIn("[0:5]", result.gif_command.arguments[result.gif_command.arguments.index("-filter_complex") + 1])

    def test_zero_stream_index_is_not_treated_as_missing(self):
        self.assertEqual(self.plan(probe=probe(index=0)).geometry.stream_index, 0)

    def test_two_pass_filters_have_identical_preprocessing_and_correct_order(self):
        result = self.plan()
        first = result.palette_command.arguments
        second = result.gif_command.arguments
        self.assertEqual(first[first.index("-filter_complex") + 1],
                         "[0:2]fps=10,scale=640:360:flags=lanczos,setsar=1,palettegen=stats_mode=full[palette]")
        self.assertEqual(second[second.index("-filter_complex") + 1],
                         "[0:2]fps=10,scale=640:360:flags=lanczos,setsar=1[video];"
                         "[video][1:0]paletteuse=dither=sierra2_4a[gif]")
        self.assertEqual(first[first.index("-frames:v") + 1], "1")
        self.assertEqual(first[first.index("-map") + 1], "[palette]")
        self.assertEqual(second[second.index("-map") + 1], "[gif]")

    def test_fixed_safety_options_and_argument_lists(self):
        result = self.plan()
        for command in (result.palette_command, result.gif_command):
            argv = command.as_argv()
            self.assertIs(type(argv), list)
            self.assertTrue(all(type(argument) is str for argument in argv))
            self.assertEqual(argv[0], str(DEFAULT_FFMPEG))
            for option in ("-n", "-nostdin", "-nostats", "-an", "-sn", "-dn", "-noautorotate"):
                self.assertIn(option, argv)
            self.assertNotIn("-y", argv)
            self.assertEqual(argv[argv.index("-progress") + 1], "pipe:1")
            self.assertEqual(argv[argv.index("-map_metadata") + 1], "-1")
            self.assertEqual(argv[argv.index("-map_chapters") + 1], "-1")
            self.assertEqual(argv[argv.index("-protocol_whitelist") + 1], "file")
            self.assertEqual(argv[argv.index("-format_whitelist") + 1], "avi")
            self.assertEqual(argv[argv.index("-f") + 1], "avi")

    def test_palette_input_is_a_single_fixed_png_not_a_pattern(self):
        result = self.plan()
        args = result.gif_command.arguments
        inputs = [args[index + 1] for index, value in enumerate(args) if value == "-i"]
        self.assertEqual(inputs, [str(self.source), str(result.palette_path)])
        self.assertEqual(args[args.index("-pattern_type") + 1], "none")
        formats = [args[index + 1] for index, value in enumerate(args) if value == "-format_whitelist"]
        self.assertEqual(formats, ["avi", "image2"])

    def test_loop_comes_from_preset_including_zero(self):
        for loop in (0, -1, 3, 65535):
            with self.subTest(loop=loop):
                arguments = self.plan(preset=replace(self.settings, loop=loop)).gif_command.arguments
                self.assertEqual(arguments[arguments.index("-loop") + 1], str(loop))

    def test_output_paths_are_fixed_inside_uncreated_job_directory(self):
        result = self.plan()
        self.assertEqual(result.job_directory, self.job)
        self.assertEqual(result.palette_path, self.job / "palette.png")
        self.assertEqual(result.gif_path, self.job / "animation.gif")
        self.assertEqual(result.palette_command.arguments[-1], str(result.palette_path))
        self.assertEqual(result.gif_command.arguments[-1], str(result.gif_path))
        self.assertNotIn(self.job, self.nodes)
        self.assertNotIn("lab/output", str(result.gif_path).replace("\\", "/"))

    def test_existing_job_directory_or_outputs_are_collisions(self):
        for kind in ("directory", "palette", "gif"):
            with self.subTest(kind=kind):
                saved = dict(self.nodes)
                self.add_node(self.job, directory=True)
                if kind != "directory":
                    self.add_node(self.job / ("palette.png" if kind == "palette" else "animation.gif"))
                self.assert_code(GifPlanErrorCode.OUTPUT_COLLISION, self.plan)
                self.nodes = saved

    def test_case_alias_job_collision(self):
        self.add_node(self.temp / "JOB_001", directory=True)
        self.assert_code(GifPlanErrorCode.OUTPUT_COLLISION, self.plan)

    def test_job_id_rejects_traversal_paths_filters_and_unsafe_names(self):
        for value in ("", "..", "../outside", "a/b", "a\\b", "/abs", "C:\\abs", "a;filter",
                      "a%03d", "a b", "x" * 65, "UPPER", True, None):
            with self.subTest(value=value):
                self.assert_code(GifPlanErrorCode.INVALID_JOB_ID, lambda: self.plan(job_id=value))
        self.assert_code(GifPlanErrorCode.INVALID_PATH, lambda: self.plan(job_id="con"))

    def test_input_urls_unc_ads_devices_and_nonexistent_files_are_rejected(self):
        for value in ("https://example.invalid/input.avi", "concat:a|b", r"\\host\share\input.avi",
                      r"\\?\C:\input.avi", r"C:\synthetic\input.avi:stream",
                      self.source.parent / "missing.avi", self.source.parent):
            with self.subTest(value=value):
                self.assert_code(GifPlanErrorCode.INVALID_PATH, lambda: self.plan(input_path=value))

    def test_non_avi_files_are_not_enabled(self):
        for suffix in (".mov", ".mp4", ".m3u8", ".ffconcat", ".mkv", ".png"):
            source = self.source.with_suffix(suffix)
            self.add_node(source)
            with self.subTest(suffix=suffix):
                self.assert_code(GifPlanErrorCode.UNSUPPORTED_FORMAT, lambda: self.plan(input_path=source))

    def test_input_junction_and_mapped_network_drive_are_rejected(self):
        self.links.add(self.source.parent)
        self.assert_code(GifPlanErrorCode.INVALID_PATH, self.plan)
        self.links.clear()
        with patch("panda_media_encoder.paths._is_remote_drive", side_effect=lambda path: path == self.source):
            self.assert_code(GifPlanErrorCode.INVALID_PATH, self.plan)

    def test_temp_junction_is_rejected(self):
        self.links.add(self.temp)
        self.assert_code(GifPlanErrorCode.INVALID_PATH, self.plan)

    def test_resolved_temp_escape_is_rejected(self):
        original = self.fake_resolve

        def resolve(path, strict=False):
            return self.root / "outside" if path == self.temp else original(path, strict)

        with patch.object(Path, "resolve", resolve):
            self.assert_code(GifPlanErrorCode.INVALID_PATH, self.plan)

    def test_resolved_job_or_output_escape_is_rejected(self):
        for escaped in (self.job, self.job / "palette.png", self.job / "animation.gif"):
            original = self.fake_resolve

            def resolve(path, strict=False):
                return self.root / "outside" if path == escaped else original(path, strict)

            with self.subTest(escaped=escaped), patch.object(Path, "resolve", resolve):
                self.assert_code(GifPlanErrorCode.INVALID_PATH, self.plan)

    def test_missing_temp_root_is_not_created(self):
        del self.nodes[self.temp]
        self.assert_code(GifPlanErrorCode.INVALID_PATH, self.plan)
        self.assertNotIn(self.temp, self.nodes)

    def test_network_repository_is_rejected(self):
        self.assert_code(GifPlanErrorCode.INVALID_PATH,
                         lambda: self.plan(repository_root=r"\\host\share\repository"))

    def test_untrusted_ffmpeg_path_and_missing_executable_are_rejected(self):
        for value in ("ffmpeg.exe", r"C:\other\ffmpeg.exe", r"C:\ffmpeg\bin\ffprobe.exe",
                      r"\\host\share\ffmpeg.exe", None):
            with self.subTest(value=value):
                self.assert_code(GifPlanErrorCode.INVALID_EXECUTABLE, lambda: self.plan(ffmpeg_executable=value))
        del self.nodes[DEFAULT_FFMPEG]
        self.assert_code(GifPlanErrorCode.INVALID_EXECUTABLE, self.plan)

    def test_filename_and_display_name_never_enter_filter_graph(self):
        source = self.source.parent / "unsafe;[x] fps=999.avi"
        self.add_node(source)
        result = self.plan(input_path=source, preset=replace(self.settings, name="unsafe;filter"))
        for command in (result.palette_command, result.gif_command):
            graph = command.arguments[command.arguments.index("-filter_complex") + 1]
            self.assertNotIn("unsafe", graph)
            self.assertNotIn("999", graph)
            self.assertEqual(command.arguments[command.arguments.index("-i") + 1], str(source))

    def test_plans_are_immutable_and_argv_lists_are_independent(self):
        result = self.plan()
        with self.assertRaises(FrozenInstanceError):
            result.geometry.width = 100
        first = result.palette_command.as_argv()
        first.append("-y")
        self.assertNotIn("-y", result.palette_command.as_argv())
        self.assertNotIn("synthetic", repr(result))

    def test_gif_plan_requires_prepared_job_before_runner_launch(self):
        result = self.plan()
        with self.assertRaises(CommandValidationError):
            run_process(result.palette_command)

    def test_runner_accepts_both_passes_after_revalidation_without_real_launch(self):
        result = self.plan()
        self.add_node(self.job, directory=True)
        for command in (result.palette_command, result.gif_command):
            if command.phase is GifPass.GIF:
                self.add_node(result.palette_path)
            child = Mock(returncode=0, stdout=io.BytesIO(b"progress=end\n"), stderr=io.BytesIO())
            child.poll.return_value = 0
            child.wait.return_value = 0
            clock = SimpleNamespace(monotonic=Mock(return_value=0.0))
            with self.subTest(phase=command.phase), patch("subprocess.Popen", return_value=child) as popen, \
                    patch("panda_media_encoder.process_runner.time", clock):
                process_result = run_process(command)
                self.assertEqual(process_result.status, RunStatus.SUCCEEDED)
                self.assertTrue(process_result.output_validation_required)
                self.assertEqual(popen.call_args.args[0], command.as_argv())
                self.assertFalse(popen.call_args.kwargs["shell"])
                self.assertTrue(child.stdout.closed and child.stderr.closed)
                child.wait.assert_called_once()
                child.terminate.assert_not_called()
                child.kill.assert_not_called()
        self.assertNotIn(result.gif_path, self.nodes)  # exit 0 is not artifact validation

    def test_forged_argv_or_pass_identity_is_rejected(self):
        result = self.plan()
        self.add_node(self.job, directory=True)
        self.add_node(result.palette_path)
        for command in (
            replace(result.gif_command, arguments=result.gif_command.arguments + ("-y",)),
            replace(result.gif_command, phase=GifPass.PALETTE),
            replace(result.gif_command, arguments=result.palette_command.arguments),
            replace(result.gif_command, executable=Path(r"C:\ffmpeg\bin\ffprobe.exe")),
            replace(result.gif_command, context=None),
            replace(result.gif_command, phase="gif"),
            replace(result.gif_command, arguments=list(result.gif_command.arguments)),
            FFmpegCommandPlan(DEFAULT_FFMPEG, result.gif_command.arguments),
        ):
            with self.subTest(command=command), self.assertRaises(CommandValidationError):
                run_process(command)
        for guard in self.guards:
            guard.assert_not_called()

    def test_fixed_template_rejects_output_and_input_swaps(self):
        result = self.plan()
        self.add_node(self.job, directory=True)
        self.add_node(result.palette_path)
        args = result.gif_command.arguments
        for changed in (
            args[:-1] + (str(result.palette_path),),
            tuple(str(result.gif_path) if value == str(result.palette_path) else value for value in args),
            tuple(str(result.palette_path) if value == str(self.source) else value for value in args),
            args[:-1] + (str(self.root / "lab" / "output" / "final.gif"),),
        ):
            with self.subTest(arguments=changed), self.assertRaises(CommandValidationError):
                run_process(replace(result.gif_command, arguments=changed))

    def test_execution_revalidates_context_not_just_command_type(self):
        result = self.plan()
        self.add_node(self.job, directory=True)
        context = result.palette_command.context
        for changed in (
            replace(context, preset=replace(self.settings, allow_upscale=True)),
            replace(context, probe=probe(sar=Fraction(2))),
            replace(context, probe=probe(rotation=RotationInfo())),
            replace(context, known_variable_resolution=True),
            replace(context, job_id="../outside"),
            replace(context, input_path=self.source.with_suffix(".mov")),
            replace(context, repository_root=r"\\host\share"),
        ):
            with self.subTest(context=changed), self.assertRaises(CommandValidationError):
                run_process(replace(result.palette_command, context=changed))

    def test_palette_pass_rejects_existing_outputs_or_unrelated_job_data(self):
        result = self.plan()
        self.add_node(self.job, directory=True)
        baseline = dict(self.nodes)
        for path in (result.palette_path, result.gif_path, self.job / "unrelated.dat"):
            self.nodes = dict(baseline)
            self.add_node(path)
            with self.subTest(path=path), self.assertRaises(CommandValidationError):
                run_process(result.palette_command)

    def test_gif_pass_requires_regular_palette_and_absent_output(self):
        result = self.plan()
        self.add_node(self.job, directory=True)
        with self.assertRaises(CommandValidationError):
            run_process(result.gif_command)
        self.add_node(result.palette_path, directory=True)
        with self.assertRaises(CommandValidationError):
            run_process(result.gif_command)
        self.add_node(result.palette_path)
        self.add_node(result.gif_path)
        with self.assertRaises(CommandValidationError):
            run_process(result.gif_command)

    def test_runtime_reparse_point_is_rejected_even_after_planning(self):
        result = self.plan()
        self.add_node(self.job, directory=True)
        self.links.add(self.job)
        with self.assertRaises(CommandValidationError):
            run_process(result.palette_command)

    def test_palette_hardlinked_to_source_is_rejected(self):
        result = self.plan()
        self.add_node(self.job, directory=True)
        self.add_node(result.palette_path)
        with patch.object(Path, "samefile", return_value=True):
            with self.assertRaises(CommandValidationError):
                run_process(result.gif_command)

    def test_complete_dataclass_forgery_is_still_revalidated(self):
        result = self.plan()
        forged = FFmpegCommandPlan(result.palette_command.executable,
                                   result.palette_command.arguments + ("-y",),
                                   GifPass.PALETTE, result.palette_command.context)
        self.add_node(self.job, directory=True)
        with self.assertRaises(GifPlanError):
            validate_gif_command(forged)

    def test_planning_does_not_mutate_files_or_launch_anything(self):
        before = dict(self.nodes)
        self.plan()
        self.assertEqual(self.nodes, before)
        for guard in self.guards:
            guard.assert_not_called()

    def test_unverified_conditions_are_explicit(self):
        result = self.plan()
        self.assertIn("midstream_resolution_changes_not_detected", result.unverified_conditions)
        self.assertIn("ffmpeg_4_4_1_runtime_compatibility", result.unverified_conditions)
        self.assertIn("filesystem_snapshot_not_a_reservation", result.unverified_conditions)


if __name__ == "__main__":
    unittest.main()
