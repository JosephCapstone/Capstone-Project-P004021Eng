"""ROS-aware mapping lifecycle worker used by the Windows DeltaUI_Joseph backend."""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

import rclpy
from ament_index_python.packages import get_package_share_directory
from cartographer_ros_msgs.srv import FinishTrajectory
from geometry_msgs.msg import PoseStamped, TwistStamped
from nav_msgs.msg import OccupancyGrid
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import Imu, LaserScan

from .mapping_core import (
    MapGeometry,
    mapping_scan_is_fresh,
    quaternion_yaw,
    render_occupancy_ppm,
    save_trinary_map,
    validate_map_name,
    validate_saved_map,
)


ACTIVE_MAPPING_STATES = {
    "starting",
    "mapping",
    "playback_complete",
    "finishing",
    "saving",
    "cancelling",
    "save_failed",
}


class MappingWorker(Node):
    def __init__(self, manage_visualization: bool = True) -> None:
        super().__init__("delta_mapping_worker")
        self._lock = threading.RLock()
        self._operation_lock = threading.Lock()
        self._operation_pending = False
        self._executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="delta")
        self._mapping_process: Optional[subprocess.Popen] = None
        self._playback_process: Optional[subprocess.Popen] = None
        self._visualization_process: Optional[subprocess.Popen] = None
        self._managed_visualization = False
        self._manage_visualization = manage_visualization
        self._trajectory_finished = False
        self._expected_mapping_stop = False
        self._latest_map: Optional[OccupancyGrid] = None
        self._pose_xy: Optional[tuple[float, float]] = None
        self._rendered_map: Optional[bytes] = None
        self._last_pose_render = 0.0
        self._topic_seen: dict[str, float] = {}
        self._node_names: set[str] = set()
        self._service_names: set[str] = set()
        self._event_id = 0
        self._events: list[dict[str, Any]] = []

        self._state: dict[str, Any] = {
            "worker": "running",
            "visualization": "starting" if manage_visualization else "unmanaged",
            "mapping": "ready",
            "source": "live",
            "playback_scan_ring": None,
            "detail": "Ready to start mapping",
            "error": None,
            "map_version": 0,
            "map": None,
            "pose": None,
            "process_running": False,
            "playback_running": False,
            "playback_complete": False,
            "finish_service_available": False,
            "trajectory_finished": False,
            "saved_paths": None,
            "events": self._events,
        }

        map_qos = QoSProfile(depth=1)
        map_qos.durability = DurabilityPolicy.TRANSIENT_LOCAL
        map_qos.reliability = ReliabilityPolicy.RELIABLE
        self.create_subscription(
            OccupancyGrid, "/navigation/global_map", self._map_callback, map_qos
        )
        self.create_subscription(
            PoseStamped, "/navigation/global_pose", self._pose_callback, 20
        )
        self.create_subscription(
            LaserScan,
            "/ouster/scan",
            lambda _message: self._mark_topic("/ouster/scan"),
            rclpy.qos.qos_profile_sensor_data,
        )
        self.create_subscription(
            LaserScan,
            "/navigation/reconstructed_scan",
            lambda _message: self._mark_topic("/navigation/reconstructed_scan"),
            rclpy.qos.qos_profile_sensor_data,
        )
        self.create_subscription(
            Imu,
            "/ouster/imu",
            lambda _message: self._mark_topic("/ouster/imu"),
            rclpy.qos.qos_profile_sensor_data,
        )
        self.create_subscription(
            TwistStamped,
            "/qbot_speed_feedback",
            lambda _message: self._mark_topic("/qbot_speed_feedback"),
            10,
        )

        self._finish_client = self.create_client(
            FinishTrajectory, "/finish_trajectory"
        )
        self.create_timer(1.0, self._refresh_runtime_state)
        self._record_event("WSL mapping worker started")
        if self._manage_visualization:
            self._executor.submit(self._ensure_visualization_impl)

    def _record_event(self, message: str, level: str = "info") -> None:
        with self._lock:
            self._event_id += 1
            self._events.append(
                {
                    "id": self._event_id,
                    "time": time.time(),
                    "level": level,
                    "message": message,
                }
            )
            del self._events[:-100]

    def _set_mapping_state(
        self, state: str, detail: str, error: Optional[str] = None
    ) -> None:
        with self._lock:
            self._state["mapping"] = state
            self._state["detail"] = detail
            self._state["error"] = error
        self._record_event(detail, "error" if error else "info")

    def _mark_topic(self, topic: str) -> None:
        with self._lock:
            self._topic_seen[topic] = time.monotonic()

    def _map_callback(self, message: OccupancyGrid) -> None:
        with self._lock:
            self._topic_seen["/navigation/global_map"] = time.monotonic()
            self._latest_map = message
            self._render_locked()

    def _pose_callback(self, message: PoseStamped) -> None:
        now = time.monotonic()
        with self._lock:
            self._topic_seen["/navigation/global_pose"] = now
            if not mapping_scan_is_fresh(self._topic_seen, now):
                return
            self._pose_xy = (message.pose.position.x, message.pose.position.y)
            self._state["pose"] = {
                "x": message.pose.position.x,
                "y": message.pose.position.y,
                "frame": message.header.frame_id,
            }
            if self._latest_map is not None and now - self._last_pose_render >= 0.2:
                self._render_locked()

    def _render_locked(self) -> None:
        if self._latest_map is None:
            return
        message = self._latest_map
        orientation = message.info.origin.orientation
        geometry = MapGeometry(
            width=message.info.width,
            height=message.info.height,
            resolution=message.info.resolution,
            origin_x=message.info.origin.position.x,
            origin_y=message.info.origin.position.y,
            origin_yaw=quaternion_yaw(
                orientation.x, orientation.y, orientation.z, orientation.w
            ),
        )
        try:
            rendered = render_occupancy_ppm(message.data, geometry, self._pose_xy)
        except (TypeError, ValueError) as error:
            self.get_logger().warning(f"Could not render occupancy map: {error}")
            return
        known_cells = sum(1 for value in message.data if value >= 0)
        self._rendered_map = rendered
        self._last_pose_render = time.monotonic()
        self._state["map_version"] += 1
        self._state["map"] = {
            "width": geometry.width,
            "height": geometry.height,
            "resolution": geometry.resolution,
            "known_cells": known_cells,
            "frame": message.header.frame_id,
        }

    def _topic_ages_locked(self) -> dict[str, Optional[float]]:
        now = time.monotonic()
        topics = (
            "/ouster/scan",
            "/navigation/reconstructed_scan",
            "/ouster/imu",
            "/qbot_speed_feedback",
            "/navigation/global_map",
            "/navigation/global_pose",
        )
        return {
            topic: None
            if topic not in self._topic_seen
            else round(now - self._topic_seen[topic], 3)
            for topic in topics
        }

    def _refresh_runtime_state(self) -> None:
        names = {f"/{name.lstrip('/')}" for name in self.get_node_names()}
        services = {name for name, _types in self.get_service_names_and_types()}
        mapping_running = (
            self._mapping_process is not None
            and self._mapping_process.poll() is None
        )
        playback_running = (
            self._playback_process is not None
            and self._playback_process.poll() is None
        )
        visualization_nodes = {
            "/navigation_visualizer",
            "/wsl_foxglove_bridge",
        }

        with self._lock:
            previous_playback = self._state["playback_running"]
            self._node_names = names
            self._service_names = services
            self._state["process_running"] = mapping_running
            self._state["playback_running"] = playback_running
            self._state["finish_service_available"] = (
                self._finish_client.service_is_ready()
            )
            self._state["topic_ages"] = self._topic_ages_locked()
            self._state["nodes"] = sorted(names)

            if visualization_nodes.issubset(names):
                self._state["visualization"] = "running"
            elif (
                self._visualization_process is not None
                and self._visualization_process.poll() is None
            ):
                self._state["visualization"] = "starting"
            elif self._manage_visualization:
                self._state["visualization"] = "degraded"

            if (
                previous_playback
                and not playback_running
                and self._state["source"] == "playback"
                and self._state["mapping"] == "mapping"
            ):
                self._state["mapping"] = "playback_complete"
                self._state["playback_complete"] = True
                self._state["detail"] = (
                    "Rosbag playback complete; mapping is ready to finish and save"
                )
                self._record_event(self._state["detail"])

            if (
                self._mapping_process is not None
                and not mapping_running
                and self._state["mapping"] in ACTIVE_MAPPING_STATES
                and self._state["mapping"] != "cancelling"
                and not self._expected_mapping_stop
            ):
                code = self._mapping_process.returncode
                self._state["mapping"] = "error"
                self._state["detail"] = f"Mapping process exited unexpectedly with code {code}"
                self._state["error"] = self._state["detail"]
                self._record_event(self._state["detail"], "error")

            mapping_nodes = {
                "/cartographer_node",
                "/cartographer_occupancy_grid_node",
                "/ouster_mapping_adapter",
                "/horizontal_ring_decoder",
            }
            if (
                self._mapping_process is None
                and self._state["mapping"] == "ready"
                and mapping_nodes.intersection(names)
                and self._mapping_os_processes_running()
            ):
                self._state["mapping"] = "conflict"
                self._state["detail"] = "Unmanaged mapping nodes are already running"
                self._state["error"] = self._state["detail"]

    @staticmethod
    def _start_process(command: list[str], log_name: str) -> subprocess.Popen:
        log_path = Path("/tmp") / log_name
        log_stream = log_path.open("ab", buffering=0)
        try:
            return subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log_stream,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            log_stream.close()

    @staticmethod
    def _stop_process(process: Optional[subprocess.Popen]) -> None:
        if process is None or process.poll() is not None:
            return
        for process_signal, timeout in (
            (signal.SIGINT, 8.0),
            (signal.SIGTERM, 3.0),
            (signal.SIGKILL, 1.0),
        ):
            try:
                os.killpg(process.pid, process_signal)
            except ProcessLookupError:
                return
            try:
                process.wait(timeout=timeout)
                return
            except subprocess.TimeoutExpired:
                continue

    @staticmethod
    def _mapping_os_processes_running() -> bool:
        """Distinguish live mapper processes from stale ROS discovery entries."""
        result = subprocess.run(
            [
                "pgrep",
                "-f",
                "[c]artographer_node|[c]artographer_occupancy_grid_node|"
                "[o]uster_mapping_adapter|[o]s_cloud.*horizontal_ring_decoder",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.returncode == 0

    def _wait_for_mapping_nodes_to_stop(self, timeout: float = 5.0) -> None:
        mapping_nodes = {
            "/cartographer_node",
            "/cartographer_occupancy_grid_node",
            "/ouster_mapping_adapter",
            "/horizontal_ring_decoder",
        }
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            names = {f"/{name.lstrip('/')}" for name in self.get_node_names()}
            if (
                not mapping_nodes.intersection(names)
                or not self._mapping_os_processes_running()
            ):
                return
            time.sleep(0.1)

    def _ensure_visualization_impl(self) -> None:
        with self._operation_lock:
            self._refresh_runtime_state()
            required = {"/navigation_visualizer", "/wsl_foxglove_bridge"}
            with self._lock:
                if required.issubset(self._node_names):
                    self._state["visualization"] = "running"
                    return
                if (
                    self._visualization_process is not None
                    and self._visualization_process.poll() is None
                ):
                    return
                self._state["visualization"] = "starting"

            command = [
                "ros2",
                "launch",
                "qbot_navigation_visualization",
                "wsl_navigation_visualization.launch.py",
                "output_frame:=os_lidar",
            ]
            try:
                process = self._start_process(command, "delta_visualization.log")
            except OSError as error:
                with self._lock:
                    self._state["visualization"] = "degraded"
                self._record_event(f"Could not start WSL visualization: {error}", "error")
                return
            with self._lock:
                self._visualization_process = process
                self._managed_visualization = True
            self._record_event("WSL LiDAR visualization and Foxglove start requested")

    def ensure_visualization(self) -> dict[str, Any]:
        self._executor.submit(self._ensure_visualization_impl)
        return {"accepted": True}

    def _submit_operation(self, operation, *args) -> bool:
        with self._lock:
            if self._operation_pending:
                return False
            self._operation_pending = True

        def run_operation() -> None:
            try:
                operation(*args)
            finally:
                with self._lock:
                    self._operation_pending = False

        self._executor.submit(run_operation)
        return True

    def start_mapping(self, payload: dict[str, Any]) -> dict[str, Any]:
        source = str(payload.get("source", "live"))
        if source not in ("live", "playback"):
            raise ValueError("source must be 'live' or 'playback'")
        bag_path = str(payload.get("bag_path", ""))
        playback_rate = float(payload.get("playback_rate", 2.0))
        playback_scan_ring = int(payload.get("scan_ring", 64))
        if playback_rate <= 0.0 or playback_rate > 20.0:
            raise ValueError("playback_rate must be greater than 0 and no more than 20")
        if playback_scan_ring < 0 or playback_scan_ring > 127:
            raise ValueError("scan_ring must be between 0 and 127")

        with self._lock:
            if self._state["mapping"] != "ready":
                raise RuntimeError(
                    f"Mapping cannot start while state is {self._state['mapping']}"
                )
            self._state["mapping"] = "starting"
            self._state["source"] = source
            self._state["playback_scan_ring"] = (
                playback_scan_ring if source == "playback" else None
            )
            self._state["detail"] = f"Starting {source} mapping"
            self._state["error"] = None
            self._state["playback_complete"] = False
            self._state["saved_paths"] = None
            self._trajectory_finished = False
            self._expected_mapping_stop = False
            self._state["trajectory_finished"] = False

        if not self._submit_operation(
            self._start_mapping_impl,
            source,
            bag_path,
            playback_rate,
            playback_scan_ring,
        ):
            with self._lock:
                self._state["mapping"] = "ready"
            raise RuntimeError("Another mapping operation is already running")
        self._record_event(f"{source.capitalize()} mapping start requested")
        return {"accepted": True}

    def _start_mapping_impl(
        self,
        source: str,
        bag_path: str,
        playback_rate: float,
        playback_scan_ring: int,
    ) -> None:
        with self._operation_lock:
            try:
                self._refresh_runtime_state()
                mapping_nodes = {
                    "/cartographer_node",
                    "/cartographer_occupancy_grid_node",
                    "/ouster_mapping_adapter",
                    "/horizontal_ring_decoder",
                }
                with self._lock:
                    if mapping_nodes.intersection(self._node_names):
                        raise RuntimeError("Mapping nodes are already running")
                    topic_ages = self._topic_ages_locked()

                if source == "live":
                    missing = [
                        topic
                        for topic in ("/ouster/scan", "/ouster/imu")
                        if topic_ages[topic] is None or topic_ages[topic] > 2.0
                    ]
                    if missing:
                        raise RuntimeError(
                            "Required live topics are not fresh: " + ", ".join(missing)
                        )
                else:
                    metadata = Path(bag_path).expanduser() / "metadata.yaml"
                    if not metadata.is_file():
                        raise RuntimeError(f"Rosbag metadata was not found: {metadata}")

                if source == "playback":
                    mapping_command = [
                        "ros2",
                        "launch",
                        "qbot_navigation_visualization",
                        "wsl_raw_ring_mapping.launch.py",
                        f"scan_ring:={playback_scan_ring}",
                    ]
                else:
                    mapping_command = [
                        "ros2",
                        "launch",
                        "qbot_navigation_visualization",
                        "wsl_2d_mapping.launch.py",
                        "start_visualization:=false",
                        "start_bridge:=false",
                        "output_frame:=os_lidar",
                    ]
                process = self._start_process(mapping_command, "delta_mapping.log")
                with self._lock:
                    self._mapping_process = process

                deadline = time.monotonic() + 20.0
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError(
                            f"Mapping process exited during startup with code {process.returncode}"
                        )
                    if self._finish_client.service_is_ready():
                        break
                    time.sleep(0.2)
                else:
                    raise RuntimeError("Cartographer finish service did not become ready")

                if source == "playback":
                    ouster_share = Path(get_package_share_directory("ouster_ros"))
                    playback_command = [
                        "ros2",
                        "bag",
                        "play",
                        str(Path(bag_path).expanduser()),
                        "--rate",
                        str(playback_rate),
                        "--topics",
                        "/ouster/metadata",
                        "/ouster/lidar_packets",
                        "/ouster/imu",
                        "--qos-profile-overrides-path",
                        str(ouster_share / "config" / "metadata-qos-override.yaml"),
                    ]
                    playback = self._start_process(
                        playback_command, "delta_playback.log"
                    )
                    with self._lock:
                        self._playback_process = playback

                with self._lock:
                    self._state["mapping"] = "mapping"
                    self._state["detail"] = f"{source.capitalize()} mapping is running"
                    self._state["error"] = None
                self._record_event(self._state["detail"])
            except Exception as error:  # process and ROS failures share one state
                self._expected_mapping_stop = True
                self._stop_process(self._playback_process)
                self._stop_process(self._mapping_process)
                with self._lock:
                    self._playback_process = None
                    self._mapping_process = None
                    self._expected_mapping_stop = False
                self._set_mapping_state("error", f"Mapping could not start: {error}", str(error))

    def finish_and_save(self, payload: dict[str, Any]) -> dict[str, Any]:
        name = validate_map_name(str(payload.get("name", "")))
        with self._lock:
            if self._state["mapping"] not in (
                "mapping",
                "playback_complete",
                "save_failed",
            ):
                raise RuntimeError(
                    f"Mapping cannot be saved while state is {self._state['mapping']}"
                )
            self._state["mapping"] = (
                "saving" if self._trajectory_finished else "finishing"
            )
            self._state["detail"] = f"Finishing and saving map {name}"
            self._state["error"] = None

        if not self._submit_operation(self._finish_and_save_impl, name):
            raise RuntimeError("Another mapping operation is already running")
        self._record_event(f"Finish and save requested for {name}")
        return {"accepted": True}

    def _finish_and_save_impl(self, name: str) -> None:
        with self._operation_lock:
            try:
                if not self._trajectory_finished:
                    if not self._finish_client.wait_for_service(timeout_sec=3.0):
                        raise RuntimeError("/finish_trajectory is not available")
                    request = FinishTrajectory.Request()
                    request.trajectory_id = 0
                    future = self._finish_client.call_async(request)
                    deadline = time.monotonic() + 10.0
                    while not future.done() and time.monotonic() < deadline:
                        time.sleep(0.05)
                    if not future.done():
                        raise RuntimeError("/finish_trajectory timed out")
                    response = future.result()
                    if response is None:
                        raise RuntimeError("/finish_trajectory returned no response")
                    if response.status.code != 0:
                        raise RuntimeError(
                            f"Cartographer rejected finish: {response.status.message}"
                        )
                    self._trajectory_finished = True
                    with self._lock:
                        self._state["trajectory_finished"] = True

                with self._lock:
                    self._state["mapping"] = "saving"
                    self._state["detail"] = f"Saving map {name}"
                    before_version = self._state["map_version"]

                deadline = time.monotonic() + 6.0
                while time.monotonic() < deadline:
                    with self._lock:
                        if self._state["map_version"] > before_version:
                            break
                    time.sleep(0.1)

                with self._lock:
                    if self._latest_map is None or not self._state["map"]:
                        raise RuntimeError("No accumulated map is available to save")
                    if self._state["map"]["known_cells"] <= 0:
                        raise RuntimeError("The accumulated map has no known cells")
                    latest_map = self._latest_map

                output_directory = Path.home() / "qbot_maps"
                output_directory.mkdir(parents=True, exist_ok=True)
                final_pgm = output_directory / f"{name}.pgm"
                final_yaml = output_directory / f"{name}.yaml"
                if final_pgm.exists() or final_yaml.exists():
                    raise RuntimeError(f"Map output already exists: {name}")

                staging_root = output_directory / ".delta_tmp"
                staging_root.mkdir(parents=True, exist_ok=True)
                staging = Path(tempfile.mkdtemp(prefix="save_", dir=staging_root))
                try:
                    stem = staging / name
                    package_check = subprocess.run(
                        ["ros2", "pkg", "prefix", "nav2_map_server"],
                        capture_output=True,
                        text=True,
                        timeout=5.0,
                        check=False,
                    )
                    if package_check.returncode == 0:
                        result = subprocess.run(
                            [
                                "ros2",
                                "run",
                                "nav2_map_server",
                                "map_saver_cli",
                                "-t",
                                "/navigation/global_map",
                                "-f",
                                str(stem),
                                "--fmt",
                                "pgm",
                                "--mode",
                                "trinary",
                            ],
                            capture_output=True,
                            text=True,
                            timeout=30.0,
                            check=False,
                        )
                        if result.returncode != 0:
                            detail = (result.stderr or result.stdout).strip()
                            raise RuntimeError(
                                "map_saver_cli failed with code "
                                f"{result.returncode}: {detail}"
                            )
                        save_backend = "nav2_map_server"
                    else:
                        orientation = latest_map.info.origin.orientation
                        geometry = MapGeometry(
                            width=latest_map.info.width,
                            height=latest_map.info.height,
                            resolution=latest_map.info.resolution,
                            origin_x=latest_map.info.origin.position.x,
                            origin_y=latest_map.info.origin.position.y,
                            origin_yaw=quaternion_yaw(
                                orientation.x,
                                orientation.y,
                                orientation.z,
                                orientation.w,
                            ),
                        )
                        save_trinary_map(latest_map.data, geometry, stem)
                        save_backend = "builtin_trinary"
                        self._record_event(
                            "nav2_map_server is unavailable; used the compatible "
                            "built-in trinary saver",
                            "warning",
                        )
                    staged_pgm = stem.with_suffix(".pgm")
                    staged_yaml = stem.with_suffix(".yaml")
                    validate_saved_map(staged_pgm, staged_yaml, name)
                    os.replace(staged_pgm, final_pgm)
                    os.replace(staged_yaml, final_yaml)
                finally:
                    shutil.rmtree(staging, ignore_errors=True)

                validate_saved_map(final_pgm, final_yaml, name)
                self._expected_mapping_stop = True
                self._stop_process(self._playback_process)
                self._stop_process(self._mapping_process)
                self._wait_for_mapping_nodes_to_stop()
                with self._lock:
                    self._playback_process = None
                    self._mapping_process = None
                    self._expected_mapping_stop = False
                    self._state["mapping"] = "saved"
                    self._state["detail"] = f"Map {name} saved and mapping stopped"
                    self._state["error"] = None
                    self._state["saved_paths"] = {
                        "name": name,
                        "pgm": str(final_pgm),
                        "yaml": str(final_yaml),
                        "backend": save_backend,
                    }
                self._record_event(self._state["detail"])
            except Exception as error:
                self._set_mapping_state(
                    "save_failed",
                    f"Map save failed; mapping remains available for retry: {error}",
                    str(error),
                )

    def cancel_mapping(self) -> dict[str, Any]:
        with self._lock:
            if self._state["mapping"] not in (
                "starting",
                "mapping",
                "playback_complete",
                "save_failed",
            ):
                raise RuntimeError(
                    f"Mapping cannot be cancelled while state is {self._state['mapping']}"
                )
            self._state["mapping"] = "cancelling"
            self._state["detail"] = "Cancelling mapping without saving"
        if not self._submit_operation(self._cancel_mapping_impl):
            raise RuntimeError("Another mapping operation is already running")
        self._record_event("Mapping cancellation requested")
        return {"accepted": True}

    def _cancel_mapping_impl(self) -> None:
        with self._operation_lock:
            self._expected_mapping_stop = True
            self._stop_process(self._playback_process)
            self._stop_process(self._mapping_process)
            self._wait_for_mapping_nodes_to_stop()
            with self._lock:
                self._playback_process = None
                self._mapping_process = None
                self._expected_mapping_stop = False
                self._trajectory_finished = False
                self._state["trajectory_finished"] = False
                self._state["process_running"] = False
                self._state["playback_running"] = False
                self._state["mapping"] = "cancelled"
                self._state["detail"] = "Mapping cancelled; no map was saved"
                self._state["error"] = None
            self._record_event(self._state["detail"])

    def new_map(self) -> dict[str, Any]:
        self._refresh_runtime_state()
        with self._lock:
            if self._state["mapping"] in ACTIVE_MAPPING_STATES:
                raise RuntimeError("Finish and save or cancel the active mapping first")
            if self._mapping_process is not None and self._mapping_process.poll() is None:
                raise RuntimeError("A mapping process is still running")
            mapping_nodes = {
                "/cartographer_node",
                "/cartographer_occupancy_grid_node",
                "/ouster_mapping_adapter",
                "/horizontal_ring_decoder",
            }
            if (
                mapping_nodes.intersection(self._node_names)
                and self._mapping_os_processes_running()
            ):
                raise RuntimeError("Unmanaged mapping nodes must be stopped first")
            self._latest_map = None
            self._pose_xy = None
            self._rendered_map = None
            self._trajectory_finished = False
            self._state.update(
                {
                    "mapping": "ready",
                    "source": "live",
                    "playback_scan_ring": None,
                    "detail": "Ready to start a new map",
                    "error": None,
                    "map_version": self._state["map_version"] + 1,
                    "map": None,
                    "pose": None,
                    "playback_complete": False,
                    "trajectory_finished": False,
                    "saved_paths": None,
                }
            )
        self._record_event("New map workspace is ready")
        return {"accepted": True}

    def get_state(self) -> dict[str, Any]:
        with self._lock:
            state = copy.deepcopy(self._state)
            state["topic_ages"] = self._topic_ages_locked()
            return state

    def get_map(self) -> tuple[int, Optional[bytes]]:
        with self._lock:
            return self._state["map_version"], self._rendered_map

    def destroy_node(self) -> bool:
        self._executor.shutdown(wait=False, cancel_futures=False)
        return super().destroy_node()


class WorkerHttpServer(ThreadingHTTPServer):
    allow_reuse_address = True


def build_handler(worker: MappingWorker):
    class Handler(BaseHTTPRequestHandler):
        server_version = "DeltaMappingWorker/1.0"

        def _send_json(self, status: int, value: Any) -> None:
            body = json.dumps(value).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 65536:
                raise ValueError("Request body is too large")
            if length == 0:
                return {}
            value = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("Request body must be a JSON object")
            return value

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            if self.path == "/health":
                self._send_json(200, {"ok": True, "service": "delta_mapping_worker"})
                return
            if self.path == "/state":
                self._send_json(200, worker.get_state())
                return
            if self.path.startswith("/map.ppm"):
                version, data = worker.get_map()
                if data is None:
                    self.send_response(204)
                    self.send_header("X-Map-Version", str(version))
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/x-portable-pixmap")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("X-Map-Version", str(version))
                self.end_headers()
                self.wfile.write(data)
                return
            self._send_json(404, {"error": "Unknown endpoint"})

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            try:
                payload = self._read_json()
                if self.path == "/mapping/start":
                    result = worker.start_mapping(payload)
                elif self.path == "/mapping/finish-save":
                    result = worker.finish_and_save(payload)
                elif self.path == "/mapping/cancel":
                    result = worker.cancel_mapping()
                elif self.path == "/mapping/new":
                    result = worker.new_map()
                elif self.path == "/visualization/ensure":
                    result = worker.ensure_visualization()
                else:
                    self._send_json(404, {"error": "Unknown endpoint"})
                    return
                self._send_json(202, result)
            except (ValueError, RuntimeError) as error:
                self._send_json(409, {"error": str(error)})
            except Exception as error:
                worker.get_logger().error(f"Worker HTTP request failed: {error}")
                self._send_json(500, {"error": str(error)})

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    return Handler


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--address", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--no-manage-visualization", action="store_true")
    arguments, ros_arguments = parser.parse_known_args(argv)

    rclpy.init(args=ros_arguments)
    worker = MappingWorker(
        manage_visualization=not arguments.no_manage_visualization
    )
    server = WorkerHttpServer(
        (arguments.address, arguments.port), build_handler(worker)
    )
    server_thread = threading.Thread(
        target=server.serve_forever, name="delta-http", daemon=True
    )
    server_thread.start()
    worker.get_logger().info(
        "Delta mapping API listening on "
        f"http://{arguments.address}:{arguments.port}"
    )
    try:
        rclpy.spin(worker)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        worker.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
