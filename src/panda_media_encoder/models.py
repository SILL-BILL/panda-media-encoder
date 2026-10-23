"""Immutable GIF settings, independent of UI and process execution."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PaletteSettings:
    stats_mode: str
    dither: str


@dataclass(frozen=True, slots=True)
class GifPreset:
    """Data container; validate untrusted settings with presets.parse_gif_preset."""

    schema_version: int
    id: str
    name: str
    format: str
    max_width: int
    max_height: int
    fps: int
    keep_aspect_ratio: bool
    allow_upscale: bool
    palette: PaletteSettings
    loop: int
