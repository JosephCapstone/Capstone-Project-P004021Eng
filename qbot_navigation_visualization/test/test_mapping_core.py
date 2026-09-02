import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qbot_navigation_visualization.mapping_core import (  # noqa: E402
    MapGeometry,
    render_occupancy_ppm,
    save_trinary_map,
    validate_map_name,
    validate_saved_map,
)


def test_validate_map_name_accepts_safe_stems():
    assert validate_map_name("delta_map_20260831-214500") == (
        "delta_map_20260831-214500"
    )


@pytest.mark.parametrize(
    "name", ["", "../map", "map name", ".hidden", "a" * 65, "map.yaml"]
)
def test_validate_map_name_rejects_unsafe_stems(name):
    with pytest.raises(ValueError):
        validate_map_name(name)


def test_render_occupancy_ppm_flips_grid_and_draws_pose():
    geometry = MapGeometry(
        width=3,
        height=2,
        resolution=1.0,
        origin_x=0.0,
        origin_y=0.0,
        origin_yaw=0.0,
    )
    image = render_occupancy_ppm([-1, 0, 100, 0, 0, 0], geometry, (1.5, 0.5))
    header, pixels = image.split(b"\n255\n", 1)
    assert header == b"P6\n3 2"
    assert len(pixels) == 3 * 2 * 3
    # The pose lies on the lower map row, which becomes the lower image row.
    pose_index = (1 * 3 + 1) * 3
    assert pixels[pose_index:pose_index + 3] == b"\xff\x25\x25"


def test_validate_saved_map_pair(tmp_path: Path):
    pgm = tmp_path / "delta_map.pgm"
    yaml = tmp_path / "delta_map.yaml"
    pgm.write_bytes(b"P5\n3 3\n255\n" + bytes(range(9)))
    yaml.write_text(
        "image: delta_map.pgm\nresolution: 0.05\norigin: [0, 0, 0]\n",
        encoding="utf-8",
    )
    validate_saved_map(pgm, yaml, "delta_map")


def test_save_trinary_map_writes_standard_pair(tmp_path: Path):
    geometry = MapGeometry(
        width=3,
        height=2,
        resolution=0.05,
        origin_x=-1.0,
        origin_y=2.0,
        origin_yaw=0.25,
    )
    pgm, yaml = save_trinary_map(
        [-1, 0, 100, 64, 65, 25], geometry, tmp_path / "fallback_map"
    )
    validate_saved_map(pgm, yaml, "fallback_map")
    metadata = yaml.read_text(encoding="utf-8")
    assert "mode: trinary" in metadata
    assert "occupied_thresh: 0.65" in metadata
