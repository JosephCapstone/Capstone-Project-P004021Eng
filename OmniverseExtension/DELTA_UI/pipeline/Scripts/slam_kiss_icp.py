#!/usr/bin/env python3
"""Build a global point-cloud map using KISS-ICP."""

import argparse
import sys
from pathlib import Path

import numpy as np
from plyfile import PlyData, PlyElement


def guess_dataloader(source_path):
    """Best-effort replication of the kind of auto-detection
    kiss_icp_pipeline's CLI does - not its actual source. Confirm this
    picks the right one for your input; override with --dataloader if not."""
    path = Path(source_path)
    if path.is_dir():
        if (path / "metadata.yaml").exists():
            return "rosbag"  # ROS2 bag folder
        return "generic"  # folder of point cloud files

    suffix = path.suffix.lower()
    if suffix == ".pcap":
        return "ouster"
    if suffix == ".bag":
        return "rosbag"
    if suffix == ".mcap":
        return "mcap"
    return "generic"


def build_dataset(source, dataloader, meta=None, topic=None):
    from kiss_icp.datasets import dataset_factory

    kwargs = {}
    if dataloader == "ouster" and meta:
        kwargs["meta"] = [meta]  # unconfirmed shape - see module docstring
    if dataloader == "rosbag":
        # ALWAYS pass topic, even as "" - confirmed by a real crash:
        # kiss_icp.datasets.rosbag.RosbagDataset.__init__(self, data_dir,
        # topic: str, ...) has NO default for topic, so omitting the kwarg
        # entirely (which happened here when topic was falsy) fails at
        # Python's own argument-binding, before kiss-icp's own
        # check_topic() auto-detection logic (which correctly picks the
        # only topic when a bag has just one) ever gets a chance to run.
        # An empty string satisfies the required argument AND correctly
        # triggers that auto-detect path inside check_topic().
        kwargs["topic"] = topic or ""

    # kiss-icp's dataloader classes expect a pathlib.Path, not a plain
    # string - confirmed by a real crash: rosbag.py's __init__ calls
    # data_dir.is_file(), which a str doesn't have.
    return dataset_factory(dataloader=dataloader, data_dir=Path(source), **kwargs)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--input",
        required=True,
        help="Source: .pcap, .bag, ROS2 bag folder, .mcap, or a "
        "folder of point cloud files",
    )
    parser.add_argument("--output", required=True, help="Output map .ply")
    parser.add_argument(
        "--meta",
        default=None,
        help="Ouster metadata .json (only used with the ouster dataloader)",
    )
    parser.add_argument(
        "--topic",
        default=None,
        help="Point cloud topic name (only used with the rosbag dataloader) - "
        "e.g. /ouster/points. Needed if the bag has more than one "
        "PointCloud2-like topic; may be optional if there's only one.",
    )
    parser.add_argument(
        "--dataloader",
        default=None,
        help="Force a specific kiss-icp dataloader (ouster, rosbag, mcap, "
        "generic, kitti, etc.) instead of auto-detecting from --input",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to a kiss-icp config YAML (generate a starting one with "
        "'kiss_icp_dump_config') - use this to set voxel size and other "
        "tuning parameters, which aren't exposed as flags here",
    )
    parser.add_argument(
        "--max-range",
        type=float,
        default=None,
        help="Override max sensor range from the config (registration "
        "range). Takes priority over --map-max-range for the "
        "registration range.",
    )
    parser.add_argument(
        "--min-range",
        type=float,
        default=None,
        help="Override min sensor range from the config (config.data.min_range, "
        "left at kiss-icp's own default of 0.0 - no cropping - if omitted). "
        "Useful on a robot-mounted sensor that can see part of its own "
        "mount hardware/chassis: those near-sensor returns sit at a fixed "
        "position relative to the sensor in EVERY frame, so they add up "
        "into a dense, static-looking blob of noise in the final map, and "
        "can also throw off frame-to-map registration since the solver "
        "sees spurious 'unmoving' geometry close to the sensor on every "
        "scan. NOT YET CONFIRMED against a real installed kiss-icp version "
        "- same caveat as --voxel-size below; if this errors out, edit the "
        "config YAML's own data.min_range by hand instead and drop this "
        "flag for this run.",
    )
    parser.add_argument(
        "--voxel-size",
        type=float,
        default=None,
        help="Override mapping.voxel_size from the config (or kiss-icp's own "
        "default, if no --config is given) - NOT YET CONFIRMED against a "
        "real installed kiss-icp, see the module docstring's dated note",
    )
    parser.add_argument(
        "--map-max-range",
        type=float,
        default=None,
        help="Maximum range in meters, from the sensor at the time of each "
        "frame. Replaces the config's data.max_range, so it sets the "
        "registration range AND the range of the saved map. If "
        "--max-range is also given, --max-range sets the registration "
        "range and this option only crops the saved map.",
    )
    parser.add_argument(
        "--map-max-horizontal",
        type=float,
        default=None,
        help="Remove points whose HORIZONTAL distance from the sensor "
        "(meters, map frame) is more than this, from the saved map only.",
    )
    parser.add_argument(
        "--map-max-vertical",
        type=float,
        default=None,
        help="Remove points whose VERTICAL distance from the sensor "
        "(meters, map frame, above or below) is more than this, from "
        "the saved map only.",
    )
    parser.add_argument(
        "--local-map",
        action="store_true",
        help="Save kiss-icp's local map instead of the global map of all "
        "frames. The local map only holds points within data.max_range "
        "of the robot's FINAL position (the old default of this script). "
        "Cannot be used with the --map-max-* limits.",
    )
    args = parser.parse_args()

    for flag, value in (
        ("--map-max-range", args.map_max_range),
        ("--map-max-horizontal", args.map_max_horizontal),
        ("--map-max-vertical", args.map_max_vertical),
    ):
        if value is not None and value <= 0:
            print(f"ERROR: {flag} must be greater than 0.")
            return 1
    any_limit = any(
        v is not None
        for v in (args.map_max_range, args.map_max_horizontal, args.map_max_vertical)
    )
    if args.local_map and any_limit:
        print(
            "ERROR: --local-map cannot be used with --map-max-range, "
            "--map-max-horizontal or --map-max-vertical. The local map is not built "
            "from the registered frames, so these limits do not apply to it."
        )
        return 1
    use_global_map = not args.local_map

    try:
        from kiss_icp.config import load_config
        from kiss_icp.kiss_icp import KissICP
    except ImportError as e:
        print(f"ERROR: could not import kiss-icp ({e}). Run: pip install kiss-icp")
        return 1

    dataloader = args.dataloader or guess_dataloader(args.input)
    print(
        f"Using dataloader: '{dataloader}'"
        + (" (auto-detected)" if not args.dataloader else " (forced via --dataloader)")
    )

    if dataloader == "ouster":
        try:
            import ouster.sdk  # noqa: F401
        except ImportError:
            print(
                "ERROR: the 'ouster' dataloader needs ouster-sdk. Run: pip install ouster-sdk"
            )
            return 1

    print(f"Loading dataset from: {args.input}")
    dataset = build_dataset(args.input, dataloader, meta=args.meta, topic=args.topic)
    n_frames = len(dataset)
    print(f"  {n_frames} frames found.")
    if n_frames == 0:
        print("ERROR: no frames found - check --input and --dataloader.")
        return 1

    # --map-max-range replaces the config's data.max_range (registration range)
    # unless --max-range is given. See the module docstring (2026-10-15).
    registration_range = (
        args.max_range if args.max_range is not None else args.map_max_range
    )
    config = load_config(args.config, max_range=registration_range)
    if registration_range is not None:
        source_flag = "--max-range" if args.max_range is not None else "--map-max-range"
        print(
            f"  Registration max range set to {config.data.max_range} m by {source_flag} "
            f"(overrides the config's data.max_range)."
        )
    else:
        print(f"  Registration max range: {config.data.max_range} m (from the config).")

    if args.min_range is not None:
        try:
            config.data.min_range = args.min_range
            print(
                f"  Min range overridden to: {args.min_range} m (config.data.min_range)"
            )
        except Exception as e:
            print(
                f"ERROR: could not override min_range on the loaded config "
                f"({type(e).__name__}: {e}) - this installed kiss-icp's config object may "
                f"not accept attribute assignment the way this override assumes. Edit the "
                f"config YAML's data.min_range directly instead, and drop --min-range for "
                f"this run."
            )
            return 1

    if args.voxel_size is not None:
        try:
            config.mapping.voxel_size = args.voxel_size
            print(
                f"  Voxel size overridden to: {args.voxel_size} m (config.mapping.voxel_size)"
            )
        except Exception as e:
            print(
                f"ERROR: could not override voxel_size on the loaded config "
                f"({type(e).__name__}: {e}) - this installed kiss-icp's config object may "
                f"not accept attribute assignment the way this override assumes (see the "
                f"module docstring's dated note on --voxel-size). Edit the config YAML's "
                f"mapping.voxel_size directly instead, and drop --voxel-size for this run."
            )
            return 1

    odometry = KissICP(config=config)

    global_map = None
    if use_global_map:
        from kiss_icp.mapping import VoxelHashMap
        from kiss_icp.voxelization import voxel_down_sample

        if (
            args.map_max_range is not None
            and args.map_max_range > config.data.max_range
        ):
            print(
                f"  NOTE: --map-max-range ({args.map_max_range} m) is larger than the "
                f"config's data.max_range ({config.data.max_range} m). Each frame is "
                f"already cropped to data.max_range before this step, so the "
                f"effective map range is {config.data.max_range} m."
            )
        # max_distance only matters for RemovePointsFarFromLocation(),
        # which this map never calls (add_points() only) - so it is
        # never pruned. A large value is passed for clarity.
        global_map = VoxelHashMap(
            voxel_size=config.mapping.voxel_size,
            max_distance=1.0e9,
            max_points_per_voxel=config.mapping.max_points_per_voxel,
        )
        limits = []
        if args.map_max_range is not None:
            limits.append(f"range {args.map_max_range} m")
        if args.map_max_horizontal is not None:
            limits.append(f"horizontal {args.map_max_horizontal} m")
        if args.map_max_vertical is not None:
            limits.append(f"vertical {args.map_max_vertical} m")
        print(
            "  Saving the global map of all frames (not pruned around the final position)."
        )
        print(
            f"  Map limits: {', '.join(limits) if limits else 'none'} "
            f"(registration range is data.max_range = {config.data.max_range} m)"
        )
    else:
        print(
            f"  Saving the kiss-icp local map: only points within data.max_range "
            f"({config.data.max_range} m) of the robot's FINAL position (--local-map)."
        )

    print("Running odometry...")
    report_every = max(1, n_frames // 20)
    kept_points = 0
    dropped_points = 0
    for idx in range(n_frames):
        raw_frame, timestamps = dataset[idx]
        frame, _source = odometry.register_frame(raw_frame, timestamps)
        if global_map is not None:
            down = voxel_down_sample(frame, config.mapping.voxel_size * 0.5)
            pose = odometry.last_pose
            # Offset from the sensor, in map-frame axes (see docstring).
            offset = down @ pose[:3, :3].T
            in_range = np.ones(len(down), dtype=bool)
            if args.map_max_range is not None:
                in_range &= np.linalg.norm(down, axis=1) <= args.map_max_range
            if args.map_max_horizontal is not None:
                in_range &= (
                    np.hypot(offset[:, 0], offset[:, 1]) <= args.map_max_horizontal
                )
            if args.map_max_vertical is not None:
                in_range &= np.abs(offset[:, 2]) <= args.map_max_vertical
            kept_points += int(in_range.sum())
            dropped_points += int((~in_range).sum())
            if in_range.any():
                world = offset[in_range] + pose[:3, 3]
                global_map.add_points(np.ascontiguousarray(world, dtype=np.float64))
        if (idx + 1) % report_every == 0 or idx == n_frames - 1:
            print(f"  {idx + 1}/{n_frames} frames processed")

    if global_map is not None:
        total = kept_points + dropped_points
        if any_limit:
            pct = (100.0 * dropped_points / total) if total else 0.0
            print(
                f"Map limits removed {dropped_points} of {total} downsampled frame points "
                f"({pct:.1f}%) before they reached the saved map."
            )
        print("Extracting the accumulated map from the global map...")
        points = global_map.point_cloud()
    else:
        print("Extracting the accumulated map from the local voxel map...")
        points = odometry.local_map.point_cloud()
    print(f"  {len(points)} points in the accumulated map.")

    if len(points) == 0:
        print(
            "WARNING: accumulated map has zero points - something likely went wrong "
            "during registration. Check the frame count and dataloader choice above."
        )

    vertex_dtype = [("x", "f4"), ("y", "f4"), ("z", "f4")]
    vertex_data = np.zeros(len(points), dtype=vertex_dtype)
    vertex_data["x"] = points[:, 0].astype(np.float32)
    vertex_data["y"] = points[:, 1].astype(np.float32)
    vertex_data["z"] = points[:, 2].astype(np.float32)
    element = PlyElement.describe(vertex_data, "vertex")
    PlyData([element], text=False).write(args.output)

    print(f"Saved to: {args.output}")
    if args.voxel_size is None:
        print(
            "\nNOTE: voxel size and other tuning parameters come from kiss-icp's own config "
            "(default, or --config if given) - not overridden here. Run "
            "'kiss_icp_dump_config' to generate an editable starting config file if you need "
            "to change these, or pass --voxel-size to override just that one value."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
