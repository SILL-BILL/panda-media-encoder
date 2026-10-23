"""Read-only, bounded loading of the initial GIF preset schema.

Version 1 accepts dimensions 1..640, integer fps 1..10, preserved aspect
ratio, no upscaling, full palette statistics and sierra2_4a dithering only.
GIF loop counts are -1 (no loop), 0 (infinite), or 1..65535.
No arbitrary command, filter, path, or additional format is accepted.
"""

import json
from pathlib import Path
import re
from typing import Any

from .models import GifPreset, PaletteSettings


MAX_PRESET_BYTES = 64 * 1024
_KEYS = {
    "schema_version", "id", "name", "format", "max_width", "max_height",
    "fps", "keep_aspect_ratio", "allow_upscale", "palette", "loop",
}


class PresetValidationError(ValueError):
    """The document is invalid or uses settings unsupported by this phase."""


def _object(value: Any, keys: set[str], label: str) -> dict:
    if type(value) is not dict:
        raise PresetValidationError(f"{label}: expected an object")
    if any(type(key) is not str for key in value):
        raise PresetValidationError(f"{label}: keys must be strings")
    missing, unknown = keys - value.keys(), value.keys() - keys
    if missing or unknown:
        raise PresetValidationError(
            f"{label}: missing keys={sorted(missing)}; unknown keys={sorted(unknown)}"
        )
    return value


def _integer(value: Any, low: int, high: int, label: str) -> int:
    if type(value) is not int or not low <= value <= high:
        raise PresetValidationError(f"{label}: expected integer {low}..{high}")
    return value


def _fixed(value: Any, expected: Any, label: str) -> None:
    if type(value) is not type(expected) or value != expected:
        raise PresetValidationError(f"{label}: only {expected!r} is supported")


def parse_gif_preset(value: Any) -> GifPreset:
    """Validate an already-decoded document without side effects."""
    data = _object(value, _KEYS, "preset")
    _fixed(data["schema_version"], 1, "schema_version")
    identifier, name = data["id"], data["name"]
    if type(identifier) is not str or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", identifier):
        raise PresetValidationError("id: expected a lowercase identifier, at most 64 characters")
    if (type(name) is not str or not 1 <= len(name) <= 80
            or name != name.strip() or any(ord(char) < 32 or ord(char) == 127 for char in name)):
        raise PresetValidationError("name: expected 1..80 characters without edge whitespace or controls")
    _fixed(data["format"], "gif", "format")
    width = _integer(data["max_width"], 1, 640, "max_width")
    height = _integer(data["max_height"], 1, 640, "max_height")
    fps = _integer(data["fps"], 1, 10, "fps")
    _fixed(data["keep_aspect_ratio"], True, "keep_aspect_ratio")
    _fixed(data["allow_upscale"], False, "allow_upscale")
    palette = _object(data["palette"], {"stats_mode", "dither"}, "palette")
    _fixed(palette["stats_mode"], "full", "palette.stats_mode")
    _fixed(palette["dither"], "sierra2_4a", "palette.dither")
    loop = _integer(data["loop"], -1, 65535, "loop")
    return GifPreset(
        schema_version=1, id=identifier, name=name, format="gif",
        max_width=width, max_height=height, fps=fps,
        keep_aspect_ratio=True, allow_upscale=False,
        palette=PaletteSettings(stats_mode="full", dither="sierra2_4a"), loop=loop,
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise PresetValidationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise PresetValidationError(f"non-standard JSON constant: {value}")


def load_gif_preset(path: str | Path) -> GifPreset:
    """Read at most 64 KiB + one sentinel byte; never write preset files."""
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(MAX_PRESET_BYTES + 1)
    except OSError as exc:
        raise PresetValidationError("preset could not be read") from exc
    if len(raw) > MAX_PRESET_BYTES:
        raise PresetValidationError("preset exceeds 64 KiB")
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                          parse_constant=_reject_constant)
    except PresetValidationError:
        raise
    except (ValueError, RecursionError) as exc:
        raise PresetValidationError("preset is not valid UTF-8 JSON") from exc
    return parse_gif_preset(data)
