"""Deterministic GIF geometry and two-pass command planning, WITHOUT execution.

Only local filesystem metadata is inspected when building a plan. No media is
opened, probed, transformed, reserved, moved or deleted. A plan is a snapshot:
later execution must revalidate paths and reserve its own unique job directory.
Existing process_runner intentionally cannot execute these command plans.
"""

from dataclasses import dataclass, field, fields
from decimal import Decimal
from enum import Enum
from fractions import Fraction
from pathlib import Path, PureWindowsPath
import re

from .models import GifPreset, PaletteSettings
from .paths import PathValidationError, _checked_path, validate_input_path
from .presets import PresetValidationError, parse_gif_preset
from .probe import MAX_DIMENSION, MAX_STREAMS, ProbeInfo, RotationInfo, VideoStreamInfo


DEFAULT_FFMPEG = Path(r"C:\ffmpeg\bin\ffmpeg.exe")
_INPUT_OPTIONS = (
    "-max_alloc", "67108864", "-probesize", "5000000", "-analyzeduration", "5000000",
    "-protocol_whitelist", "file", "-format_whitelist", "avi", "-f", "avi", "-noautorotate",
)
_GLOBAL_OPTIONS = ("-hide_banner", "-loglevel", "error", "-nostdin", "-n",
                   "-progress", "pipe:1", "-nostats")
_OUTPUT_OPTIONS = ("-an", "-sn", "-dn", "-map_metadata", "-1", "-map_chapters", "-1")
_UNVERIFIED = (
    "ffmpeg_4_4_1_runtime_compatibility",
    "avi_external_reference_boundary",
    "midstream_resolution_changes_not_detected",
    "filesystem_snapshot_not_a_reservation",
)


class GifPlanErrorCode(Enum):
    INVALID_PRESET = "invalid_preset"
    INVALID_PROBE = "invalid_probe"
    UNSUPPORTED_FORMAT = "unsupported_format"
    INVALID_DIMENSIONS = "invalid_dimensions"
    INVALID_STREAM = "invalid_stream"
    UNSUPPORTED_SAR = "unsupported_sar"
    UNKNOWN_ROTATION = "unknown_rotation"
    CONFLICTING_ROTATION = "conflicting_rotation"
    UNSUPPORTED_ROTATION = "unsupported_rotation"
    VARIABLE_RESOLUTION = "variable_resolution"
    INVALID_PATH = "invalid_path"
    INVALID_JOB_ID = "invalid_job_id"
    INVALID_EXECUTABLE = "invalid_executable"
    OUTPUT_COLLISION = "output_collision"


class GifPlanError(ValueError):
    def __init__(self, code: GifPlanErrorCode, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class GifGeometry:
    width: int
    height: int
    fps: int
    stream_index: int


@dataclass(frozen=True, slots=True)
class FFmpegCommandPlan:
    """Immutable plan; as_argv returns a fresh list, never a shell string."""

    executable: Path = field(repr=False)
    arguments: tuple[str, ...] = field(repr=False)

    def as_argv(self) -> list[str]:
        return [str(self.executable), *self.arguments]


@dataclass(frozen=True, slots=True)
class GifPlan:
    geometry: GifGeometry
    palette_command: FFmpegCommandPlan
    gif_command: FFmpegCommandPlan
    job_directory: Path = field(repr=False)
    palette_path: Path = field(repr=False)
    gif_path: Path = field(repr=False)
    unverified_conditions: tuple[str, ...] = _UNVERIFIED


def _validated_preset(preset: GifPreset) -> GifPreset:
    if type(preset) is not GifPreset or type(preset.palette) is not PaletteSettings:
        raise GifPlanError(GifPlanErrorCode.INVALID_PRESET, "expected GIF preset and palette settings")
    # Do not use asdict/deepcopy: invalid field objects must not execute hooks.
    data = {item.name: getattr(preset, item.name) for item in fields(GifPreset)}
    data["palette"] = {"stats_mode": preset.palette.stats_mode, "dither": preset.palette.dither}
    try:
        return parse_gif_preset(data)
    except PresetValidationError:
        raise GifPlanError(GifPlanErrorCode.INVALID_PRESET, "GIF preset is invalid or unsupported") from None


def _stream(probe: ProbeInfo, known_variable_resolution: bool) -> VideoStreamInfo:
    if type(known_variable_resolution) is not bool:
        raise GifPlanError(GifPlanErrorCode.INVALID_PROBE, "resolution-change flag must be boolean")
    if known_variable_resolution:
        raise GifPlanError(GifPlanErrorCode.VARIABLE_RESOLUTION, "known changing-resolution input is unsupported")
    if type(probe) is not ProbeInfo:
        raise GifPlanError(GifPlanErrorCode.INVALID_PROBE, "expected ProbeInfo")
    if type(probe.format_name) is not str or probe.format_name != "avi":
        raise GifPlanError(GifPlanErrorCode.UNSUPPORTED_FORMAT, "only AVI input is supported")
    if (type(probe.video_streams) is not tuple or not 1 <= len(probe.video_streams) <= MAX_STREAMS
            or type(probe.selected_stream_index) is not int):
        raise GifPlanError(GifPlanErrorCode.INVALID_STREAM, "invalid video stream selection")
    indexes = set()
    for stream in probe.video_streams:
        if (type(stream) is not VideoStreamInfo or type(stream.index) is not int
                or not 0 <= stream.index <= 2**31 - 1 or stream.index in indexes
                or type(stream.attached_pic) is not bool):
            raise GifPlanError(GifPlanErrorCode.INVALID_STREAM, "invalid or duplicate stream index")
        indexes.add(stream.index)
    selected = next((stream for stream in probe.video_streams
                     if stream.index == probe.selected_stream_index), None)
    if selected is None or selected.attached_pic:
        raise GifPlanError(GifPlanErrorCode.INVALID_STREAM, "selected stream is missing or is a cover image")
    for size in (selected.width, selected.height):
        if type(size) is not int or not 1 <= size <= MAX_DIMENSION:
            raise GifPlanError(GifPlanErrorCode.INVALID_DIMENSIONS, "input dimensions must be supported positive integers")
    if type(selected.sar) is not Fraction or selected.sar != Fraction(1):
        raise GifPlanError(GifPlanErrorCode.UNSUPPORTED_SAR, "explicit square-pixel SAR=1 is required")
    if selected.dar is not None:
        if type(selected.dar) is not Fraction or selected.dar != Fraction(selected.width, selected.height):
            raise GifPlanError(GifPlanErrorCode.INVALID_PROBE, "DAR conflicts with square-pixel dimensions")
    if type(selected.rotation) is not RotationInfo:
        raise GifPlanError(GifPlanErrorCode.UNKNOWN_ROTATION, "rotation information is invalid or unknown")
    angles = (selected.rotation.tag_degrees, selected.rotation.display_matrix_degrees)
    known_angles = [angle for angle in angles if angle is not None]
    if not known_angles or any(type(angle) is not Decimal or not angle.is_finite() for angle in known_angles):
        raise GifPlanError(GifPlanErrorCode.UNKNOWN_ROTATION, "at least one explicit finite zero rotation is required")
    if len(known_angles) == 2 and known_angles[0] != known_angles[1]:
        raise GifPlanError(GifPlanErrorCode.CONFLICTING_ROTATION, "rotation fields disagree")
    if any(angle != 0 for angle in known_angles):
        raise GifPlanError(GifPlanErrorCode.UNSUPPORTED_ROTATION, "nonzero rotation is unsupported")
    return selected


def calculate_gif_geometry(probe: ProbeInfo, preset: GifPreset, *,
                           known_variable_resolution: bool = False) -> GifGeometry:
    """Pure calculation for explicit SAR=1 and explicit zero rotation.

    Use exact rational scale min(1, max_width/width, max_height/height), floor
    each scaled axis, then clamp to at least 1 pixel. Integer raster rounding
    can approximate the aspect ratio, especially for extreme aspect ratios.
    No axis ever exceeds its source or preset bound. Input fps is not used.
    False means no known resolution change, not a whole-file verification.
    """
    settings = _validated_preset(preset)
    stream = _stream(probe, known_variable_resolution)
    scale = min(Fraction(1), Fraction(settings.max_width, stream.width),
                Fraction(settings.max_height, stream.height))
    width = max(1, stream.width * scale.numerator // scale.denominator)
    height = max(1, stream.height * scale.numerator // scale.denominator)
    return GifGeometry(width, height, settings.fps, stream.index)


def _checked_executable(value: str | Path) -> Path:
    if not isinstance(value, (str, Path)):
        raise GifPlanError(GifPlanErrorCode.INVALID_EXECUTABLE, "invalid FFmpeg executable")
    expected = PureWindowsPath(str(DEFAULT_FFMPEG))
    supplied = PureWindowsPath(str(value))
    if not supplied.is_absolute() or supplied != expected:
        raise GifPlanError(GifPlanErrorCode.INVALID_EXECUTABLE, "only the verified absolute FFmpeg location is allowed")
    try:
        executable = validate_input_path(value)
    except PathValidationError:
        raise GifPlanError(GifPlanErrorCode.INVALID_EXECUTABLE, "FFmpeg executable validation failed") from None
    if PureWindowsPath(str(executable)) != expected:
        raise GifPlanError(GifPlanErrorCode.INVALID_EXECUTABLE, "FFmpeg resolved outside the verified location")
    return executable


def _temporary_paths(repository_root: str | Path, job_id: str) -> tuple[Path, Path, Path]:
    if type(job_id) is not str or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", job_id):
        raise GifPlanError(GifPlanErrorCode.INVALID_JOB_ID, "job ID must be 1..64 safe lowercase ASCII characters")
    try:
        # Reuse existing path normalization / network / reparse-point checks.
        # _checked_path is an internal dependency; no existing API is expanded.
        root = _checked_path(repository_root, must_exist=True)
        expected = root / "lab" / "temp"
        temp = _checked_path(expected, must_exist=True)
        if not root.is_dir() or not temp.is_dir() or temp != expected:
            raise GifPlanError(GifPlanErrorCode.INVALID_PATH, "temporary root escapes approved lab/temp")
        job = _checked_path(temp / job_id, must_exist=False)
        palette = _checked_path(job / "palette.png", must_exist=False)
        gif = _checked_path(job / "animation.gif", must_exist=False)
        if job.parent != temp or palette.parent != job or gif.parent != job:
            raise GifPlanError(GifPlanErrorCode.INVALID_PATH, "temporary outputs escape the approved job directory")
        # Reuse of even an empty job directory is rejected. No reservation or
        # filesystem mutation is attempted; execution needs exclusive mkdir.
        if (job.exists() or palette.exists() or gif.exists()
                or any(child.name.casefold() == job_id.casefold() for child in temp.iterdir())):
            raise GifPlanError(GifPlanErrorCode.OUTPUT_COLLISION, "temporary job/output name already exists")
    except (PathValidationError, OSError, TypeError, ValueError) as exc:
        if isinstance(exc, GifPlanError):
            raise
        raise GifPlanError(GifPlanErrorCode.INVALID_PATH, "temporary path validation failed") from None
    return job, palette, gif


def build_gif_plan(input_path: str | Path, probe: ProbeInfo, preset: GifPreset,
                   repository_root: str | Path, job_id: str, *,
                   ffmpeg_executable: str | Path = DEFAULT_FFMPEG,
                   known_variable_resolution: bool = False) -> GifPlan:
    """Plan two commands, checking metadata only; NEVER create or run anything.

    probe must describe input_path; the current ProbeInfo cannot attest that
    association, orientation absence, or whole-file resolution stability. The
    caller must supply trusted matching observations. Missing rotation is NOT
    inferred to be zero. Final output publication is outside this API.
    """
    settings = _validated_preset(preset)
    geometry = calculate_gif_geometry(probe, settings, known_variable_resolution=known_variable_resolution)
    executable = _checked_executable(ffmpeg_executable)
    try:
        source = validate_input_path(input_path)
    except (PathValidationError, TypeError, ValueError):
        raise GifPlanError(GifPlanErrorCode.INVALID_PATH, "input path validation failed") from None
    if source.suffix.lower() != ".avi":
        raise GifPlanError(GifPlanErrorCode.UNSUPPORTED_FORMAT, "only AVI input is supported")
    job, palette, gif = _temporary_paths(repository_root, job_id)
    if source in (palette, gif):
        raise GifPlanError(GifPlanErrorCode.OUTPUT_COLLISION, "input and temporary output must differ")
    preprocessing = (f"fps={geometry.fps},scale={geometry.width}:{geometry.height}:flags=lanczos,setsar=1")
    input_arguments = (*_GLOBAL_OPTIONS, *_INPUT_OPTIONS, "-i", str(source))
    palette_filter = f"[0:{geometry.stream_index}]{preprocessing},palettegen=stats_mode=full[palette]"
    gif_filter = (f"[0:{geometry.stream_index}]{preprocessing}[video];"
                  "[video][1:0]paletteuse=dither=sierra2_4a[gif]")
    first = FFmpegCommandPlan(executable, (
        *input_arguments, "-filter_complex", palette_filter, "-map", "[palette]",
        *_OUTPUT_OPTIONS, "-frames:v", "1", "-c:v", "png", "-f", "image2", str(palette),
    ))
    second = FFmpegCommandPlan(executable, (
        *input_arguments, "-protocol_whitelist", "file", "-format_whitelist", "image2",
        "-f", "image2", "-pattern_type", "none", "-i", str(palette),
        "-filter_complex", gif_filter, "-map", "[gif]", *_OUTPUT_OPTIONS,
        "-c:v", "gif", "-loop", str(settings.loop), "-f", "gif", str(gif),
    ))
    return GifPlan(geometry, first, second, job, palette, gif)
