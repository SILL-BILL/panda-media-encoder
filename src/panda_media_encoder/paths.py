"""Read-only local path checks and GIF output planning.

This module neither opens media contents nor reserves or creates output files.
All symlinks/reparse points are conservatively rejected, even if their targets
would stay local. A plan is a snapshot, NOT a reservation or a guarantee against
later filesystem races. Future execution must revalidate and finalize safely.
"""

import ctypes
import os
from pathlib import Path, PureWindowsPath
import re
import stat
from typing import Iterable


class PathValidationError(ValueError):
    """A local input or proposed output violates the initial safety policy."""


def _is_remote_drive(path: Path) -> bool:
    if os.name != "nt":
        return False
    # GetDriveType performs a drive classification, not a media-content read.
    get_drive_type = ctypes.windll.kernel32.GetDriveTypeW
    get_drive_type.argtypes = [ctypes.c_wchar_p]
    get_drive_type.restype = ctypes.c_uint
    drive_type = get_drive_type(path.anchor)
    if drive_type in (0, 1):
        raise PathValidationError("drive is unknown or unavailable")
    return drive_type == 4  # DRIVE_REMOTE, including mapped network drives


def _local_absolute(value: str | os.PathLike[str]) -> Path:
    raw = os.fspath(value)
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise PathValidationError("expected a nonempty local path")
    windows = PureWindowsPath(raw)
    if raw.startswith(("\\\\", "//")) or windows.drive.startswith("\\\\"):
        raise PathValidationError("UNC and device paths are not allowed")
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", raw) and not re.match(r"^[A-Za-z]:[\\/]", raw):
        raise PathValidationError("URLs and drive-relative paths are not allowed")
    if windows.drive and os.name != "nt":
        raise PathValidationError("Windows drive paths require Windows")
    # Reject ADS and Win32-special aliases before any filesystem lookup.
    for component in windows.parts:
        if component == windows.anchor or component in (".", ".."):
            continue
        if (any(char in component for char in '<>:"|?*')
                or any(ord(char) < 32 for char in component)
                or component.endswith((" ", ".")) or PureWindowsPath(component).is_reserved()):
            raise PathValidationError("path contains an unsafe Windows component")
    path = Path(os.path.abspath(raw))
    if _is_remote_drive(path):
        raise PathValidationError("network drives are not allowed")
    return path


def _reject_links(path: Path) -> None:
    # Check from the root down so a junction is rejected before following it.
    for part in (*reversed(path.parents), path):
        try:
            info = part.lstat()
        except FileNotFoundError:
            break
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise PathValidationError("symlinks and reparse points are not supported")


def _checked_path(value: str | os.PathLike[str], *, must_exist: bool) -> Path:
    try:
        path = _local_absolute(value)
        _reject_links(path)
        return path.resolve(strict=must_exist)
    except (OSError, RuntimeError) as exc:
        raise PathValidationError("path is missing, inaccessible, or cannot be resolved") from exc


def validate_input_path(value: str | os.PathLike[str]) -> Path:
    """Accept existing local regular files without reading their contents."""
    path = _checked_path(value, must_exist=True)
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            raise PathValidationError("input must be a regular file")
    except OSError as exc:
        raise PathValidationError("input metadata is inaccessible") from exc
    return path


def _path_key(path: Path) -> str:
    # Windows output naming is case-insensitive even in tests on another OS.
    return str(path).casefold()


def plan_gif_output(
    input_path: str | os.PathLike[str],
    repository_root: str | os.PathLike[str],
    queued_outputs: Iterable[str | os.PathLike[str]] = (),
) -> Path:
    """Plan <input stem>_review.gif only inside root/lab/output.

    The root is trusted application configuration, never a preset field.
    Directories must already exist. No mkdir, copies, deletes or finalization.
    """
    source = validate_input_path(input_path)
    root = _checked_path(repository_root, must_exist=True)
    expected = root / "lab" / "output"
    directory = _checked_path(expected, must_exist=True)
    if _path_key(directory) != _path_key(expected) or not directory.is_dir():
        raise PathValidationError("output directory escapes the approved lab/output")
    candidate = _checked_path(directory / f"{source.stem}_review.gif", must_exist=False)
    if _path_key(candidate.parent) != _path_key(directory):
        raise PathValidationError("output escapes the approved directory")
    try:
        if candidate.exists() and source.samefile(candidate):
            raise PathValidationError("input and output refer to the same file")
        # Also detect case aliases on case-sensitive test filesystems.
        if any(child.name.casefold() == candidate.name.casefold() for child in directory.iterdir()):
            raise PathValidationError("output name already exists")
    except OSError as exc:
        raise PathValidationError("cannot check output collisions") from exc
    for queued in queued_outputs:
        other = _checked_path(queued, must_exist=False)
        if _path_key(other.parent) != _path_key(directory):
            raise PathValidationError("queued output is outside approved lab/output")
        if _path_key(other) == _path_key(candidate):
            raise PathValidationError("output name collides with another queued candidate")
    return candidate
