#!/usr/bin/env python3
"""
import_pointcloud.py
=====================
Makes a point cloud PLY file that the stages of the pipeline can read.
Use it for a cloud that was NOT made by Stage 1 (for example a public data
set, or a cloud from another program).

Why it is needed
----------------
The stages (CloudCompare in Stage 3 and Stage 5, the Python scripts after
them) expect a PLY file in which the points are in an element called
"vertex" and the coordinates are called x, y and z. Some files do not
follow this. Example: the MultiChange3D file pointCloud0.ply has the
element name "params". CloudCompare, started from the command line,
cannot find x, y, z in it. It then reads 0 points and reports "not enough
memory". The memory is not the problem.

What it does
------------
1. Reads a PLY file (ASCII or binary).
2. Finds the element that holds the points: "vertex" if it exists, else
   the largest element that has the properties x, y and z.
3. Renames the element to "vertex".
4. Renames the properties to the standard names: x, y, z; red, green,
   blue (also r/g/b, diffuse_red ...); nx, ny, nz (also normal_x ...).
5. Removes points with a coordinate that is not a finite number.
6. Keeps all other number properties (scalar fields, for example
   cd_type) with their own type and name.
7. Writes a binary little-endian PLY file with one element, "vertex".
   Mesh faces and other elements are not written (a message tells it).

The coordinates keep their own type (float or double). The script does not
move, scale or rotate the points. The units must be metres. The script
prints a NOTE when the size of the cloud looks wrong for metres.

Usage
-----
    python import_pointcloud.py --input in.ply --output out.ply
    python import_pointcloud.py --input in.ply --output out.ply --report out.json

Exit code 0 = file written. Exit code 1 = error (no file written).

Standalone: it needs only numpy and plyfile (scipy is used for the point
spacing if it is installed).
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np

try:
    from plyfile import PlyData, PlyElement
except ImportError:                                    # pragma: no cover
    PlyData = None
    PlyElement = None


class ImportError_(Exception):
    """A problem with the input file. The message is written for the user."""


# Property names that are changed to the standard names. The test is on the
# lower-case name.
_XYZ = {"x": "x", "y": "y", "z": "z"}
_COLOUR = {
    "red": "red", "r": "red", "diffuse_red": "red",
    "green": "green", "g": "green", "diffuse_green": "green",
    "blue": "blue", "b": "blue", "diffuse_blue": "blue",
}
_NORMAL = {
    "nx": "nx", "normal_x": "nx", "normalx": "nx",
    "ny": "ny", "normal_y": "ny", "normaly": "ny",
    "nz": "nz", "normal_z": "nz", "normalz": "nz",
}
_STANDARD = {**_XYZ, **_COLOUR, **_NORMAL}

# A cloud in metres that is larger than this (or has a point spacing that
# is larger than this) probably has other units.
_EXTENT_WARN_M = 500.0
_SPACING_WARN_M = 0.5


def _list_property_names(element):
    """Names of the properties that are lists (for example face indices)."""
    return [p.name for p in element.properties if getattr(p, "val_dtype", None) is not None
            and hasattr(p, "len_dtype")]


def _has_xyz(element):
    names = {p.name.lower() for p in element.properties}
    return {"x", "y", "z"} <= names


def find_point_element(ply):
    """Returns the element that holds the points, or raises ImportError_.

    'vertex' is used if it has x, y and z. Otherwise the largest element
    that has x, y and z is used."""
    names = [e.name for e in ply.elements]
    if "vertex" in names:
        element = ply["vertex"]
        if _has_xyz(element):
            return element
    candidates = [e for e in ply.elements if _has_xyz(e)]
    if not candidates:
        found = "; ".join(
            f"{e.name} ({e.count}): " + ", ".join(p.name for p in e.properties)
            for e in ply.elements) or "no element"
        raise ImportError_(
            "The file has no element with the properties x, y and z. "
            "It has: " + found)
    return max(candidates, key=lambda e: e.count)


def _standard_names(prop_names):
    """Maps each property name to its output name. Raises on a clash."""
    mapping = {}
    used = {}
    for name in prop_names:
        out = _STANDARD.get(name.lower(), name)
        if out in used:
            raise ImportError_(
                f"The properties '{used[out]}' and '{name}' would both be "
                f"called '{out}'. Rename one of them and try again.")
        used[out] = name
        mapping[name] = out
    return mapping


def normalize_ply(input_path, output_path):
    """Reads input_path and writes the standard point cloud PLY file to
    output_path. Returns a dict with the result (also written to the report).

    Raises ImportError_ for a problem with the input file."""
    if PlyData is None:
        raise ImportError_("The Python package 'plyfile' is not installed. "
                           "Run: pip install plyfile")
    input_path = Path(input_path)
    output_path = Path(output_path)
    if not input_path.is_file():
        raise ImportError_(f"The input file does not exist: {input_path}")
    try:
        ply = PlyData.read(str(input_path))
    except Exception as exc:                                # noqa: BLE001
        raise ImportError_(f"The file cannot be read as a PLY file: {exc}")

    element = find_point_element(ply)
    data = element.data
    n_in = len(data)
    if n_in == 0:
        raise ImportError_(f"The element '{element.name}' has 0 points.")

    # Keep the number properties. A list property cannot be written back
    # in the same way, so it is left out and the message tells it.
    list_props = set(_list_property_names(element))
    keep = [name for name in data.dtype.names if name not in list_props]
    mapping = _standard_names(keep)

    # Remove the points with a coordinate that is not a finite number.
    coord = {mapping[n]: np.asarray(data[n]) for n in keep if mapping[n] in ("x", "y", "z")}
    finite = np.isfinite(coord["x"]) & np.isfinite(coord["y"]) & np.isfinite(coord["z"])
    n_bad = int(n_in - finite.sum())
    if n_bad == n_in:
        raise ImportError_("Every point has a coordinate that is not a finite number.")

    dtype = np.dtype([(mapping[n], data.dtype[n]) for n in keep])
    out = np.empty(int(finite.sum()), dtype=dtype)
    for n in keep:
        out[mapping[n]] = np.asarray(data[n])[finite]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = output_path.with_name(output_path.name + ".tmp")
    PlyData([PlyElement.describe(out, "vertex")], text=False, byte_order="<").write(str(tmp))
    tmp.replace(output_path)

    xyz = np.column_stack([out["x"], out["y"], out["z"]]).astype(np.float64)
    bbox_min, bbox_max = xyz.min(axis=0), xyz.max(axis=0)
    extent = bbox_max - bbox_min

    spacing = None
    try:
        from scipy.spatial import cKDTree
        rng = np.random.default_rng(0)
        sample = xyz[rng.choice(len(xyz), min(len(xyz), 20000), replace=False)]
        dist, _ = cKDTree(xyz).query(sample, k=2)
        spacing = float(np.median(dist[:, 1]))
    except Exception:                                       # noqa: BLE001
        spacing = None

    other_elements = [e.name for e in ply.elements if e.name != element.name]
    fields = list(out.dtype.names)
    notes = []
    if element.name != "vertex":
        notes.append(f"The element '{element.name}' was renamed to 'vertex'.")
    renamed = {n: mapping[n] for n in keep if mapping[n] != n}
    if renamed:
        notes.append("Properties renamed: " + ", ".join(f"{a} -> {b}" for a, b in renamed.items()) + ".")
    if list_props:
        notes.append("List properties not written: " + ", ".join(sorted(list_props)) + ".")
    if other_elements:
        notes.append("Elements not written: " + ", ".join(other_elements)
                     + " (the stages use points only).")
    if n_bad:
        notes.append(f"{n_bad} point(s) with a coordinate that is not a finite number were removed.")
    if float(extent.max()) > _EXTENT_WARN_M:
        notes.append(f"NOTE: The cloud is {extent.max():.0f} units wide. The stages need metres. "
                     "If the units are millimetres, scale the cloud before you use it.")
    if spacing is not None and spacing > _SPACING_WARN_M:
        notes.append(f"NOTE: The point spacing is {spacing:.3f} units. "
                     "The stages need metres. Check the units of the file.")

    return {
        "input": str(input_path),
        "output": str(output_path),
        "element_in": element.name,
        "n_points_in": int(n_in),
        "n_points": int(len(out)),
        "n_removed_not_finite": n_bad,
        "fields": fields,
        "has_colour": all(c in fields for c in ("red", "green", "blue")),
        "has_normals": all(c in fields for c in ("nx", "ny", "nz")),
        "bbox_min": [float(v) for v in bbox_min],
        "bbox_max": [float(v) for v in bbox_max],
        "extent": [float(v) for v in extent],
        "median_spacing": spacing,
        "notes": notes,
    }


def summary_text(info):
    """A short text for the user (used by the script and by the dialogs)."""
    lines = [
        f"Points: {info['n_points']}"
        + (f" (of {info['n_points_in']})" if info["n_points"] != info["n_points_in"] else ""),
        "Size (x, y, z): " + " x ".join(f"{v:.3f}" for v in info["extent"]) + " units",
    ]
    if info.get("median_spacing") is not None:
        lines.append(f"Median point spacing: {info['median_spacing'] * 100:.2f} cm (if the unit is metres)")
    lines.append("Fields: " + ", ".join(info["fields"]))
    lines.extend(info["notes"])
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Make a point cloud PLY file that the pipeline stages can read.")
    parser.add_argument("--input", required=True, help="PLY file to read")
    parser.add_argument("--output", required=True, help="PLY file to write")
    parser.add_argument("--report", help="optional JSON file for the result")
    args = parser.parse_args(argv)

    try:
        info = normalize_ply(args.input, args.output)
    except ImportError_ as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(summary_text(info))
    if args.report:
        Path(args.report).write_text(json.dumps(info, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
