"""Bounded worker API for fixed, separately validated probe and GIF requests.

Not a general command runner. No invocation happens on import. Probe and GIF
requests have independent executable/argv validators. Call from a worker,
never from Tk's event loop. Unit tests mock ALL process creation.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass, field
from enum import Enum
import io
import math
import os
from pathlib import Path, PureWindowsPath
import subprocess
import threading
import time
from typing import TYPE_CHECKING

from .paths import PathValidationError, validate_input_path

if TYPE_CHECKING:
    from .gif_engine import FFmpegCommandPlan


DEFAULT_FFPROBE = Path(r"C:\ffmpeg\bin\ffprobe.exe")
PROBE_ENTRIES = (
    "stream=index,codec_type,codec_name,width,height,avg_frame_rate,"
    "sample_aspect_ratio,display_aspect_ratio,duration:"
    "stream_disposition=attached_pic:stream_tags=rotate:"
    "stream_side_data=side_data_type,rotation:format=format_name,duration"
)
PROBE_OPTIONS = (
    "-v", "error", "-max_alloc", "67108864", "-probesize", "5000000",
    "-analyzeduration", "5000000", "-protocol_whitelist", "file",
    "-format_whitelist", "avi", "-f", "avi", "-show_entries", PROBE_ENTRIES,
    "-of", "json",
)


class CommandValidationError(ValueError):
    """An executable, request or resource limit is outside the approved API."""


class RunStatus(Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    START_FAILED = "start_failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    IO_FAILED = "io_failed"
    CLEANUP_FAILED = "cleanup_failed"


@dataclass(frozen=True, slots=True)
class RunLimits:
    timeout: float = 30.0
    terminate_timeout: float = 2.0
    kill_timeout: float = 2.0
    drain_timeout: float = 2.0
    stdout_bytes: int = 1024 * 1024
    stderr_bytes: int = 64 * 1024

    def __post_init__(self):
        for name in ("timeout", "terminate_timeout", "kill_timeout", "drain_timeout"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 300:
                raise CommandValidationError(f"{name}: expected finite seconds in (0, 300]")
        for name in ("stdout_bytes", "stderr_bytes"):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= 8 * 1024 * 1024:
                raise CommandValidationError(f"{name}: expected integer bytes in 1..8388608")


@dataclass(frozen=True, slots=True)
class ProbeCommand:
    executable: Path = field(repr=False)
    input_path: Path = field(repr=False)
    arguments: tuple[str, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """SUCCEEDED means process exit success, never validated media completion."""

    status: RunStatus
    returncode: int | None = None
    stdout: bytes = field(default=b"", repr=False)
    stderr: bytes = field(default=b"", repr=False)
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    elapsed: float = 0.0
    error_code: str | None = None
    output_validation_required: bool = False


def _validated_arguments(command: ProbeCommand) -> list[str]:
    if type(command) is not ProbeCommand:
        # Lazy import avoids the probe -> runner -> GIF -> probe import cycle.
        from .gif_engine import FFmpegCommandPlan, GifPlanError, validate_gif_command

        if type(command) is not FFmpegCommandPlan:
            raise CommandValidationError("only typed probe or GIF commands are accepted")
        try:
            return validate_gif_command(command)
        except GifPlanError:
            raise CommandValidationError("GIF command validation failed") from None
    expected = PureWindowsPath(str(DEFAULT_FFPROBE))
    supplied = PureWindowsPath(str(command.executable))
    if not supplied.is_absolute() or str(supplied).casefold() != str(expected).casefold():
        raise CommandValidationError("only the verified absolute ffprobe executable is allowed")
    try:
        executable = validate_input_path(command.executable)
        source = validate_input_path(command.input_path)
    except PathValidationError:
        raise CommandValidationError("executable or input path validation failed") from None
    if str(PureWindowsPath(str(executable))).casefold() != str(expected).casefold():
        raise CommandValidationError("executable resolved outside the verified location")
    if source.suffix.lower() != ".avi":
        raise CommandValidationError("only forced AVI inspection is supported")
    required = (*PROBE_OPTIONS, "-i", str(source))
    if type(command.arguments) is not tuple or command.arguments != required:
        raise CommandValidationError("arguments must exactly match the fixed probe request")
    return [str(executable), *required]


def _read_chunk(stream, stop: threading.Event) -> bytes:
    # Raw Windows anonymous pipes: peek before reading so cancellation can
    # stop readers even if a descendant were to keep an inherited pipe open.
    # BytesIO is used by tests only; no real child process is spawned in tests.
    if os.name != "nt" or isinstance(stream, io.BytesIO):
        return stream.read(8192) if not stop.is_set() else b""
    import msvcrt

    handle = msvcrt.get_osfhandle(stream.fileno())
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    peek = kernel.PeekNamedPipe
    peek.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
                     ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint), ctypes.c_void_p]
    peek.restype = ctypes.c_int
    while not stop.is_set():
        available = ctypes.c_uint()
        if not peek(handle, None, 0, None, ctypes.byref(available), None):
            error = ctypes.get_last_error()
            if error in (109, 232):  # broken pipe / no data (closed)
                return b""
            raise OSError(error, "pipe inspection failed")
        if available.value:
            return os.read(stream.fileno(), min(8192, available.value))
        stop.wait(0.01)
    return b""


class _Capture:
    def __init__(self, stream, limit: int, stop: threading.Event):
        self.stream, self.limit, self.stop = stream, limit, stop
        self.data = bytearray()
        self.truncated = False
        self.failed = False

    def drain(self):
        try:
            while True:
                chunk = _read_chunk(self.stream, self.stop)
                if not chunk:
                    break
                remaining = self.limit - len(self.data)
                self.data.extend(chunk[:remaining])
                self.truncated |= len(chunk) > remaining
        except (OSError, ValueError):
            self.failed = True
        finally:
            try:
                self.stream.close()
            except OSError:
                self.failed = True


def _stop_and_reap(process, limits: RunLimits) -> tuple[bool, bool]:
    """Return (confirmed reaped, stop requested); tolerate exit/signal races."""
    try:
        already_exited = process.poll() is not None
    except OSError:
        already_exited = False
    if already_exited:
        try:
            process.wait(timeout=limits.kill_timeout)
            return True, False
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        process.terminate()
    except OSError:
        pass
    try:
        process.wait(timeout=limits.terminate_timeout)
        return True, True
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=limits.kill_timeout)
        return True, True
    except (OSError, subprocess.TimeoutExpired):
        return False, True


def run_process(command: ProbeCommand | FFmpegCommandPlan, *, limits: RunLimits = RunLimits(),
                cancel: threading.Event | None = None) -> ProcessResult:
    """Run a fixed typed request; no raw command API, logs, files or callbacks.

    Validation errors raise CommandValidationError. Operational failures return
    ProcessResult. CLEANUP_FAILED means termination or reader cleanup could not
    be confirmed and must never be treated as a successful job.
    """
    if type(limits) is not RunLimits:
        raise CommandValidationError("limits must be RunLimits")
    arguments = _validated_arguments(command)
    output_validation_required = type(command) is not ProbeCommand
    start = time.monotonic()
    if cancel is not None and cancel.is_set():
        return ProcessResult(RunStatus.CANCELLED, elapsed=time.monotonic() - start,
                             output_validation_required=output_validation_required)
    try:
        process = subprocess.Popen(arguments, shell=False, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   bufsize=0, close_fds=True,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError:
        return ProcessResult(RunStatus.START_FAILED, elapsed=time.monotonic() - start,
                             error_code="start_failed", output_validation_required=output_validation_required)

    stop = threading.Event()
    captures = [_Capture(process.stdout, limits.stdout_bytes, stop),
                _Capture(process.stderr, limits.stderr_bytes, stop)]
    readers = [threading.Thread(target=capture.drain, daemon=True,
                                name=f"panda-probe-{index}")
               for index, capture in enumerate(captures)]
    started = []
    status = RunStatus.FAILED
    cleanup_ok = True
    try:
        for reader in readers:
            reader.start()
            started.append(reader)
        while True:
            # Completion wins when completion and cancellation are observed together.
            if process.poll() is not None:
                process.wait(timeout=limits.kill_timeout)
                status = RunStatus.SUCCEEDED if process.returncode == 0 else RunStatus.FAILED
                break
            cause = (RunStatus.CANCELLED if cancel is not None and cancel.is_set()
                     else RunStatus.TIMED_OUT if time.monotonic() - start >= limits.timeout else None)
            if cause is not None:
                cleanup_ok, stopped = _stop_and_reap(process, limits)
                status = cause if stopped else (
                    RunStatus.SUCCEEDED if process.returncode == 0 else RunStatus.FAILED)
                break
            try:
                process.wait(timeout=min(0.05, max(0.001, limits.timeout - (time.monotonic() - start))))
            except subprocess.TimeoutExpired:
                pass
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        status = RunStatus.IO_FAILED
        cleanup_ok, _ = _stop_and_reap(process, limits)
    finally:
        deadline = time.monotonic() + limits.drain_timeout
        for reader in started:
            reader.join(timeout=max(0, deadline - time.monotonic()))
        if any(reader.is_alive() for reader in started):
            stop.set()
            for reader in started:
                reader.join(timeout=limits.drain_timeout)
            cleanup_ok = False  # incomplete EOF, even if the readers now stopped
        for index in range(len(started), len(captures)):
            captures[index].stream.close()
    if not cleanup_ok or any(reader.is_alive() for reader in started):
        status = RunStatus.CLEANUP_FAILED
    elif any(capture.failed for capture in captures):
        status = RunStatus.IO_FAILED
    return ProcessResult(
        status, process.returncode, bytes(captures[0].data), bytes(captures[1].data),
        captures[0].truncated, captures[1].truncated, time.monotonic() - start,
        None if status is RunStatus.SUCCEEDED else status.value,
        output_validation_required=output_validation_required,
    )
