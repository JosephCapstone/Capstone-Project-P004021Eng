"""Pure helpers shared by the mapping worker and its tests."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence


MAP_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def validate_map_name(name: str) -> str:
    """Return a safe map stem or raise ``ValueError``."""
    value = name.strip()
    if not MAP_NAME_PATTERN.fullmatch(value):
        raise ValueError(
            "Map name must be 1-64 characters and use only letters, numbers, "
            "underscores, or hyphens"
        )
    return value


def validate_saved_map(pgm_path: Path, yaml_path: Path, expected_name: str) -> None:
    """Validate the two files produced by ``map_saver_cli``."""
    if not pgm_path.is_file() or pgm_path.stat().st_size <= 16:
        raise RuntimeError(f"Saved map image is missing or empty: {pgm_path}")
    if not yaml_path.is_file() or yaml_path.stat().st_size <= 16:
        raise RuntimeError(f"Saved map metadata is missing or empty: {yaml_path}")

    with pgm_path.open("rb") as stream:
        if stream.read(2) not in (b"P5", b"P2"):
            raise RuntimeError(f"Saved map is not a PGM image: {pgm_path}")

    metadata = yaml_path.read_text(encoding="utf-8")
    if f"image: {expected_name}.pgm" not in metadata:
        raise RuntimeError("Saved map YAML does not reference the matching PGM")
    if "resolution:" not in metadata or "origin:" not in metadata:
        raise RuntimeError("Saved map YAML is missing resolution or origin metadata")


def save_trinary_map(
    data: Sequence[int] | Iterable[int], geometry: "MapGeometry", stem: Path
) -> tuple[Path, Path]:
    """Write a Nav2-compatible trinary PGM/YAML pair."""
    values = list(data)
    expected = geometry.width * geometry.height
    if len(values) != expected:
        raise ValueError(f"Expected {expected} cells, received {len(values)}")

    pixels = bytearray(expected)
    for source_row in range(geometry.height):
        target_row = geometry.height - 1 - source_row
        for column in range(geometry.width):
            value = int(values[source_row * geometry.width + column])
            if value < 0 or 25 < value < 65:
                shade = 205
            elif value >= 65:
                shade = 0
            else:
                shade = 254
            pixels[target_row * geometry.width + column] = shade

    pgm_path = stem.with_suffix(".pgm")
    yaml_path = stem.with_suffix(".yaml")
    header = (
        "P5\n"
        f"# CREATOR: delta_mapping_worker {geometry.resolution:.9g} m/pix\n"
        f"{geometry.width} {geometry.height}\n"
        "255\n"
    ).encode("ascii")
    pgm_path.write_bytes(header + pixels)
    yaml_path.write_text(
        f"image: {stem.name}.pgm\n"
        "mode: trinary\n"
        f"resolution: {geometry.resolution:.9g}\n"
        f"origin: [{geometry.origin_x:.12g}, {geometry.origin_y:.12g}, "
        f"{geometry.origin_yaw:.12g}]\n"
        "negate: 0\n"
        "occupied_thresh: 0.65\n"
        "free_thresh: 0.25\n",
        encoding="utf-8",
    )
    return pgm_path, yaml_path


@dataclass(frozen=True)
class MapGeometry:
    width: int
    height: int
    resolution: float
    origin_x: float
    origin_y: float
    origin_yaw: float


def _pose_pixel(
    geometry: MapGeometry, pose_xy: Optional[tuple[float, float]]
) -> Optional[tuple[int, int]]:
    if pose_xy is None or geometry.resolution <= 0.0:
        return None

    delta_x = pose_xy[0] - geometry.origin_x
    delta_y = pose_xy[1] - geometry.origin_y
    cos_yaw = math.cos(geometry.origin_yaw)
    sin_yaw = math.sin(geometry.origin_yaw)
    grid_x = (cos_yaw * delta_x + sin_yaw * delta_y) / geometry.resolution
    grid_y = (-sin_yaw * delta_x + cos_yaw * delta_y) / geometry.resolution
    column = int(math.floor(grid_x))
    row = geometry.height - 1 - int(math.floor(grid_y))
    if row < 0 or row >= geometry.height or column < 0 or column >= geometry.width:
        return None
    return row, column


def render_occupancy_ppm(
    data: Sequence[int] | Iterable[int],
    geometry: MapGeometry,
    pose_xy: Optional[tuple[float, float]] = None,
) -> bytes:
    """Render an OccupancyGrid as a binary PPM with an optional red pose marker."""
    if geometry.width <= 0 or geometry.height <= 0:
        raise ValueError("Map dimensions must be positive")

    values = list(data)
    expected = geometry.width * geometry.height
    if len(values) != expected:
        raise ValueError(f"Expected {expected} cells, received {len(values)}")

    pixels = bytearray(expected * 3)
    for source_row in range(geometry.height):
        target_row = geometry.height - 1 - source_row
        for column in range(geometry.width):
            value = int(values[source_row * geometry.width + column])
            if value < 0:
                shade = 205
            elif value == 0:
                shade = 254
            else:
                shade = max(0, min(254, round(254 * (1.0 - value / 100.0))))
            index = (target_row * geometry.width + column) * 3
            pixels[index:index + 3] = bytes((shade, shade, shade))

    marker = _pose_pixel(geometry, pose_xy)
    if marker is not None:
        center_row, center_column = marker
        for row_delta in range(-4, 5):
            for column_delta in range(-4, 5):
                if row_delta * row_delta + column_delta * column_delta > 16:
                    continue
                row = center_row + row_delta
                column = center_column + column_delta
                if 0 <= row < geometry.height and 0 <= column < geometry.width:
                    index = (row * geometry.width + column) * 3
                    pixels[index:index + 3] = b"\xff\x25\x25"

    header = f"P6\n{geometry.width} {geometry.height}\n255\n".encode("ascii")
    return header + pixels


def quaternion_yaw(x: float, y: float, z: float, w: float) -> float:
    """Return planar yaw from a quaternion."""
    sin_yaw = 2.0 * (w * z + x * y)
    cos_yaw = 1.0 - 2.0 * (y * y + z * z)
    return math.atan2(sin_yaw, cos_yaw)
