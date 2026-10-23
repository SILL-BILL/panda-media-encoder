"""Strict parsing and fixed argument planning for local AVI inspection.

Only synthetic JSON / mocked process execution is verified in Phase 1B-3.
The FFmpeg 4.4.1 switches and the AVI demuxer's local-reference boundary still
require separately approved integration checks. MOV/MP4 (including drefs),
playlists, concat, and all other formats are rejected rather than relaxed.
Protocol allowlisting is not an OS sandbox and 'file' alone cannot isolate
arbitrary local-file references. Do not advertise this API as safe for hostile
media; forcing AVI narrows the parser surface but is not proof of isolation.
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from fractions import Fraction
import json
from pathlib import Path
import re
import threading

from .paths import PathValidationError, validate_input_path
from .process_runner import (
    DEFAULT_FFPROBE, PROBE_OPTIONS, CommandValidationError, ProbeCommand,
    ProcessResult, RunLimits, RunStatus, run_process,
)


MAX_JSON_BYTES = 1024 * 1024
MAX_STREAMS = 256
MAX_DIMENSION = 32768


class ProbeErrorCode(Enum):
    INVALID_INPUT = "invalid_input"
    UNSUPPORTED_FORMAT = "unsupported_format"
    INVALID_COMMAND = "invalid_command"
    MALFORMED_JSON = "malformed_json"
    UNKNOWN_FIELD = "unknown_field"
    INVALID_DATA = "invalid_data"
    UNSUPPORTED_METADATA = "unsupported_metadata"
    NO_VIDEO = "no_video"
    COVER_ONLY = "cover_only"
    OUTPUT_LIMIT = "output_limit"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    START_FAILED = "start_failed"
    PROCESS_FAILED = "process_failed"
    IO_FAILED = "io_failed"
    CLEANUP_FAILED = "cleanup_failed"


class ProbeError(ValueError):
    def __init__(self, code: ProbeErrorCode, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class RotationInfo:
    # Keep both conventions separate; do not guess the transform or its sign.
    tag_degrees: Decimal | None = None
    display_matrix_degrees: Decimal | None = None


@dataclass(frozen=True, slots=True)
class VideoStreamInfo:
    index: int
    codec: str | None
    width: int
    height: int
    fps: Fraction | None
    sar: Fraction | None
    dar: Fraction | None
    rotation: RotationInfo
    duration: Decimal | None
    attached_pic: bool


@dataclass(frozen=True, slots=True)
class ProbeInfo:
    format_name: str
    duration: Decimal | None
    video_streams: tuple[VideoStreamInfo, ...]
    selected_stream_index: int


def build_probe_command(input_path: str | Path, *, executable: Path = DEFAULT_FFPROBE) -> ProbeCommand:
    """Validate local file metadata only and build an immutable fixed request."""
    try:
        source = validate_input_path(input_path)
    except PathValidationError:
        raise ProbeError(ProbeErrorCode.INVALID_INPUT, "input is not an approved local regular file") from None
    if source.suffix.lower() != ".avi":
        raise ProbeError(ProbeErrorCode.UNSUPPORTED_FORMAT, "only forced AVI inspection is supported initially")
    return ProbeCommand(executable, source, (*PROBE_OPTIONS, "-i", str(source)))


def _object(value, allowed: set[str], label: str, required: set[str] = frozenset()) -> dict:
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise ProbeError(ProbeErrorCode.INVALID_DATA, f"{label}: expected an object")
    if value.keys() - allowed:
        raise ProbeError(ProbeErrorCode.UNKNOWN_FIELD, f"{label}: contains unrequested fields")
    if required - value.keys():
        raise ProbeError(ProbeErrorCode.INVALID_DATA, f"{label}: missing required fields")
    return value


def _integer(value, low: int, high: int, label: str) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ProbeError(ProbeErrorCode.INVALID_DATA, f"{label}: invalid integer or range")
    return value


def _duration(value) -> Decimal | None:
    if value is None or value == "N/A":
        return None
    if type(value) is not str or not re.fullmatch(r"\d{1,12}(?:\.\d{1,12})?", value):
        raise ProbeError(ProbeErrorCode.INVALID_DATA, "duration: invalid seconds")
    duration = Decimal(value)
    if duration > Decimal("10000000000"):
        raise ProbeError(ProbeErrorCode.INVALID_DATA, "duration: outside supported range")
    return duration


def _ratio(value, separator: str, label: str) -> Fraction | None:
    if value is None or value == "N/A":
        return None
    if type(value) is not str or not re.fullmatch(r"\d{1,9}" + re.escape(separator) + r"\d{1,9}", value):
        raise ProbeError(ProbeErrorCode.INVALID_DATA, f"{label}: invalid rational")
    numerator, denominator = map(int, value.split(separator))
    if denominator == 0:
        raise ProbeError(ProbeErrorCode.INVALID_DATA, f"{label}: zero denominator")
    if numerator == 0:
        if label in ("sar", "dar"):
            return None  # ffprobe's explicit unspecified-ratio representation
        raise ProbeError(ProbeErrorCode.INVALID_DATA, "fps: nonpositive frame rate")
    ratio = Fraction(numerator, denominator)
    if ratio > 1000:
        raise ProbeError(ProbeErrorCode.INVALID_DATA, f"{label}: outside supported range")
    return ratio


def _angle(value, *, tag: bool) -> Decimal:
    if (tag and type(value) is not str) or (not tag and type(value) not in (int, float)):
        raise ProbeError(ProbeErrorCode.INVALID_DATA, "rotation: invalid type")
    if len(str(value)) > 32:
        raise ProbeError(ProbeErrorCode.INVALID_DATA, "rotation: oversized value")
    try:
        angle = Decimal(str(value))
    except InvalidOperation:
        raise ProbeError(ProbeErrorCode.INVALID_DATA, "rotation: invalid degrees") from None
    if not angle.is_finite() or abs(angle) > 36000:
        raise ProbeError(ProbeErrorCode.INVALID_DATA, "rotation: invalid degrees")
    return angle


def _rotation(stream: dict) -> RotationInfo:
    tags = _object(stream.get("tags", {}), {"rotate"}, "stream.tags")
    tag = _angle(tags["rotate"], tag=True) if "rotate" in tags else None
    side_data = stream.get("side_data_list", [])
    if type(side_data) is not list or len(side_data) > 8:
        raise ProbeError(ProbeErrorCode.INVALID_DATA, "side data: expected bounded list")
    matrix = None
    for entry in side_data:
        entry = _object(entry, {"side_data_type", "rotation"}, "side data", {"side_data_type", "rotation"})
        if entry["side_data_type"] != "Display Matrix":
            raise ProbeError(ProbeErrorCode.UNSUPPORTED_METADATA, "unverified side data type")
        if matrix is not None:
            raise ProbeError(ProbeErrorCode.UNSUPPORTED_METADATA, "multiple rotation matrices")
        matrix = _angle(entry["rotation"], tag=False)
    return RotationInfo(tag, matrix)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProbeError(ProbeErrorCode.MALFORMED_JSON, "duplicate JSON field")
        result[key] = value
    return result


def _invalid_constant(_value):
    raise ProbeError(ProbeErrorCode.MALFORMED_JSON, "nonstandard JSON number")


def parse_probe_json(payload: bytes, *, stdout_truncated: bool = False) -> ProbeInfo:
    """Parse bounded synthetic/tool JSON; no filesystem or process operations."""
    if stdout_truncated or (isinstance(payload, bytes) and len(payload) > MAX_JSON_BYTES):
        raise ProbeError(ProbeErrorCode.OUTPUT_LIMIT, "ffprobe JSON exceeded its byte limit")
    if type(payload) is not bytes:
        raise ProbeError(ProbeErrorCode.INVALID_DATA, "ffprobe JSON must be bytes")
    try:
        data = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object,
                          parse_constant=_invalid_constant)
    except ProbeError:
        raise
    except (ValueError, RecursionError):
        raise ProbeError(ProbeErrorCode.MALFORMED_JSON, "ffprobe returned invalid UTF-8 JSON") from None
    data = _object(data, {"streams", "format"}, "root", {"streams", "format"})
    format_info = _object(data["format"], {"format_name", "duration"}, "format", {"format_name"})
    if format_info["format_name"] != "avi":
        raise ProbeError(ProbeErrorCode.UNSUPPORTED_FORMAT, "reported format differs from forced AVI")
    duration = _duration(format_info.get("duration"))
    streams = data["streams"]
    if type(streams) is not list or len(streams) > MAX_STREAMS:
        raise ProbeError(ProbeErrorCode.INVALID_DATA, "streams: expected bounded list")
    allowed = {"index", "codec_type", "codec_name", "width", "height", "avg_frame_rate",
               "sample_aspect_ratio", "display_aspect_ratio", "duration", "disposition",
               "tags", "side_data_list"}
    seen, videos = set(), []
    for stream in streams:
        stream = _object(stream, allowed, "stream", {"index", "codec_type"})
        index = _integer(stream["index"], 0, 2**31 - 1, "stream.index")
        if index in seen:
            raise ProbeError(ProbeErrorCode.INVALID_DATA, "duplicate stream index")
        seen.add(index)
        kind = stream["codec_type"]
        if type(kind) is not str or kind not in {"video", "audio", "subtitle", "data", "attachment"}:
            raise ProbeError(ProbeErrorCode.UNSUPPORTED_METADATA, "unknown stream type")
        codec = stream.get("codec_name")
        if codec is not None and (type(codec) is not str or not re.fullmatch(r"[a-z0-9_]{1,64}", codec)):
            raise ProbeError(ProbeErrorCode.INVALID_DATA, "codec: invalid identifier")
        stream_duration = _duration(stream.get("duration"))
        rotation = _rotation(stream)
        if "disposition" in stream:
            disposition = _object(stream["disposition"], {"attached_pic"}, "disposition", {"attached_pic"})
            _integer(disposition["attached_pic"], 0, 1, "attached_pic")
        if kind != "video":
            # Non-video streams are not candidates, but malformed requested
            # fields must not escape structural/type validation either.
            for dimension in ("width", "height"):
                if dimension in stream:
                    _integer(stream[dimension], 0, MAX_DIMENSION, dimension)
            rate = stream.get("avg_frame_rate")
            if rate != "0/0":  # ffprobe may emit this for non-video streams
                _ratio(rate, "/", "fps")
            _ratio(stream.get("sample_aspect_ratio"), ":", "sar")
            _ratio(stream.get("display_aspect_ratio"), ":", "dar")
            continue
        width = _integer(stream.get("width"), 1, MAX_DIMENSION, "width")
        height = _integer(stream.get("height"), 1, MAX_DIMENSION, "height")
        disposition = _object(stream.get("disposition"), {"attached_pic"}, "disposition", {"attached_pic"})
        attached = _integer(disposition["attached_pic"], 0, 1, "attached_pic") == 1
        videos.append(VideoStreamInfo(
            index, codec, width, height, _ratio(stream.get("avg_frame_rate"), "/", "fps"),
            _ratio(stream.get("sample_aspect_ratio"), ":", "sar"),
            _ratio(stream.get("display_aspect_ratio"), ":", "dar"), rotation,
            stream_duration, attached,
        ))
    if not videos:
        raise ProbeError(ProbeErrorCode.NO_VIDEO, "no video stream")
    selected = next((stream for stream in videos if not stream.attached_pic), None)
    if selected is None:
        raise ProbeError(ProbeErrorCode.COVER_ONLY, "only attached cover images found")
    return ProbeInfo("avi", duration, tuple(videos), selected.index)


def probe_input(input_path: str | Path, *, executable: Path = DEFAULT_FFPROBE,
                limits: RunLimits = RunLimits(), cancel: threading.Event | None = None) -> ProbeInfo:
    """Worker entry point; call only with separately authorized media access.

    Phase 1B-3 tests replace run_process; no real ffprobe invocation is approved.
    Raw stderr, paths and metadata are never included in exception messages.
    """
    command = build_probe_command(input_path, executable=executable)
    try:
        result: ProcessResult = run_process(command, limits=limits, cancel=cancel)
    except CommandValidationError:
        raise ProbeError(ProbeErrorCode.INVALID_COMMAND, "probe command validation failed") from None
    errors = {
        RunStatus.FAILED: ProbeErrorCode.PROCESS_FAILED,
        RunStatus.START_FAILED: ProbeErrorCode.START_FAILED,
        RunStatus.TIMED_OUT: ProbeErrorCode.TIMED_OUT,
        RunStatus.CANCELLED: ProbeErrorCode.CANCELLED,
        RunStatus.IO_FAILED: ProbeErrorCode.IO_FAILED,
        RunStatus.CLEANUP_FAILED: ProbeErrorCode.CLEANUP_FAILED,
    }
    if result.status is not RunStatus.SUCCEEDED:
        code = errors.get(result.status, ProbeErrorCode.PROCESS_FAILED)
        raise ProbeError(code, f"ffprobe did not complete: {code.value}")
    return parse_probe_json(result.stdout, stdout_truncated=result.stdout_truncated)
