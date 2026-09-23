#!/usr/bin/env python3
"""
slam_kiss_icp.py
===================
Runs KISS-ICP odometry directly via its Python API - not the
kiss_icp_pipeline CLI, which only writes poses/metrics and never an
accumulated point cloud map (confirmed by inspecting real output: config,
poses.npy, poses_kitti.txt, poses_tum.txt, result_metrics - no .ply/.pcd
anywhere). This calls KissICP.register_frame() per frame in a loop, same
as kiss-icp's own OdometryPipeline internally does, then extracts the
fully accumulated map directly from the internal voxel hash map and
writes it out as a .ply - giving Stage 1 the same kind of output
ouster-cli's slam+save already provides, via a genuinely different SLAM
backend/algorithm.

Verified against the real installed kiss-icp 1.2.3 source (pipeline.py,
kiss_icp.py, mapping.py - not guessed from docs):
    - kiss_icp.datasets.dataset_factory(dataloader, data_dir) builds the
      dataset object; dataset[idx] returns (raw_frame, timestamps);
      len(dataset) gives the frame count
    - kiss_icp.kiss_icp.KissICP(config).register_frame(frame, timestamps)
      runs one odometry step; the resulting pose lands at .last_pose
      afterward, and the frame gets folded into .local_map internally
    - .local_map.point_cloud() returns the full accumulated map as an
      Nx3 numpy array - this is what gets written out here

Requires:
    pip install kiss-icp plyfile numpy
    pip install ouster-sdk   (only for the 'ouster' dataloader / .pcap input)

Usage:
    python slam_kiss_icp.py --input capture.pcap --meta metadata.json --output map.ply
    python slam_kiss_icp.py --input capture.bag --output map.ply
    python slam_kiss_icp.py --input recording.mcap --output map.ply
    python slam_kiss_icp.py --input point_cloud_folder --output map.ply
    python slam_kiss_icp.py --input capture.bag --config indoor.yaml --voxel-size 0.08 --output map.ply

STATUS: confirmed working end-to-end against real data - 247-frame ROS2
bag (Ouster driver's PointCloud2 topic, /ouster/points, converted from
ROS2 to ROS1 .bag via rosbags-convert - though this actually ran directly
against the original ROS2 bag folder with --dataloader rosbag, no
conversion needed). All frames processed, 2628-point accumulated map
extracted and saved successfully. This confirms both previously-unverified
guesses were correct:
  - kiss-icp's rosbag dataloader needs data_dir as a pathlib.Path, not a
    plain string (this was a real bug, now fixed - AttributeError on
    data_dir.is_file() otherwise)
  - the rosbag dataloader's topic-selection kwarg is indeed named 'topic'
Point count from the accumulated map may look low relative to raw frame
density - that's kiss-icp's own internal voxel map accumulation (not
this script), controllable via --config if you need finer output.

The dataloader auto-detection function below is still an unverified
guess at what kiss_icp_pipeline's CLI does internally (its actual logic
in kiss_icp/tools/cmd.py wasn't inspected) - low risk, since --dataloader
lets you force the right one if it guesses wrong. The ouster dataloader's
--meta argument shape (list vs. plain path) also remains unconfirmed.

--voxel-size (added 2026-08-14, NOT YET CONFIRMED against real data):
overrides config.mapping.voxel_size on the loaded config object, after
load_config() runs - independent of --config, so a different cell size
can be tried without hand-editing or duplicating a YAML file just for
that one value. This assumes kiss-icp's config object (a pydantic model,
per the real 1.2.3 source inspected above) allows attribute assignment
after construction - plausible, since kiss-icp doesn't appear to mark it
frozen, but not yet run against a real installed kiss-icp to confirm. If
this errors out, the console message says so directly - edit the config
YAML's mapping.voxel_size by hand instead and drop --voxel-size for that
run.

--map-max-range (added 2026-09-23): removes points farther than this
from the sensor from the SAVED map only. Registration still uses the
config's data.max_range. Verified against the kiss-icp 1.2.3 source
(kiss_icp.py, mapping.py, VoxelHashMap.cpp): data.max_range ALSO sets
how far the local map reaches - VoxelHashMap::Update() deletes every
voxel farther than max_range from the robot's CURRENT position after
each frame. The default output below IS that local map, so it only
holds points within data.max_range of the robot's FINAL position.
Lowering data.max_range to cut open-space noise would therefore also
cut the saved map. With --map-max-range, this script instead builds a
separate global map: each registered frame is voxel-downsampled the
same way kiss-icp does internally (voxel_size * 0.5), cropped to
--map-max-range in the sensor frame, moved into the world frame with
the frame's pose, and added to a second kiss-icp VoxelHashMap (same
voxel_size and max_points_per_voxel) that is never pruned. Output
density per voxel therefore follows the same rules as the default path.
Without --map-max-range, the output is unchanged (the local map).
UNVERIFIED against a real capture - tested on synthetic frames only.
"""

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
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", required=True,
                         help="Source: .pcap, .bag, ROS2 bag folder, .mcap, or a "
                              "folder of point cloud files")
    parser.add_argument("--output", required=True, help="Output map .ply")
    parser.add_argument("--meta", default=None,
                         help="Ouster metadata .json (only used with the ouster dataloader)")
    parser.add_argument("--topic", default=None,
                         help="Point cloud topic name (only used with the rosbag dataloader) - "
                              "e.g. /ouster/points. Needed if the bag has more than one "
                              "PointCloud2-like topic; may be optional if there's only one.")
    parser.add_argument("--dataloader", default=None,
                         help="Force a specific kiss-icp dataloader (ouster, rosbag, mcap, "
                              "generic, kitti, etc.) instead of auto-detecting from --input")
    parser.add_argument("--config", default=None,
                         help="Path to a kiss-icp config YAML (generate a starting one with "
                              "'kiss_icp_dump_config') - use this to set voxel size and other "
                              "tuning parameters, which aren't exposed as flags here")
    parser.add_argument("--max-range", type=float, default=None,
                         help="Override max sensor range from the config")
    parser.add_argument("--min-range", type=float, default=None,
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
                              "flag for this run.")
    parser.add_argument("--voxel-size", type=float, default=None,
                         help="Override mapping.voxel_size from the config (or kiss-icp's own "
                              "default, if no --config is given) - NOT YET CONFIRMED against a "
                              "real installed kiss-icp, see the module docstring's dated note")
    parser.add_argument("--map-max-range", type=float, default=None,
                         help="Remove points farther than this (meters, from the sensor) "
                              "from the SAVED map only. Registration still uses the config's "
                              "data.max_range. Builds an unpruned global map instead of "
                              "saving kiss-icp's local map - see the module docstring.")
    args = parser.parse_args()

    if args.map_max_range is not None and args.map_max_range <= 0:
        print("ERROR: --map-max-range must be greater than 0.")
        return 1

    try:
        from kiss_icp.config import load_config
        from kiss_icp.kiss_icp import KissICP
    except ImportError as e:
        print(f"ERROR: could not import kiss-icp ({e}). Run: pip install kiss-icp")
        return 1

    dataloader = args.dataloader or guess_dataloader(args.input)
    print(f"Using dataloader: '{dataloader}'"
          + (" (auto-detected)" if not args.dataloader else " (forced via --dataloader)"))

    if dataloader == "ouster":
        try:
            import ouster.sdk  # noqa: F401
        except ImportError:
            print("ERROR: the 'ouster' dataloader needs ouster-sdk. Run: pip install ouster-sdk")
            return 1

    print(f"Loading dataset from: {args.input}")
    dataset = build_dataset(args.input, dataloader, meta=args.meta, topic=args.topic)
    n_frames = len(dataset)
    print(f"  {n_frames} frames found.")
    if n_frames == 0:
        print("ERROR: no frames found - check --input and --dataloader.")
        return 1

    config = load_config(args.config, max_range=args.max_range)

    if args.min_range is not None:
        try:
            config.data.min_range = args.min_range
            print(f"  Min range overridden to: {args.min_range} m (config.data.min_range)")
        except Exception as e:
            print(f"ERROR: could not override min_range on the loaded config "
                  f"({type(e).__name__}: {e}) - this installed kiss-icp's config object may "
                  f"not accept attribute assignment the way this override assumes. Edit the "
                  f"config YAML's data.min_range directly instead, and drop --min-range for "
                  f"this run.")
            return 1

    if args.voxel_size is not None:
        try:
            config.mapping.voxel_size = args.voxel_size
            print(f"  Voxel size overridden to: {args.voxel_size} m (config.mapping.voxel_size)")
        except Exception as e:
            print(f"ERROR: could not override voxel_size on the loaded config "
                  f"({type(e).__name__}: {e}) - this installed kiss-icp's config object may "
                  f"not accept attribute assignment the way this override assumes (see the "
                  f"module docstring's dated note on --voxel-size). Edit the config YAML's "
                  f"mapping.voxel_size directly instead, and drop --voxel-size for this run.")
            return 1

    odometry = KissICP(config=config)

    global_map = None
    if args.map_max_range is not None:
        from kiss_icp.mapping import VoxelHashMap
        from kiss_icp.voxelization import voxel_down_sample
        if args.map_max_range > config.data.max_range:
            print(f"  NOTE: --map-max-range ({args.map_max_range} m) is larger than the "
                  f"config's data.max_range ({config.data.max_range} m). Each frame is "
                  f"already cropped to data.max_range before this step, so the "
                  f"effective map range is {config.data.max_range} m.")
        # max_distance only matters for RemovePointsFarFromLocation(),
        # which this map never calls (add_points() only) - so it is
        # never pruned. A large value is passed for clarity.
        global_map = VoxelHashMap(
            voxel_size=config.mapping.voxel_size,
            max_distance=1.0e9,
            max_points_per_voxel=config.mapping.max_points_per_voxel)
        print(f"  Map max range: {args.map_max_range} m (saved map only - registration "
              f"still uses data.max_range = {config.data.max_range} m)")

    print("Running odometry...")
    report_every = max(1, n_frames // 20)
    kept_points = 0
    dropped_points = 0
    for idx in range(n_frames):
        raw_frame, timestamps = dataset[idx]
        frame, _source = odometry.register_frame(raw_frame, timestamps)
        if global_map is not None:
            down = voxel_down_sample(frame, config.mapping.voxel_size * 0.5)
            in_range = np.linalg.norm(down, axis=1) <= args.map_max_range
            keep = down[in_range]
            kept_points += int(in_range.sum())
            dropped_points += int((~in_range).sum())
            if len(keep):
                pose = odometry.last_pose
                world = keep @ pose[:3, :3].T + pose[:3, 3]
                global_map.add_points(np.ascontiguousarray(world, dtype=np.float64))
        if (idx + 1) % report_every == 0 or idx == n_frames - 1:
            print(f"  {idx + 1}/{n_frames} frames processed")

    if global_map is not None:
        total = kept_points + dropped_points
        pct = (100.0 * dropped_points / total) if total else 0.0
        print(f"Map max range removed {dropped_points} of {total} downsampled frame points "
              f"({pct:.1f}%) before they reached the saved map.")
        print("Extracting the accumulated map from the range-limited global map...")
        points = global_map.point_cloud()
    else:
        print("Extracting the accumulated map from the local voxel map...")
        points = odometry.local_map.point_cloud()
    print(f"  {len(points)} points in the accumulated map.")

    if len(points) == 0:
        print("WARNING: accumulated map has zero points - something likely went wrong "
              "during registration. Check the frame count and dataloader choice above.")

    vertex_dtype = [("x", "f4"), ("y", "f4"), ("z", "f4")]
    vertex_data = np.zeros(len(points), dtype=vertex_dtype)
    vertex_data["x"] = points[:, 0].astype(np.float32)
    vertex_data["y"] = points[:, 1].astype(np.float32)
    vertex_data["z"] = points[:, 2].astype(np.float32)
    element = PlyElement.describe(vertex_data, "vertex")
    PlyData([element], text=False).write(args.output)

    print(f"Saved to: {args.output}")
    if args.voxel_size is None:
        print("\nNOTE: voxel size and other tuning parameters come from kiss-icp's own config "
              "(default, or --config if given) - not overridden here. Run "
              "'kiss_icp_dump_config' to generate an editable starting config file if you need "
              "to change these, or pass --voxel-size to override just that one value.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
