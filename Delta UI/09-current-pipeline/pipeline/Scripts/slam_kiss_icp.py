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
fully accumulated map directly from a voxel hash map and writes it out as
a .ply - giving Stage 1 the same kind of output ouster-cli's slam+save
already provides, via a genuinely different SLAM backend/algorithm.

Verified against the real installed kiss-icp 1.2.3 source (pipeline.py,
kiss_icp.py, mapping.py - not guessed from docs):
    - kiss_icp.datasets.dataset_factory(dataloader, data_dir) builds the
      dataset object; dataset[idx] returns (raw_frame, timestamps);
      len(dataset) gives the frame count
    - kiss_icp.kiss_icp.KissICP(config).register_frame(frame, timestamps)
      runs one odometry step; the resulting pose lands at .last_pose
      afterward, and the frame gets folded into .local_map internally
    - .local_map.point_cloud() returns the local map as an Nx3 numpy array

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

WHICH MAP IS SAVED (changed 2026-10-14):
data.max_range does TWO jobs in kiss-icp (verified against the kiss-icp
1.2.3 source: kiss_icp.py, mapping.py, VoxelHashMap.cpp): it crops every
frame, and VoxelHashMap::Update() deletes every local-map voxel farther
than max_range from the robot's CURRENT position after each frame. The
kiss-icp local map is therefore a ball around the robot, and the local map
at the end of a run only holds points within data.max_range of the robot's
FINAL position. On a path that ends where it started (a loop), the saved
local map was a ball around the start, and the far end of a room that is
longer than max_range was missing. That was the old default of this script.
The default is now a separate global map: each registered frame is
voxel-downsampled the same way kiss-icp does internally (voxel_size * 0.5),
cropped to any map limit that is set (see below), moved into the world
frame with the frame's pose, and added to a second kiss-icp VoxelHashMap
(same voxel_size and max_points_per_voxel) that is never pruned. Output
density per voxel follows the same rules as the local map. Registration
uses data.max_range (the config's value, or the value of --max-range or
--map-max-range), and each frame is cropped to data.max_range, so the
global map reaches as far as the sensor saw from every pose on the path. --local-map gives the old output (the kiss-icp
local map near the final position).

--map-max-range (added 2026-09-23): removes points farther than this
from the sensor from the SAVED map only, measured in each frame from the
sensor position of THAT frame (not from the start or the end of the
path).
CHANGED 2026-10-15: --map-max-range now also replaces the config's
data.max_range for the run, so registration uses the same range. Before
this change it cropped only the saved map and registration kept the
config's range, so the setting looked like it did nothing to the SLAM.
The range can now be larger than the config's value too. If --max-range is
given as well, --max-range sets the registration range and --map-max-range
only crops the saved map (the old behaviour). The horizontal and vertical
limits below still crop the saved map only.

--map-max-horizontal / --map-max-vertical (added 2026-09-23): the same
saved-map-only crop as --map-max-range, but split into a horizontal
distance (sqrt(dx^2 + dy^2)) and a vertical distance (|dz|) from the
sensor, so an open area can be cut short horizontally without losing a
high ceiling (or the reverse). Measured in the MAP frame, relative to
the sensor's position at that frame: the offset is the frame point
rotated by the frame's pose (translation removed). The map frame is
kiss-icp's first-frame sensor frame, so "vertical" means the sensor's
up axis at the START of the scan - robot pitch or roll during the scan
does not tilt the limit, but a sensor that was already tilted at the
start does (Stage 2 levels the map afterwards, not this crop). Any
combination of the three limits can be given; a point must pass every
limit that is set. These limits cannot be used with --local-map.
UNVERIFIED against a real capture - tested on synthetic frames only.

ROS2 BAG FOLDERS ARE READ BY THIS SCRIPT (added 2026-10-16, Hesai XT32 support):
For a ROS2 bag folder (a folder with metadata.yaml) with dataloader "rosbag",
this script reads the bag itself with the 'rosbags' package. It does not use
the kiss-icp rosbag loader any more. Reasons:
  - kiss-icp 1.2.3 does not depend on 'rosbags'. Only ouster-sdk installs it.
    A PC without ouster-sdk has no rosbags, and the kiss-icp loader fails.
  - With rosbags 0.11.0 the kiss-icp loader stops at its import of its own
    point_cloud2 module ("rosbags library not installed", although rosbags is
    installed). rosbags 0.9.x works. Also, AnyReader without a typestore stops
    on a ROS2 Humble sqlite bag with "Bag contains no type definitions" (such a
    bag holds no message definitions). Both checked on two real Hesai XT32 bags,
    2026-10-16.
The built-in reader works with rosbags 0.9.x and with 0.10 and newer (it passes
the ROS2 Humble typestore when rosbags has one). It returns the same data as the
kiss-icp loader: x, y, z as float64 and the per-point time from the first field
named t, timestamp or time. Differences:
  - The time is made relative to the earliest time of the frame. KISS-ICP scales
    the time of each frame by its own minimum and maximum, so this does not change
    the result. It keeps the precision of an absolute float64 time, which is what
    the Hesai ROS2 driver publishes (field 'timestamp', seconds since 1970).
    A time field that cannot be used (float32 absolute time, or no spread within
    the frame) is reported once, and that frame set runs without deskew.
  - Points that are not finite are removed together with their time values.
  - Points at exactly (0, 0, 0) are removed. They are returns that did not come
    back (about 8 percent of the points of a Hesai XT32 frame). Without this, every
    frame adds a point at the sensor position to the map. --keep-zero-points keeps
    them (the old behaviour).
--dataloader rosbag-kiss uses the kiss-icp loader again. ROS1 .bag files and
.mcap files still use the kiss-icp loaders.
Checked: two real Hesai XT32 bags (stationary), rosbags 0.9.23 and 0.11.0: the
saved maps are identical. With --keep-zero-points the map has the same 73623
points as the kiss-icp loader (bag 1, rosbags 0.9.23, Ouster config), and 8 of
them are within 5 cm of the sensor position. Without it, the map has 73603
points and none are there. The two maps are not the same point for point,
because the points at (0, 0, 0) also change the ICP a little. NOT checked: a
moving Hesai recording (deskew on real motion), and a bag from a real Ouster
sensor.

--flying-pixel-filter (added 2026-10-16, update 23, NOT YET CONFIRMED against a
real capture): removes flying pixels from every frame BEFORE the frame goes to
KISS-ICP. A flying pixel is a false point between a near surface and a far
surface (the laser spot is on the edge of an object and the return is a mix of
both). It lies in the air, up to tens of centimetres from any real surface. Later
stages cannot remove it without also cutting thin real structure, because a
general noise filter cannot see the difference. The scan can: a flying pixel has
a range that is very different from BOTH neighbours on the same line of the range
image (the line of one laser beam, or the same column in the next beam), and a
real edge point has one neighbour at a similar range.
The test: a point is removed when |r - r_before| > T and |r - r_after| > T, on the
row direction or on the column direction, and the point is not on the straight line
between its neighbours (|r_before + r_after - 2 r| > T; this keeps a floor or a wall
that the beam crosses at a small angle). T = --flying-pixel-abs + --flying-pixel-rel
x r, where r is the range of the point. The defaults are 0.10 m and 0.02.
On simulated frames (a 128-beam sensor, a room with ribs, pipes and lockers; points
in row order, as the Ouster pcap dataloader gives them) the filter removed 66 % (low
noise), 71 % (medium noise) and 57 % (high noise) of the false returns that the
simulator marks. It removed 0.06 %, 0.10 % and 1.3 % of the good points. It needs
about 50 ms for a frame of 120 000 points. The map from the true poses had 2.61 %
of its points more than 5 cm from the surface at low noise. A filter of this kind
before the merge (without the slope test) and the noise filter SOR 6 / 1.0 after
the merge brought it to 0.26 %. SOR alone gave 1.08 %.
Where the rows and columns come from: the kiss-icp dataloaders give a list of
points and a time stamp for each point, not an image. The time stamp of a point is
the same for all points in one column (the Ouster dataloader and the Ouster ROS
driver give the time in the scan for each column). The rows follow one after the
other, so the time stamp falls back to a small value at the start of each new row.
The script finds the column from the rank of the time stamp, and the row from the
number of falls back. The filter runs only when this gives an image that makes
sense (see organised_layout()). A frame without it is not filtered, and the script
prints a count at the end. A source with no time stamps (the generic dataloader)
is never filtered. A source in firing order (column after column, as the
Hesai drivers and the simulator write it) has no rows to find, and it is not
filtered.
The Ouster dataloader keeps the staggered layout of the sensor. In this layout the
beams of one column are a few columns apart in azimuth, so the test in the column
direction compares points that are a little more apart than neighbours. This
makes the test a little stricter at real edges. Check the result on your sensor:
run once with and once without the option, and compare the maps.
"""

import argparse
import sys
from pathlib import Path

import numpy as np
from plyfile import PlyData, PlyElement


# Field names that kiss-icp reads as the per-point time (kiss_icp/tools/
# point_cloud2.py, read_point_cloud). The first field of the message with one of
# these names is used. Ouster: 't' (uint32, ns from the frame start). Hesai:
# 'timestamp' (float64, seconds since 1970).
TIME_FIELD_NAMES = ("t", "timestamp", "time")

# sensor_msgs/PointField datatype -> numpy type code
_POINTFIELD_TYPES = {1: "i1", 2: "u1", 3: "i2", 4: "u2", 5: "i4", 6: "u4", 7: "f4", 8: "f8"}

# A float32 time of this size or more has a step of 0.1 s or more. Not usable.
_FLOAT32_TIME_LIMIT = 1.0e6


def is_ros2_bag_folder(path):
    path = Path(path)
    return path.is_dir() and (path / "metadata.yaml").exists()


def _rosbags_version():
    try:
        from importlib.metadata import version
        return version("rosbags")
    except Exception:
        return "unknown version"


class Ros2BagDataset:
    """Reads one PointCloud2 topic of a ROS2 bag folder with the rosbags package.
    Same interface as the kiss-icp datasets that this script uses: len(dataset)
    and dataset[idx] -> (points Nx3 float64, timestamps N float64 or an empty
    array). Frames must be read in order (0, 1, 2, ...), as the main loop does."""

    def __init__(self, bag_dir, topic=None, drop_zero_points=True):
        try:
            from rosbags.highlevel import AnyReader
        except ImportError as error:
            raise RuntimeError(
                f"Reading a ROS2 bag needs the 'rosbags' package ({error}). "
                f"Run: pip install rosbags") from error
        self.drop_zero_points = drop_zero_points
        paths = [Path(bag_dir)]
        try:
            from rosbags.typesys import Stores, get_typestore   # rosbags 0.10 and newer
        except ImportError:
            self._reader = AnyReader(paths)                    # rosbags 0.9.x
        else:
            self._reader = AnyReader(
                paths, default_typestore=get_typestore(Stores.ROS2_HUMBLE))
        self._reader.open()

        clouds = [c for c in self._reader.connections
                  if c.msgtype == "sensor_msgs/msg/PointCloud2"]
        names = ", ".join(c.topic for c in clouds) or "none"
        if topic:
            chosen = [c for c in clouds if c.topic == topic]
            if not chosen:
                self.close()
                raise RuntimeError(f"The bag has no PointCloud2 topic '{topic}'. "
                                   f"PointCloud2 topics in this bag: {names}.")
        elif len(clouds) == 1:
            chosen = clouds
        else:
            self.close()
            raise RuntimeError(
                "The bag has no PointCloud2 topic." if not clouds else
                f"The bag has more than one PointCloud2 topic ({names}). "
                f"Choose one with --topic.")
        self.connection = chosen[0]
        self.topic = self.connection.topic
        self.n_frames = int(self.connection.msgcount)
        self.reader_version = _rosbags_version()
        self.time_field = None
        self.time_note = None            # why a frame had no usable time, set once
        self.zero_points_dropped = 0
        self.nonfinite_points_dropped = 0
        self.frames_without_time = 0
        self.points_kept = 0
        self.first_frame_time_span = None
        self._warned = set()
        self._start_iteration()

    def _start_iteration(self):
        self._messages = self._reader.messages(connections=[self.connection])
        self._next = 0

    def close(self):
        reader, self._reader = getattr(self, "_reader", None), None
        if reader is not None:
            try:
                reader.close()
            except Exception:
                pass

    def __del__(self):
        self.close()

    def __len__(self):
        return self.n_frames

    def __getitem__(self, idx):
        if idx != self._next:
            if idx == 0:
                self._start_iteration()
            else:
                raise IndexError(f"Ros2BagDataset reads frames in order. Next frame is "
                                 f"{self._next}, not {idx}.")
        connection, _stamp, raw = next(self._messages)
        self._next += 1
        message = self._reader.deserialize(raw, connection.msgtype)
        return self._read_cloud(message)

    def _warn_once(self, key, text):
        if key not in self._warned:
            self._warned.add(key)
            print(f"  WARNING: {text}")

    def _read_cloud(self, message):
        fields = {f.name: f for f in message.fields}
        for name in ("x", "y", "z"):
            if name not in fields:
                raise RuntimeError(f"The PointCloud2 topic '{self.topic}' has no field '{name}'.")
        time_name = next((f.name for f in message.fields if f.name in TIME_FIELD_NAMES), None)
        order = ">" if message.is_bigendian else "<"
        wanted = ["x", "y", "z"] + ([time_name] if time_name else [])
        dtype = np.dtype({
            "names": wanted,
            "formats": [order + _POINTFIELD_TYPES[fields[n].datatype] for n in wanted],
            "offsets": [int(fields[n].offset) for n in wanted],
            "itemsize": int(message.point_step)})
        count = int(message.width) * int(message.height)
        data = np.frombuffer(np.ascontiguousarray(message.data), dtype=dtype, count=count)
        xyz = np.column_stack([data["x"], data["y"], data["z"]]).astype(np.float64)

        keep = np.isfinite(xyz).all(axis=1)
        self.nonfinite_points_dropped += int((~keep).sum())
        if self.drop_zero_points:
            zero = (xyz == 0.0).all(axis=1) & keep
            self.zero_points_dropped += int(zero.sum())
            keep &= ~zero
        xyz = xyz[keep]
        self.points_kept += len(xyz)

        timestamps = np.array([])
        if time_name is None:
            self._warn_once("no_time", f"Topic '{self.topic}' has no time field "
                            f"({', '.join(TIME_FIELD_NAMES)}). KISS-ICP runs WITHOUT deskew.")
        else:
            self.time_field = time_name
            raw_time = data[time_name][keep]
            usable, note = True, None
            if raw_time.size == 0:
                usable = False
            elif not np.isfinite(raw_time.astype(np.float64)).all():
                usable, note = False, "the time field has values that are not numbers"
            elif data.dtype[time_name].kind == "f" and data.dtype[time_name].itemsize == 4 \
                    and np.abs(raw_time).max() >= _FLOAT32_TIME_LIMIT:
                usable, note = False, ("the time field is float32 with an absolute time. "
                                       "A float32 cannot hold the fraction of a second")
            if usable:
                t = raw_time.astype(np.float64)
                span = float(t.max() - t.min())
                if self.first_frame_time_span is None:
                    self.first_frame_time_span = span
                    print(f"  Time field '{time_name}': {data.dtype[time_name]}, "
                          f"{span:.6f} time units across the first frame.")
                if span > 0.0:
                    timestamps = t - t.min()
                else:
                    usable, note = False, "all points of the frame have the same time"
            if not usable:
                self.frames_without_time += 1
                if note:
                    self._warn_once("bad_time", f"{note}. KISS-ICP runs WITHOUT deskew "
                                    f"for the affected frames.")
        return xyz, timestamps

    def summary_lines(self):
        lines = [f"ROS2 bag reader: topic '{self.topic}', rosbags {self.reader_version}, "
                 f"{self.points_kept} points read."]
        if self.drop_zero_points:
            lines.append(f"  Removed {self.zero_points_dropped} points at exactly (0, 0, 0) "
                         f"(returns that did not come back).")
        if self.nonfinite_points_dropped:
            lines.append(f"  Removed {self.nonfinite_points_dropped} points that were not finite.")
        if self.frames_without_time:
            lines.append(f"  {self.frames_without_time} of {self.n_frames} frames had no usable "
                         f"point time: no deskew for those frames.")
        elif self.time_field:
            lines.append(f"  Deskew used the time field '{self.time_field}' for every frame.")
        return lines


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


def build_dataset(source, dataloader, meta=None, topic=None, drop_zero_points=True):
    # A ROS2 bag folder is read by this script (see the module docstring).
    # "rosbag-kiss" forces the kiss-icp loader, as before 2026-10-16.
    if dataloader == "rosbag" and is_ros2_bag_folder(source):
        return Ros2BagDataset(source, topic=topic, drop_zero_points=drop_zero_points)
    if dataloader == "rosbag-kiss":
        dataloader = "rosbag"

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


def organised_layout(timestamps, n_points, min_rows=8, max_rows=512, min_columns=64):
    """
    Finds the row and the column of each point of one frame from the time stamps.
    Returns (rows, columns, n_rows, n_columns), or None when the frame does not
    have the layout that the flying-pixel filter needs. See the module docstring.

    The points of the frame must follow in the order row after row. The time stamp
    must be the same for all points of a column and rise from the first to the
    last column of a row. The time stamp falls back at the start of each row.

    The layout is refused when: there is no time stamp for each point, there are
    fewer than min_rows or more than max_rows rows, there are fewer than
    min_columns columns, or two points have the same row and column.
    """
    t = np.asarray(timestamps, dtype=np.float64).reshape(-1)
    if len(t) != n_points or n_points < min_rows * min_columns:
        return None
    if not np.isfinite(t).all():
        return None
    rows = np.concatenate([[0], np.cumsum(np.diff(t) < 0)]).astype(np.int64)
    n_rows = int(rows[-1]) + 1
    if n_rows < min_rows or n_rows > max_rows:
        return None
    unique_t, columns = np.unique(t, return_inverse=True)
    n_columns = len(unique_t)
    if n_columns < min_columns:
        return None
    cell = rows * n_columns + columns
    if len(np.unique(cell)) != n_points:
        return None
    return rows, columns.astype(np.int64), n_rows, n_columns


def flying_pixel_mask(points, timestamps, abs_tolerance=0.10, rel_tolerance=0.02):
    """
    Flags the flying pixels of one frame.

    points: (N, 3), in the frame of the sensor. timestamps: (N,), see
    organised_layout(). Returns a boolean array (True = flying pixel), or None
    when the layout is not recognised (nothing is flagged then).

    A point is a flying pixel when its range differs from the range of BOTH
    neighbours on one line (the row, or the column) by more than T = abs_tolerance
    + rel_tolerance x range, AND the point is not on the straight line between its
    neighbours (|r_before + r_after - 2 r| > T). The second part keeps the points
    of a smooth slope: a floor or a wall that the beam crosses at a small angle
    has a large range step between two beams, and the step is the same on both
    sides. A missing neighbour (no return) is not a difference, so a point at the
    end of a line, or next to a gap, stays.
    """
    points = np.asarray(points, dtype=np.float64)
    layout = organised_layout(timestamps, len(points))
    if layout is None:
        return None
    rows, columns, n_rows, n_columns = layout
    ranges = np.linalg.norm(points, axis=1)
    image = np.full((n_rows, n_columns), np.nan)
    image[rows, columns] = ranges
    tolerance = abs_tolerance + rel_tolerance * image
    flag = np.zeros(image.shape, dtype=bool)
    with np.errstate(invalid="ignore"):
        before = np.full_like(image, np.nan)
        after = np.full_like(image, np.nan)
        before[:, 1:] = image[:, :-1]
        after[:, :-1] = image[:, 1:]
        flag |= ((np.abs(image - before) > tolerance) & (np.abs(image - after) > tolerance)
                 & (np.abs(before + after - 2.0 * image) > tolerance))
        before = np.full_like(image, np.nan)
        after = np.full_like(image, np.nan)
        before[1:] = image[:-1]
        after[:-1] = image[1:]
        flag |= ((np.abs(image - before) > tolerance) & (np.abs(image - after) > tolerance)
                 & (np.abs(before + after - 2.0 * image) > tolerance))
    return flag[rows, columns]


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
                              "generic, kitti, etc.) instead of auto-detecting from --input. "
                              "A ROS2 bag folder with 'rosbag' is read by this script's own "
                              "reader; 'rosbag-kiss' uses the kiss-icp rosbag loader instead.")
    parser.add_argument("--keep-zero-points", action="store_true",
                         help="ROS2 bag folders only: keep the points at exactly (0, 0, 0). "
                              "By default they are removed. They are returns that did not come "
                              "back, and each one would put a point at the sensor position in "
                              "the map.")
    parser.add_argument("--config", default=None,
                         help="Path to a kiss-icp config YAML (generate a starting one with "
                              "'kiss_icp_dump_config') - use this to set voxel size and other "
                              "tuning parameters, which aren't exposed as flags here")
    parser.add_argument("--max-range", type=float, default=None,
                         help="Override max sensor range from the config (registration "
                              "range). Takes priority over --map-max-range for the "
                              "registration range.")
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
                         help="Maximum range in meters, from the sensor at the time of each "
                              "frame. Replaces the config's data.max_range, so it sets the "
                              "registration range AND the range of the saved map. If "
                              "--max-range is also given, --max-range sets the registration "
                              "range and this option only crops the saved map.")
    parser.add_argument("--map-max-horizontal", type=float, default=None,
                         help="Remove points whose HORIZONTAL distance from the sensor "
                              "(meters, map frame) is more than this, from the saved map only.")
    parser.add_argument("--map-max-vertical", type=float, default=None,
                         help="Remove points whose VERTICAL distance from the sensor "
                              "(meters, map frame, above or below) is more than this, from "
                              "the saved map only.")
    parser.add_argument("--local-map", action="store_true",
                         help="Save kiss-icp's local map instead of the global map of all "
                              "frames. The local map only holds points within data.max_range "
                              "of the robot's FINAL position (the old default of this script). "
                              "Cannot be used with the --map-max-* limits.")
    parser.add_argument("--flying-pixel-filter", action="store_true",
                         help="Remove flying pixels (false points between a near and a far "
                              "surface) from every frame before SLAM, with the range image of "
                              "the scan. The rows and columns come from the time stamps of the "
                              "points. A frame without a usable layout is not filtered. See "
                              "the module docstring. Default: off")
    parser.add_argument("--flying-pixel-abs", type=float, default=0.10,
                         help="Flying-pixel filter: the fixed part of the range difference "
                              "limit, in meters. Default: 0.10")
    parser.add_argument("--flying-pixel-rel", type=float, default=0.02,
                         help="Flying-pixel filter: the part of the limit that grows with the "
                              "range (a fraction of the range of the point). Default: 0.02")
    args = parser.parse_args()

    if args.flying_pixel_filter and (args.flying_pixel_abs < 0 or args.flying_pixel_rel < 0
                                      or args.flying_pixel_abs + args.flying_pixel_rel <= 0):
        print("ERROR: --flying-pixel-abs and --flying-pixel-rel must not be negative, and "
              "they must not both be 0.")
        return 1

    for flag, value in (("--map-max-range", args.map_max_range),
                        ("--map-max-horizontal", args.map_max_horizontal),
                        ("--map-max-vertical", args.map_max_vertical)):
        if value is not None and value <= 0:
            print(f"ERROR: {flag} must be greater than 0.")
            return 1
    any_limit = any(v is not None for v in (
        args.map_max_range, args.map_max_horizontal, args.map_max_vertical))
    if args.local_map and any_limit:
        print("ERROR: --local-map cannot be used with --map-max-range, "
              "--map-max-horizontal or --map-max-vertical. The local map is not built "
              "from the registered frames, so these limits do not apply to it.")
        return 1
    use_global_map = not args.local_map

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
    try:
        dataset = build_dataset(args.input, dataloader, meta=args.meta, topic=args.topic,
                                drop_zero_points=not args.keep_zero_points)
    except RuntimeError as e:
        print(f"ERROR: {e}")
        return 1
    if isinstance(dataset, Ros2BagDataset):
        print(f"  ROS2 bag read by this script (rosbags {dataset.reader_version}), "
              f"topic '{dataset.topic}'.")
    n_frames = len(dataset)
    print(f"  {n_frames} frames found.")
    if n_frames == 0:
        print("ERROR: no frames found - check --input and --dataloader.")
        return 1

    # --map-max-range replaces the config's data.max_range (registration range)
    # unless --max-range is given. See the module docstring (2026-10-15).
    registration_range = args.max_range if args.max_range is not None else args.map_max_range
    config = load_config(args.config, max_range=registration_range)
    if registration_range is not None:
        source_flag = "--max-range" if args.max_range is not None else "--map-max-range"
        print(f"  Registration max range set to {config.data.max_range} m by {source_flag} "
              f"(overrides the config's data.max_range).")
    else:
        print(f"  Registration max range: {config.data.max_range} m (from the config).")

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
    if use_global_map:
        from kiss_icp.mapping import VoxelHashMap
        from kiss_icp.voxelization import voxel_down_sample
        if args.map_max_range is not None and args.map_max_range > config.data.max_range:
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
        limits = []
        if args.map_max_range is not None:
            limits.append(f"range {args.map_max_range} m")
        if args.map_max_horizontal is not None:
            limits.append(f"horizontal {args.map_max_horizontal} m")
        if args.map_max_vertical is not None:
            limits.append(f"vertical {args.map_max_vertical} m")
        print("  Saving the global map of all frames (not pruned around the final position).")
        print(f"  Map limits: {', '.join(limits) if limits else 'none'} "
              f"(registration range is data.max_range = {config.data.max_range} m)")
    else:
        print(f"  Saving the kiss-icp local map: only points within data.max_range "
              f"({config.data.max_range} m) of the robot's FINAL position (--local-map).")

    print("Running odometry...")
    report_every = max(1, n_frames // 20)
    kept_points = 0
    dropped_points = 0
    fp_removed = 0
    fp_points = 0
    fp_filtered_frames = 0
    fp_skipped_frames = 0
    if args.flying_pixel_filter:
        print(f"  Flying-pixel filter on: limit {args.flying_pixel_abs} m + "
              f"{args.flying_pixel_rel} x range.")
    for idx in range(n_frames):
        raw_frame, timestamps = dataset[idx]
        if args.flying_pixel_filter:
            flying = flying_pixel_mask(raw_frame, timestamps,
                                       args.flying_pixel_abs, args.flying_pixel_rel)
            if flying is None:
                fp_skipped_frames += 1
            else:
                fp_filtered_frames += 1
                fp_points += len(flying)
                fp_removed += int(flying.sum())
                if flying.any():
                    keep = ~flying
                    raw_frame = np.asarray(raw_frame)[keep]
                    timestamps = np.asarray(timestamps)[keep]
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
                in_range &= np.hypot(offset[:, 0], offset[:, 1]) <= args.map_max_horizontal
            if args.map_max_vertical is not None:
                in_range &= np.abs(offset[:, 2]) <= args.map_max_vertical
            kept_points += int(in_range.sum())
            dropped_points += int((~in_range).sum())
            if in_range.any():
                world = offset[in_range] + pose[:3, 3]
                global_map.add_points(np.ascontiguousarray(world, dtype=np.float64))
        if (idx + 1) % report_every == 0 or idx == n_frames - 1:
            print(f"  {idx + 1}/{n_frames} frames processed")

    if isinstance(dataset, Ros2BagDataset):
        for line in dataset.summary_lines():
            print(line)

    if args.flying_pixel_filter:
        if fp_filtered_frames:
            share = 100.0 * fp_removed / fp_points if fp_points else 0.0
            print(f"Flying-pixel filter: removed {fp_removed} of {fp_points} points "
                  f"({share:.2f} %) in {fp_filtered_frames} frames.")
        if fp_skipped_frames:
            print(f"{'WARNING: ' if fp_skipped_frames > n_frames // 2 else 'NOTE: '}"
                  f"{fp_skipped_frames} of {n_frames} frames were NOT filtered, because the "
                  f"rows and columns of the scan could not be found from the time stamps "
                  f"(no time stamps, a different point order, or points missing). "
                  f"The map of these frames has all its flying pixels.")

    if global_map is not None:
        total = kept_points + dropped_points
        if any_limit:
            pct = (100.0 * dropped_points / total) if total else 0.0
            print(f"Map limits removed {dropped_points} of {total} downsampled frame points "
                  f"({pct:.1f}%) before they reached the saved map.")
        print("Extracting the accumulated map from the global map...")
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
        print("\nNOTE: voxel size and other tuning parameters come from kiss-icp's own "
              "config (default, or --config if given) - not overridden here. Run "
              "'kiss_icp_dump_config' to generate an editable starting config file if you need "
              "to change these, or pass --voxel-size to override just that one value.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
