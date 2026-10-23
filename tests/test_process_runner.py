"""All children and pipes are synthetic; no executable is ever started."""

from dataclasses import replace
import io
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from panda_media_encoder.process_runner import (
    DEFAULT_FFPROBE, PROBE_OPTIONS, CommandValidationError, ProbeCommand,
    ProcessResult, RunLimits, RunStatus, run_process,
    _read_chunk,
)
from panda_media_encoder.paths import PathValidationError


class TrackingPipe(io.BytesIO):
    def __init__(self, content):
        super().__init__(content)
        self.bytes_read = 0

    def read(self, count=-1):
        data = super().read(count)
        self.bytes_read += len(data)
        return data


class FakeProcess:
    def __init__(self, *, code=0, blocked=False, resist=False, terminate_error=False,
                 kill_error=False, never_exit=False, stdout=b"{}", stderr=b""):
        self.stdout, self.stderr = TrackingPipe(stdout), TrackingPipe(stderr)
        self.returncode = None
        self.code, self.blocked, self.resist = code, blocked, resist
        self.terminate_error, self.kill_error, self.never_exit = terminate_error, kill_error, never_exit
        self.signalled = self.killed = False
        self.poll = Mock(side_effect=lambda: self.returncode)
        self.wait = Mock(side_effect=self._wait)
        self.terminate = Mock(side_effect=self._terminate)
        self.kill = Mock(side_effect=self._kill)

    def _wait(self, timeout=None):
        if self.returncode is not None:
            return self.returncode
        if not self.never_exit and (not self.blocked or self.killed or (self.signalled and not self.resist)):
            self.returncode = -9 if self.killed else -15 if self.signalled else self.code
            return self.returncode
        raise subprocess.TimeoutExpired("redacted", timeout)

    def _terminate(self):
        if self.terminate_error:
            raise OSError("private error must not escape")
        self.signalled = True

    def _kill(self):
        if self.kill_error:
            raise OSError("private error must not escape")
        self.killed = True


class RunnerTests(unittest.TestCase):
    def setUp(self):
        # Guard every test, even tests intended to fail before Popen.
        self.popen = self.enterContext(patch("panda_media_encoder.process_runner.subprocess.Popen"))
        self.path_check = self.enterContext(patch(
            "panda_media_encoder.process_runner.validate_input_path", side_effect=lambda path: Path(path)))
        self.source = Path(r"C:\synthetic\dummy.avi")
        self.command = ProbeCommand(DEFAULT_FFPROBE, self.source, (*PROBE_OPTIONS, "-i", str(self.source)))
        self.limits = RunLimits(timeout=0.005, terminate_timeout=0.005,
                                kill_timeout=0.005, drain_timeout=0.1)

    def execute(self, process=None, **kwargs):
        process = process or FakeProcess()
        self.popen.return_value = process
        result = run_process(self.command, limits=self.limits, **kwargs)
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)
        self.assertFalse(any(thread.name.startswith("panda-probe-") for thread in threading.enumerate()))
        return result

    def test_normal_exit_and_exact_safe_popen_contract(self):
        result = self.execute()
        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        self.assertEqual(result.returncode, 0)
        args, kwargs = self.popen.call_args
        self.assertIs(type(args[0]), list)
        self.assertEqual(args[0], [str(DEFAULT_FFPROBE), *self.command.arguments])
        self.assertIs(kwargs["shell"], False)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual((kwargs["stdout"], kwargs["stderr"]), (subprocess.PIPE, subprocess.PIPE))
        self.assertEqual(kwargs["bufsize"], 0)
        self.assertTrue(kwargs["close_fds"])

    def test_nonzero_exit_is_not_success(self):
        result = self.execute(FakeProcess(code=7))
        self.assertEqual((result.status, result.returncode), (RunStatus.FAILED, 7))

    def test_start_failure_is_redacted(self):
        self.popen.side_effect = OSError("secret filename and environment")
        result = run_process(self.command)
        self.assertEqual(result.status, RunStatus.START_FAILED)
        self.assertNotIn("secret", repr(result))

    def test_rejects_command_strings_and_raw_lists(self):
        for value in ("ffprobe -i file", ["ffprobe"], None):
            with self.subTest(value=value), self.assertRaises(CommandValidationError):
                run_process(value)
        self.popen.assert_not_called()

    def test_rejects_path_search_replacement_and_scripts(self):
        for value in ("ffprobe.exe", r"C:\other\ffprobe.exe", r"C:\ffmpeg\bin\ffmpeg.exe",
                      r"C:\ffmpeg\bin\script.py", r"\\server\share\ffprobe.exe"):
            with self.subTest(value=value), self.assertRaises(CommandValidationError):
                run_process(replace(self.command, executable=Path(value)))
        self.popen.assert_not_called()

    def test_executable_resolution_cannot_change_verified_location(self):
        self.path_check.side_effect = [Path(r"C:\other\ffprobe.exe"), self.source]
        with self.assertRaises(CommandValidationError):
            run_process(self.command)
        self.popen.assert_not_called()

    def test_path_validation_failure_does_not_start(self):
        self.path_check.side_effect = PathValidationError("private path")
        with self.assertRaisesRegex(CommandValidationError, "validation failed"):
            run_process(self.command)
        self.popen.assert_not_called()

    def test_rejects_argument_injection_and_format_switch(self):
        for arguments in (self.command.arguments + ("-show_data",),
                          ("-i", "https://example.invalid"), list(self.command.arguments)):
            with self.subTest(arguments=arguments), self.assertRaises(CommandValidationError):
                run_process(replace(self.command, arguments=arguments))
        with self.assertRaises(CommandValidationError):
            run_process(replace(self.command, input_path=Path(r"C:\synthetic\file.mov")))
        self.popen.assert_not_called()

    def test_timeout_terminates_and_reaps(self):
        process, cancel = FakeProcess(blocked=True), threading.Event()
        clock = SimpleNamespace(monotonic=Mock(return_value=0.0))
        original_wait = process._wait

        def poll():
            # Expire the deadline before cancellation is observable, without
            # relying on elapsed wall time or reader-thread scheduling.
            clock.monotonic.return_value = self.limits.timeout + 1.0
            return process.returncode

        def wait(timeout=None):
            self.assertTrue(process.signalled, "timeout must request termination before reaping")
            self.assertFalse(cancel.is_set())
            cancel.set()  # A later cancellation must not relabel the timeout.
            return original_wait(timeout)

        process.poll.side_effect = poll
        process.wait.side_effect = wait
        # Replace only the runner's time reference, not the stdlib time module
        # used by threading for actual bounded joins.
        with patch("panda_media_encoder.process_runner.time", clock):
            result = self.execute(process, cancel=cancel)
        self.assertEqual(result.status, RunStatus.TIMED_OUT)
        self.assertTrue(cancel.is_set())
        process.terminate.assert_called_once()
        process.kill.assert_not_called()
        process.wait.assert_called_once_with(timeout=self.limits.terminate_timeout)
        self.assertEqual(process.returncode, -15)

    def test_timeout_escalates_to_kill_only_when_needed(self):
        process = FakeProcess(blocked=True, resist=True)
        self.assertEqual(self.execute(process).status, RunStatus.TIMED_OUT)
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        self.assertEqual(process.returncode, -9)

    def test_terminate_failure_still_kills_and_reaps(self):
        process = FakeProcess(blocked=True, terminate_error=True)
        self.assertEqual(self.execute(process).status, RunStatus.TIMED_OUT)
        process.kill.assert_called_once()
        self.assertIsNotNone(process.returncode)

    def test_kill_failure_is_cleanup_failure(self):
        process = FakeProcess(blocked=True, resist=True, kill_error=True, never_exit=True)
        self.assertEqual(self.execute(process).status, RunStatus.CLEANUP_FAILED)

    def test_pre_cancel_does_not_spawn(self):
        cancel = threading.Event()
        cancel.set()
        self.assertEqual(run_process(self.command, cancel=cancel).status, RunStatus.CANCELLED)
        self.popen.assert_not_called()

    def test_cancel_during_execution(self):
        process, cancel = FakeProcess(blocked=True), threading.Event()
        clock = SimpleNamespace(monotonic=Mock(return_value=0.0))
        original_wait = process._wait

        def wait(timeout=None):
            if not process.signalled:
                # Raise a test failure rather than loop forever if the runner
                # incorrectly ignores cancellation while the clock is frozen.
                self.assertFalse(cancel.is_set(), "cancellation must be observed before waiting again")
                cancel.set()
            else:
                self.assertTrue(cancel.is_set())
            return original_wait(timeout)

        process.wait.side_effect = wait
        # No wall-clock time, including child/reader startup, can expire the
        # deadline before the controlled wait sets the cancellation event.
        with patch("panda_media_encoder.process_runner.time", clock):
            result = self.execute(process, cancel=cancel)
        self.assertEqual(result.status, RunStatus.CANCELLED)
        self.assertTrue(cancel.is_set())
        process.terminate.assert_called_once()
        process.kill.assert_not_called()
        self.assertEqual(process.wait.call_count, 2)
        self.assertEqual(process.wait.call_args.kwargs, {"timeout": self.limits.terminate_timeout})
        self.assertEqual(process.returncode, -15)

    def test_completion_wins_over_concurrent_cancel(self):
        process = FakeProcess()
        process.returncode = 0
        cancel = Mock()
        cancel.is_set.side_effect = [False, True]
        self.assertEqual(self.execute(process, cancel=cancel).status, RunStatus.SUCCEEDED)
        process.terminate.assert_not_called()

    def test_exit_race_just_before_terminate(self):
        process = FakeProcess()
        process.returncode = 0
        process.poll.side_effect = [None, 0]
        cancel = Mock()
        cancel.is_set.side_effect = [False, True]
        self.assertEqual(self.execute(process, cancel=cancel).status, RunStatus.SUCCEEDED)
        process.terminate.assert_not_called()

    def test_both_large_streams_are_fully_drained_but_bounded(self):
        process = FakeProcess(stdout=b"a" * 40000, stderr=b"b" * 50000)
        self.popen.return_value = process
        limits = replace(self.limits, stdout_bytes=100, stderr_bytes=73)
        # This tests drain/capture limits, not elapsed time. Reader startup
        # must not accidentally expire the unrelated 5ms fixture deadline.
        clock = SimpleNamespace(monotonic=Mock(return_value=0.0))
        with patch("panda_media_encoder.process_runner.time", clock):
            result = run_process(self.command, limits=limits)
        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        self.assertEqual((result.stdout, result.stderr), (b"a" * 100, b"b" * 73))
        self.assertTrue(result.stdout_truncated and result.stderr_truncated)
        self.assertEqual((process.stdout.bytes_read, process.stderr.bytes_read), (40000, 50000))
        self.assertTrue(process.stdout.closed and process.stderr.closed)

    def test_exact_capture_limit_is_not_truncated(self):
        self.popen.return_value = FakeProcess(stdout=b"a" * 100)
        result = run_process(self.command, limits=replace(self.limits, stdout_bytes=100))
        self.assertFalse(result.stdout_truncated)

    def test_read_failure_is_reported_and_pipes_closed(self):
        with patch("panda_media_encoder.process_runner._read_chunk", side_effect=OSError("private data")):
            self.assertEqual(self.execute().status, RunStatus.IO_FAILED)

    def test_wait_os_error_attempts_kill_and_reports_unconfirmed_reaping(self):
        process = FakeProcess(blocked=True)
        process.wait.side_effect = OSError("private handle error")
        self.assertEqual(self.execute(process).status, RunStatus.CLEANUP_FAILED)
        process.terminate.assert_called_once()
        process.kill.assert_called_once()

    def test_reader_that_never_reaches_eof_is_stopped_and_closed(self):
        process = FakeProcess()
        self.limits = replace(self.limits, drain_timeout=0.005)

        def held_pipe(_stream, stop):
            stop.wait(0.5)
            return b""

        with patch("panda_media_encoder.process_runner._read_chunk", side_effect=held_pipe):
            self.assertEqual(self.execute(process).status, RunStatus.CLEANUP_FAILED)

    def native_pipe(self, peek, *, error=0):
        self.enterContext(patch("panda_media_encoder.process_runner.os.name", "nt"))
        self.enterContext(patch.dict(sys.modules, {
            "msvcrt": SimpleNamespace(get_osfhandle=Mock(return_value=123)),
        }))
        self.enterContext(patch("panda_media_encoder.process_runner.ctypes.WinDLL", create=True,
                                return_value=SimpleNamespace(PeekNamedPipe=peek)))
        self.enterContext(patch("panda_media_encoder.process_runner.ctypes.get_last_error",
                                create=True, return_value=error))
        return Mock(fileno=Mock(return_value=44))

    def test_windows_pipe_reads_only_available_bytes(self):
        def available(_handle, _buffer, _size, _read, count, _left):
            count._obj.value = 4
            return 1

        stream = self.native_pipe(Mock(side_effect=available))
        with patch("panda_media_encoder.process_runner.os.read", return_value=b"data") as read:
            self.assertEqual(_read_chunk(stream, threading.Event()), b"data")
            read.assert_called_once_with(44, 4)

    def test_windows_pipe_eof_does_not_read(self):
        stream = self.native_pipe(Mock(return_value=0), error=109)
        with patch("panda_media_encoder.process_runner.os.read") as read:
            self.assertEqual(_read_chunk(stream, threading.Event()), b"")
            read.assert_not_called()

    def test_windows_pipe_unknown_error_is_not_eof(self):
        stream = self.native_pipe(Mock(return_value=0), error=5)
        with self.assertRaises(OSError):
            _read_chunk(stream, threading.Event())

    def test_windows_pipe_idle_poll_can_be_cancelled(self):
        stream = self.native_pipe(Mock(return_value=1))  # zero available bytes
        stop = Mock()
        stop.is_set.side_effect = [False, True]
        with patch("panda_media_encoder.process_runner.os.read") as read:
            self.assertEqual(_read_chunk(stream, stop), b"")
            stop.wait.assert_called_once_with(0.01)
            read.assert_not_called()

    def test_reader_start_failure_reaps_and_closes_unstarted_pipes(self):
        process = FakeProcess(blocked=True)
        with patch("panda_media_encoder.process_runner.threading.Thread.start", side_effect=RuntimeError):
            self.assertEqual(self.execute(process).status, RunStatus.IO_FAILED)
        process.terminate.assert_called_once()

    def test_raw_output_is_not_in_result_repr(self):
        result = ProcessResult(RunStatus.FAILED, stderr=b"private absolute path", stdout=b"secret")
        self.assertNotIn("private", repr(result))
        self.assertNotIn("secret", repr(result))
        self.assertNotIn("synthetic", repr(self.command))

    def test_limits_reject_bool_nan_zero_excess_and_wrong_types(self):
        for field, values in {
            "timeout": (True, 0, -1, 301, float("nan"), float("inf"), "30"),
            "terminate_timeout": (False, 0), "kill_timeout": (0,), "drain_timeout": (0,),
            "stdout_bytes": (True, 0, -1, 8388609, 1.5), "stderr_bytes": (False, 0),
        }.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(CommandValidationError):
                    RunLimits(**{field: value})
        with self.assertRaises(CommandValidationError):
            run_process(self.command, limits={})
        self.popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
