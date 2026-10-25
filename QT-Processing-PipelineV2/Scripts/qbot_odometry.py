#!/usr/bin/env python3
"""
qbot_odometry.py
================
Standalone helper for Stage 1 (SLAM). It finds the wheel and gyro data of the
Quanser QBot in a ROS2 bag, compares them with the SLAM path, and can give the
data to KISS-ICP as a motion prior. It is used by slam_kiss_icp.py. It can also
run by itself (see Usage). Backward compatible: slam_kiss_icp.py does not need it
unless a bag has QBot topics.

Needs: numpy and rosbags (pip install rosbags). It does not need kiss-icp.

WHAT IT READS
    /qbot_imu             sensor_msgs/msg/Imu              gyro (angular_velocity.z)
    /qbot_speed_feedback  geometry_msgs/msg/TwistStamped   forward speed (linear.x)
A topic is used when its type is right AND its name has the word "qbot" (the name
test keeps out the IMU of the lidar, which is in the lidar frame, and the
commanded speed /cmd_vel). Other topics can be named with --imu-topic and
--speed-topic. Topics /qbot_joint (wheel angles) and the angular.z of the twist are
NOT used: on the two real bags the angular.z had the opposite sign of the gyro, and
the wheel angles had no known unit.

WHAT IT DOES
    1. Find the topics (find_topics) and read them (read_odometry).
    2. Make a path of the robot from the speed and the yaw of the gyro
       (OdometryTrack). This is dead reckoning. It drifts. The only use of it is
       the motion between two lidar frames, which is short.
    3. check_against_poses: compare that path with the SLAM poses. It measures how
       well the two agree (yaw change per frame, travelled distance, direction of
       travel, and the mount yaw). It changes nothing. The result is a report.
    4. OdometryTrack.relative_transform(t0, t1): the motion of the sensor between
       two times, in the frame of the sensor at t0. slam_kiss_icp.py puts it in
       KISS-ICP as the starting guess for the next frame (--odometry-prior qbot).
    5. OdometryTrack.pose_path / odometry_path_in_slam_frame: the odometry-only path of
       the sensor (no SLAM) as a poses file in TUM format (--save-odometry). The path
       is planar: x, y and yaw come from the data. z, roll and pitch stay as they are
       at the first frame. It is dead reckoning, so it drifts. Use it to compare with
       the SLAM path, or as a second source for a later stage (see frame_chain.py).

FRAMES (measured on the two real bags of 2026-10-20, sensor in the middle of the
QBot, Hesai XT32)
    The robot drives forward along the -y axis of the lidar. So the x axis of the
    robot (forward) is the direction -90 degrees in the x-y plane of the lidar.
    That is the default of --mount-yaw-deg (-90). The check below measures the
    direction of travel and says if the value does not fit. The sensor must be in
    the middle of the robot, with its z axis up. There is no lever arm in the
    calculation.

LIMITS (read this)
    - Dead reckoning drifts. Do not use it as a map. Use it for one frame.
    - At 20 Hz, KISS-ICP alone is already good. On the two real bags the prior
      changed the path by 1.5 cm or less (see CHANGES.md). The prior only matters
      when the robot turns much between two frames (more than about 10 degrees),
      for example when the frame rate is low.
    - The prior also sets the speed that KISS-ICP uses for deskew.
    - Not checked: a full run (drift over minutes), a different robot, and a
      sensor that is not in the middle of the robot.

Usage:
    python qbot_odometry.py --bag BAG_FOLDER                  (list what the bag has)
    python qbot_odometry.py --bag BAG_FOLDER --poses P.txt    (compare with a SLAM path)
    python qbot_odometry.py --bag BAG_FOLDER --poses P.txt --save-odometry O.txt
                                          (also write the odometry-only path at the
                                           times of P.txt, in the frame of P.txt)
P.txt is the file that slam_kiss_icp.py writes with --save-poses (TUM format).
"""

import argparse
import math
import sys
from pathlib import Path

import numpy as np

IMU_TYPE = "sensor_msgs/msg/Imu"
SPEED_TYPE = "geometry_msgs/msg/TwistStamped"
JOINT_TYPE = "sensor_msgs/msg/JointState"
ODOM_TYPE = "nav_msgs/msg/Odometry"

DEFAULT_MOUNT_YAW_DEG = -90.0
# The odometry must cover the time between two frames. A gap larger than this
# (seconds) in the data means that no prior is made for that frame.
DEFAULT_MAX_GAP = 0.25

# Limits of the check. These are the judgement of the author, not a standard. The
# check only prints a warning. It never changes a result.
CHECK_MIN_FRAMES = 20
CHECK_MIN_MOTION_SPEED = 0.15            # m/s, for the direction of travel
CHECK_YAW_TOLERANCE_DEG = 3.0            # net yaw difference allowed (at least) ...
CHECK_YAW_TOLERANCE_SHARE = 0.05         # ... or this share of the net turn
CHECK_DISTANCE_RATIO = (0.85, 1.15)      # SLAM distance / odometry distance
CHECK_MOUNT_TOLERANCE_DEG = 15.0


# ---------------------------------------------------------------------------
# Reading the bag
# ---------------------------------------------------------------------------

def _open_reader(bag_dir):
    """AnyReader for a ROS2 bag folder (rosbags 0.9.x and 0.10 or newer)."""
    try:
        from rosbags.highlevel import AnyReader
    except ImportError as error:
        raise RuntimeError(f"Reading a ROS2 bag needs the 'rosbags' package ({error}). "
                           f"Run: pip install rosbags") from error
    paths = [Path(bag_dir)]
    try:
        from rosbags.typesys import Stores, get_typestore       # rosbags 0.10 and newer
    except ImportError:
        reader = AnyReader(paths)                               # rosbags 0.9.x
    else:
        reader = AnyReader(paths, default_typestore=get_typestore(Stores.ROS2_HUMBLE))
    reader.open()
    return reader


def _is_bag_folder(path):
    path = Path(path)
    return path.is_dir() and (path / "metadata.yaml").exists()


def find_topics(connections, imu_topic=None, speed_topic=None):
    """
    Chooses the topics. `connections` is a list of (name, message type, count).
    Returns a dict:
        imu, speed        the chosen topic names, or None
        joint             the name of a JointState topic with "qbot" in the name, or
                          None (listed in the report, never used)
        not_used          the names of Imu and TwistStamped topics that were NOT
                          chosen (for the report)
    A topic given with imu_topic or speed_topic is used as it is, if the bag has it.
    """
    by_name = {name: (kind, count) for name, kind, count in connections}

    def pick(kind, given, words):
        if given:
            return given if given in by_name else None
        candidates = [(name, count) for name, (k, count) in by_name.items()
                      if k == kind and "qbot" in name.lower()
                      and not any(w in name.lower() for w in ("cmd", "command"))]
        if words:
            preferred = [c for c in candidates if any(w in c[0].lower() for w in words)]
            candidates = preferred or candidates
        candidates.sort(key=lambda c: -(c[1] or 0))
        return candidates[0][0] if candidates else None

    imu = pick(IMU_TYPE, imu_topic, ("imu",))
    speed = pick(SPEED_TYPE, speed_topic, ("speed", "odom", "twist", "vel"))
    joint = next((n for n, (k, _c) in by_name.items()
                  if k == JOINT_TYPE and "qbot" in n.lower()), None)
    used = {imu, speed}
    not_used = sorted(n for n, (k, _c) in by_name.items()
                      if k in (IMU_TYPE, SPEED_TYPE, ODOM_TYPE) and n not in used)
    return {"imu": imu, "speed": speed, "joint": joint, "not_used": not_used}


def list_bag_topics(bag_dir):
    """[(name, type, count)] of a ROS2 bag folder. [] when it is not a bag or cannot
    be read. Never raises."""
    if not _is_bag_folder(bag_dir):
        return []
    try:
        reader = _open_reader(bag_dir)
    except Exception:
        return []
    try:
        return [(c.topic, c.msgtype, int(c.msgcount)) for c in reader.connections]
    except Exception:
        return []
    finally:
        try:
            reader.close()
        except Exception:
            pass


def _stamp_seconds(message, bag_time_ns):
    stamp = getattr(getattr(message, "header", None), "stamp", None)
    if stamp is not None and (int(stamp.sec) != 0 or int(stamp.nanosec) != 0):
        return int(stamp.sec) + int(stamp.nanosec) * 1e-9
    return bag_time_ns * 1e-9


class OdometryData:
    """The raw samples. All times are seconds on the clock of the message headers
    (the same clock as the lidar header, checked on the two real bags)."""

    def __init__(self, imu_t, gyro, speed_t, speed, speed_wz, topics):
        self.imu_t = np.asarray(imu_t, dtype=np.float64)
        self.gyro = np.asarray(gyro, dtype=np.float64).reshape(-1, 3)
        self.speed_t = np.asarray(speed_t, dtype=np.float64)
        self.speed = np.asarray(speed, dtype=np.float64)
        self.speed_wz = np.asarray(speed_wz, dtype=np.float64)
        self.topics = topics

    @property
    def usable(self):
        return len(self.imu_t) >= 3 and len(self.speed_t) >= 3

    def rates(self):
        def rate(t):
            return (len(t) - 1) / (t[-1] - t[0]) if len(t) > 1 and t[-1] > t[0] else 0.0
        return rate(self.imu_t), rate(self.speed_t)


def read_odometry(bag_dir, imu_topic=None, speed_topic=None):
    """
    Reads the gyro and the speed from a ROS2 bag folder.
    Returns OdometryData, or None when the bag has no usable pair of topics.
    Raises RuntimeError when rosbags is missing or the bag cannot be opened.
    """
    if not _is_bag_folder(bag_dir):
        return None
    reader = _open_reader(bag_dir)
    try:
        connections = [(c.topic, c.msgtype, int(c.msgcount)) for c in reader.connections]
        topics = find_topics(connections, imu_topic, speed_topic)
        if not topics["imu"] or not topics["speed"]:
            return None
        by_topic = {c.topic: c for c in reader.connections}
        imu_t, gyro, speed_t, speed, speed_wz = [], [], [], [], []
        for connection, bag_time, raw in reader.messages(
                connections=[by_topic[topics["imu"]], by_topic[topics["speed"]]]):
            message = reader.deserialize(raw, connection.msgtype)
            if connection.topic == topics["imu"]:
                w = message.angular_velocity
                imu_t.append(_stamp_seconds(message, bag_time))
                gyro.append((float(w.x), float(w.y), float(w.z)))
            else:
                twist = message.twist
                speed_t.append(_stamp_seconds(message, bag_time))
                speed.append(float(twist.linear.x))
                speed_wz.append(float(twist.angular.z))
    finally:
        try:
            reader.close()
        except Exception:
            pass
    data = OdometryData(imu_t, gyro, speed_t, speed, speed_wz, topics)
    order_i = np.argsort(data.imu_t, kind="stable")
    order_s = np.argsort(data.speed_t, kind="stable")
    data.imu_t, data.gyro = data.imu_t[order_i], data.gyro[order_i]
    data.speed_t, data.speed, data.speed_wz = (
        data.speed_t[order_s], data.speed[order_s], data.speed_wz[order_s])
    return data if data.usable else None


# ---------------------------------------------------------------------------
# The path of the robot
# ---------------------------------------------------------------------------

def _rot2(angle):
    c, s = math.cos(angle), math.sin(angle)
    return np.array([[c, -s], [s, c]])


class OdometryTrack:
    """Dead reckoning from the speed (linear.x) and the yaw rate of the gyro (z).

    The path is made at the times of the speed samples: yaw = sum of gyro z x dt,
    then x and y from the speed in the direction of the yaw. The origin is the
    first speed sample. Only differences are used, so the origin does not matter.
    """

    def __init__(self, data, mount_yaw_deg=DEFAULT_MOUNT_YAW_DEG, max_gap=DEFAULT_MAX_GAP):
        self.data = data
        self.mount_yaw_deg = float(mount_yaw_deg)
        self.max_gap = float(max_gap)
        # Columns: the robot x axis (forward) and y axis (left), in lidar coordinates.
        self.rlb = _rot2(math.radians(self.mount_yaw_deg))
        t = data.speed_t
        dt = np.r_[0.0, np.diff(t)]
        wz = np.interp(t, data.imu_t, data.gyro[:, 2])
        self.t = t
        self.yaw = np.cumsum(wz * dt)
        self.x = np.cumsum(data.speed * np.cos(self.yaw) * dt)
        self.y = np.cumsum(data.speed * np.sin(self.yaw) * dt)
        self.speed = data.speed
        # Positions of the gaps (larger than max_gap) in the two series, so that
        # covers() does not search the data again for each frame.
        self._speed_gaps = np.flatnonzero(np.diff(self.t) > self.max_gap)
        self._imu_gaps = np.flatnonzero(np.diff(data.imu_t) > self.max_gap)

    def covers(self, t0, t1):
        """True if both the speed and the gyro have samples around t0 and t1 with no
        gap larger than max_gap in between."""
        for times, gaps in ((self.t, self._speed_gaps), (self.data.imu_t, self._imu_gaps)):
            if t0 < times[0] or t1 > times[-1]:
                return False
            i0 = max(int(np.searchsorted(times, t0, side="right")) - 1, 0)
            i1 = min(int(np.searchsorted(times, t1, side="left")), len(times) - 1)
            # A gap g is the space between sample g and sample g + 1.
            if i1 > i0 and np.searchsorted(gaps, i0, side="left") < np.searchsorted(
                    gaps, i1, side="left"):
                return False
        return True

    def yaw_at(self, times):
        return np.interp(times, self.t, self.yaw)

    def relative_transform(self, t0, t1):
        """
        The motion of the sensor from time t0 to time t1, as a 4x4 matrix in the frame
        of the sensor at t0 (the form that KISS-ICP uses for last_delta). Returns None
        when t1 <= t0 or the data does not cover the interval.
        """
        if not t1 > t0 or not self.covers(t0, t1):
            return None
        x0, x1 = np.interp([t0, t1], self.t, self.x)
        y0, y1 = np.interp([t0, t1], self.t, self.y)
        th0, th1 = np.interp([t0, t1], self.t, self.yaw)
        c, s = math.cos(th0), math.sin(th0)
        dx, dy = x1 - x0, y1 - y0
        d_robot = np.array([c * dx + s * dy, -s * dx + c * dy])
        rb = _rot2(th1 - th0)
        transform = np.eye(4)
        transform[:2, :2] = self.rlb @ rb @ self.rlb.T
        transform[:2, 3] = self.rlb @ d_robot
        return transform

    def pose_path(self, times, anchor=None):
        """
        The odometry-only path of the sensor: a pose for each time in `times`, from
        dead reckoning only (no SLAM). Returns (indices, poses). `indices` are the
        positions in `times` of the frames that the path has (a frame outside the
        time range of the data, or after a gap in the data, has no pose and is left
        out). `poses` is a list of 4x4 matrices, the pose of the sensor in the same
        kind of frame as a SLAM pose file: the origin is the first frame that has a
        pose. That pose is `anchor` (a 4x4 matrix, default the identity). To put the
        path in the frame of a SLAM path, give the SLAM pose of that first frame as
        `anchor`; then both paths have the same origin and the same frame.

        Each pose is the motion from the first frame to that frame, calculated in one
        step (not a sum of small steps), so the numerical error does not grow.
        """
        times = np.asarray(times, dtype=np.float64)
        anchor = np.eye(4) if anchor is None else np.asarray(anchor, dtype=np.float64)
        indices, poses = [], []
        first_time = None
        for k, t in enumerate(times):
            if not math.isfinite(t):
                continue
            if first_time is None:
                # The first frame that both series (speed and gyro) have data for.
                if (max(self.t[0], self.data.imu_t[0]) <= t
                        <= min(self.t[-1], self.data.imu_t[-1])):
                    first_time = t
                    indices.append(k)
                    poses.append(anchor.copy())
                continue
            relative = self.relative_transform(first_time, t)
            if relative is None:
                continue
            indices.append(k)
            poses.append(anchor @ relative)
        return indices, poses


class OdometryPrior:
    """Gives the motion between the frame before and the present frame.
    Counts how many frames got a value and how many did not."""

    def __init__(self, track):
        self.track = track
        self.used = 0
        self.missing = 0

    def delta(self, previous_time, time):
        if previous_time is None or time is None:
            return None
        value = self.track.relative_transform(previous_time, time)
        if value is None:
            self.missing += 1
        else:
            self.used += 1
        return value


# ---------------------------------------------------------------------------
# Check against a SLAM path
# ---------------------------------------------------------------------------

def _wrap_deg(angle):
    return (angle + 180.0) % 360.0 - 180.0


def check_against_poses(track, frame_times, poses):
    """
    Compares the odometry with SLAM poses. `frame_times`: N times in seconds (same
    clock as the odometry). `poses`: N x 4 x 4 (sensor pose in the map, as KISS-ICP
    gives it). Only the yaw and the x-y position of the poses are used (a robot on a
    floor).

    Returns a dict, or None if there are fewer than CHECK_MIN_FRAMES frames that the
    odometry covers. Keys (angles in degrees, distances in metres):
        n_frames, duration
        slam_yaw_net, gyro_yaw_net     net yaw change of the two, over the frames
        yaw_net_difference
        yaw_step_rms_residual          rms of (SLAM yaw step - gyro yaw step) per frame
        yaw_step_slope, yaw_step_corr  fit of the SLAM step to the gyro step
        slam_distance, odom_distance, distance_ratio
        speed_slope, speed_corr        SLAM speed against |odometry speed|
        travel_direction_median        direction of travel in the sensor frame (the
                                       sign of the speed is used; moving frames only)
        travel_direction_p10_p90
        n_moving
        mount_yaw_estimate             the direction of the robot x axis in the lidar
                                       frame, from the travel direction (None when
                                       there are too few moving frames)
        mount_yaw_used
        mount_yaw_difference
        findings                       list of (level, text), level "OK" or "WARNING"
    """
    times = np.asarray(frame_times, dtype=np.float64)
    poses = np.asarray(poses, dtype=np.float64)
    ok = (times >= track.t[0]) & (times <= track.t[-1]) & np.isfinite(times)
    # Use the frames that are inside the time range of the odometry.
    if ok.sum() < CHECK_MIN_FRAMES:
        return None
    idx = np.flatnonzero(ok)
    times, poses = times[idx], poses[idx]
    n = len(times)

    yaw = np.unwrap(np.arctan2(poses[:, 1, 0], poses[:, 0, 0]))
    gyro_yaw = track.yaw_at(times)
    d_slam = np.diff(yaw)
    d_gyro = np.diff(gyro_yaw)
    result = {"n_frames": n, "duration": float(times[-1] - times[0])}
    result["slam_yaw_net"] = math.degrees(yaw[-1] - yaw[0])
    result["gyro_yaw_net"] = math.degrees(gyro_yaw[-1] - gyro_yaw[0])
    result["yaw_net_difference"] = result["slam_yaw_net"] - result["gyro_yaw_net"]
    if np.std(d_gyro) > 1e-9 and np.std(d_slam) > 1e-9:
        slope = float(np.linalg.lstsq(d_gyro[:, None], d_slam, rcond=None)[0][0])
        corr = float(np.corrcoef(d_gyro, d_slam)[0, 1])
    else:
        slope, corr = float("nan"), float("nan")
    result["yaw_step_slope"], result["yaw_step_corr"] = slope, corr
    result["yaw_step_rms_residual"] = math.degrees(float(np.std(d_slam - d_gyro)))

    p = poses[:, :2, 3]
    steps = np.diff(p, axis=0)
    step_len = np.linalg.norm(steps, axis=1)
    frame_dt = np.diff(times)
    mid = 0.5 * (times[1:] + times[:-1])
    v_mid = np.interp(mid, track.t, track.speed)
    slam_speed = step_len / np.where(frame_dt > 0, frame_dt, np.nan)
    sel = np.isfinite(slam_speed)
    speed_slope = float(np.linalg.lstsq(np.abs(v_mid[sel])[:, None], slam_speed[sel],
                                        rcond=None)[0][0]) if sel.any() else float("nan")
    speed_corr = float(np.corrcoef(np.abs(v_mid[sel]), slam_speed[sel])[0, 1]) \
        if sel.sum() > 2 and np.std(slam_speed[sel]) > 0 and np.std(v_mid[sel]) > 0 \
        else float("nan")
    result["speed_slope"], result["speed_corr"] = speed_slope, speed_corr
    result["slam_distance"] = float(step_len.sum())
    result["odom_distance"] = float(np.sum(np.abs(v_mid) * frame_dt))
    result["distance_ratio"] = (result["slam_distance"] / result["odom_distance"]
                                if result["odom_distance"] > 0.05 else float("nan"))

    # Direction of travel in the sensor frame at the start of each step.
    angles = []
    for k in range(n - 1):
        if abs(v_mid[k]) > CHECK_MIN_MOTION_SPEED and slam_speed[k] > CHECK_MIN_MOTION_SPEED * 0.5:
            local = poses[k, :2, :2].T @ steps[k]
            angle = math.degrees(math.atan2(local[1], local[0]))
            if v_mid[k] < 0:
                angle += 180.0
            angles.append(_wrap_deg(angle))
    result["n_moving"] = len(angles)
    result["mount_yaw_used"] = track.mount_yaw_deg
    if len(angles) >= CHECK_MIN_FRAMES:
        a = np.array(angles)
        # Circular median: the angle with the smallest summed distance.
        reference = float(np.median(a))
        a = np.array([_wrap_deg(x - reference) for x in a]) + reference
        median = _wrap_deg(float(np.median(a)))
        result["travel_direction_median"] = median
        result["travel_direction_p10_p90"] = (_wrap_deg(float(np.percentile(a, 10))),
                                              _wrap_deg(float(np.percentile(a, 90))))
        result["mount_yaw_estimate"] = median
        result["mount_yaw_difference"] = _wrap_deg(median - track.mount_yaw_deg)
    else:
        result["travel_direction_median"] = None
        result["travel_direction_p10_p90"] = None
        result["mount_yaw_estimate"] = None
        result["mount_yaw_difference"] = None

    findings = []
    tol = max(CHECK_YAW_TOLERANCE_DEG, CHECK_YAW_TOLERANCE_SHARE * abs(result["gyro_yaw_net"]))
    if abs(result["yaw_net_difference"]) <= tol:
        findings.append(("OK", f"The net turn of SLAM ({result['slam_yaw_net']:+.1f} deg) agrees "
                               f"with the gyro ({result['gyro_yaw_net']:+.1f} deg)."))
    else:
        findings.append(("WARNING",
                         f"The net turn of SLAM ({result['slam_yaw_net']:+.1f} deg) does not agree "
                         f"with the gyro ({result['gyro_yaw_net']:+.1f} deg). SLAM can have drifted "
                         f"in yaw, or the gyro has a bias, or the clocks differ."))
    ratio = result["distance_ratio"]
    if math.isnan(ratio):
        findings.append(("OK", "The robot did not move far enough to compare the distance."))
    elif CHECK_DISTANCE_RATIO[0] <= ratio <= CHECK_DISTANCE_RATIO[1]:
        findings.append(("OK", f"The distance of SLAM ({result['slam_distance']:.2f} m) agrees "
                               f"with the wheel speed ({result['odom_distance']:.2f} m)."))
    else:
        findings.append(("WARNING",
                         f"The distance of SLAM ({result['slam_distance']:.2f} m) does not agree "
                         f"with the wheel speed ({result['odom_distance']:.2f} m). The ratio is "
                         f"{ratio:.2f}. SLAM can have slipped (a plain wall, a corridor), or the "
                         f"wheels slipped."))
    if result["mount_yaw_difference"] is None:
        findings.append(("OK", "Too few moving frames to check the mount yaw."))
    elif abs(result["mount_yaw_difference"]) <= CHECK_MOUNT_TOLERANCE_DEG:
        findings.append(("OK", f"The direction of travel ({result['mount_yaw_estimate']:+.0f} deg in "
                               f"the lidar frame) fits the mount yaw {track.mount_yaw_deg:+.0f} deg."))
    else:
        findings.append(("WARNING",
                         f"The direction of travel is {result['mount_yaw_estimate']:+.0f} deg in the "
                         f"lidar frame. The mount yaw is set to {track.mount_yaw_deg:+.0f} deg. "
                         f"Do not use the odometry prior with this value. Set --odometry-mount-yaw "
                         f"to {result['mount_yaw_estimate']:.0f}."))
    result["findings"] = findings
    return result


def report_lines(data, track, check):
    """The text for the run output. `check` can be None."""
    imu_rate, speed_rate = data.rates()
    lines = [
        f"QBot odometry: gyro '{data.topics['imu']}' ({len(data.imu_t)} samples, "
        f"{imu_rate:.1f} Hz), speed '{data.topics['speed']}' ({len(data.speed_t)} samples, "
        f"{speed_rate:.1f} Hz)."]
    if data.topics.get("joint"):
        lines.append(f"  Wheel topic '{data.topics['joint']}' is in the bag. It is not used.")
    if data.topics.get("not_used"):
        lines.append("  Other topics that are not used: " + ", ".join(data.topics["not_used"]) + ".")
    if check is None:
        lines.append(f"  Check against SLAM: not done. Fewer than {CHECK_MIN_FRAMES} frames are "
                     f"inside the time range of the odometry.")
        return lines
    lines.append(
        f"  Check against SLAM, {check['n_frames']} frames, {check['duration']:.1f} s: "
        f"yaw step residual {check['yaw_step_rms_residual']:.2f} deg per frame "
        f"(slope {check['yaw_step_slope']:.3f}, corr {check['yaw_step_corr']:.3f}); "
        f"speed slope {check['speed_slope']:.3f}.")
    for level, text in check["findings"]:
        lines.append(f"  {level}: {text}")
    return lines


def odometry_path_in_slam_frame(track, frame_times, slam_poses):
    """
    The odometry-only path at the frame times of a SLAM run, in the frame of the SLAM
    poses. The first frame that the odometry covers gets the SLAM pose of that frame
    as its pose. Then the two paths have the same origin and the same frame, and
    they can be compared or drawn together. Returns (indices, poses): `indices` are
    the positions of the frames that have a pose (see OdometryTrack.pose_path).
    Returns ([], []) when the odometry covers none of the frames.
    """
    indices, relative = track.pose_path(frame_times)
    if not indices:
        return [], []
    anchor = np.asarray(slam_poses[indices[0]], dtype=np.float64)
    return indices, [anchor @ pose for pose in relative]


# ---------------------------------------------------------------------------
# Poses file (TUM format: time x y z qx qy qz qw)
# ---------------------------------------------------------------------------

def matrix_to_quaternion(rotation):
    """Rotation matrix -> (qx, qy, qz, qw)."""
    m = np.asarray(rotation, dtype=np.float64)
    trace = m[0, 0] + m[1, 1] + m[2, 2]
    if trace > 0:
        s = math.sqrt(trace + 1.0) * 2
        w = 0.25 * s
        x, y, z = (m[2, 1] - m[1, 2]) / s, (m[0, 2] - m[2, 0]) / s, (m[1, 0] - m[0, 1]) / s
    elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
        s = math.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2
        w, x = (m[2, 1] - m[1, 2]) / s, 0.25 * s
        y, z = (m[0, 1] + m[1, 0]) / s, (m[0, 2] + m[2, 0]) / s
    elif m[1, 1] > m[2, 2]:
        s = math.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2
        w, y = (m[0, 2] - m[2, 0]) / s, 0.25 * s
        x, z = (m[0, 1] + m[1, 0]) / s, (m[1, 2] + m[2, 1]) / s
    else:
        s = math.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2
        w, z = (m[1, 0] - m[0, 1]) / s, 0.25 * s
        x, y = (m[0, 2] + m[2, 0]) / s, (m[1, 2] + m[2, 1]) / s
    q = np.array([x, y, z, w])
    return tuple(q / np.linalg.norm(q))


def quaternion_to_matrix(qx, qy, qz, qw):
    n = math.sqrt(qx * qx + qy * qy + qz * qz + qw * qw)
    x, y, z, w = qx / n, qy / n, qz / n, qw / n
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def write_tum(path, times, poses):
    """Writes the poses in TUM format. A line is: time x y z qx qy qz qw. `times` are
    seconds (absolute, when the source has a time) or frame numbers."""
    lines = []
    for t, pose in zip(times, poses):
        qx, qy, qz, qw = matrix_to_quaternion(pose[:3, :3])
        lines.append(f"{t:.9f} {pose[0, 3]:.9f} {pose[1, 3]:.9f} {pose[2, 3]:.9f} "
                     f"{qx:.9f} {qy:.9f} {qz:.9f} {qw:.9f}")
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)


def read_tum(path):
    """-> (times N, poses N x 4 x 4)."""
    rows = [line.split() for line in Path(path).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")]
    if not rows:
        raise ValueError(f"The poses file {path} has no lines.")
    values = np.array([[float(v) for v in row] for row in rows])
    if values.shape[1] != 8:
        raise ValueError("The poses file must be in TUM format (8 values on a line).")
    poses = np.tile(np.eye(4), (len(values), 1, 1))
    for i, row in enumerate(values):
        poses[i, :3, :3] = quaternion_to_matrix(*row[4:8])
        poses[i, :3, 3] = row[1:4]
    return values[:, 0], poses


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="List the QBot odometry in a ROS2 bag, and compare it with a SLAM "
                    "path. See the top of this file.")
    parser.add_argument("--bag", required=True, help="ROS2 bag folder (with metadata.yaml)")
    parser.add_argument("--poses", default=None,
                        help="Poses file in TUM format, from slam_kiss_icp.py --save-poses")
    parser.add_argument("--save-odometry", default=None, metavar="PATH",
                        help="Write the odometry-only path (from the gyro and the wheel speed, "
                             "no SLAM) to this file in TUM format. With --poses: at the times "
                             "of that file, in the frame of that file. Without: at the times of "
                             "the speed samples, with the first pose as the identity.")
    parser.add_argument("--imu-topic", default=None)
    parser.add_argument("--speed-topic", default=None)
    parser.add_argument("--mount-yaw-deg", type=float, default=DEFAULT_MOUNT_YAW_DEG,
                        help="Direction of the robot forward axis in the x-y plane of the "
                             "lidar, in degrees. Default: -90 (forward = lidar -y).")
    args = parser.parse_args(argv)

    if not _is_bag_folder(args.bag):
        print(f"ERROR: '{args.bag}' is not a ROS2 bag folder (no metadata.yaml).")
        return 1
    try:
        topics = list_bag_topics(args.bag)
        chosen = find_topics(topics, args.imu_topic, args.speed_topic)
        print("Topics: " + (", ".join(f"{n} ({k}, {c})" for n, k, c in topics) or "none"))
        data = read_odometry(args.bag, args.imu_topic, args.speed_topic)
    except RuntimeError as error:
        print(f"ERROR: {error}")
        return 1
    if data is None:
        print("No usable odometry in this bag. Need an Imu topic and a TwistStamped topic "
              f"with 'qbot' in the name (found: imu={chosen['imu']}, speed={chosen['speed']}). "
              "Use --imu-topic and --speed-topic to name other topics.")
        return 0
    track = OdometryTrack(data, args.mount_yaw_deg)
    check = None
    if args.poses:
        times, poses = read_tum(args.poses)
        check = check_against_poses(track, times, poses)
    for line in report_lines(data, track, check):
        print(line)
    if args.save_odometry:
        if args.poses:
            indices, path = odometry_path_in_slam_frame(track, times, poses)
            kept_times = times[indices]
        else:
            indices, path = track.pose_path(track.t)
            kept_times = track.t[indices]
        if not indices:
            print("ERROR: the odometry covers none of the times. No file is written.")
            return 1
        write_tum(args.save_odometry, kept_times, path)
        print(f"Saved {len(indices)} odometry poses to: {args.save_odometry}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
