"""Backend facade for the DELTA PySide frontend.

The facade keeps Jetson SSH, WSL startup, ROS state, and file copying out of
the UI.  Its public methods return immediately; verified state is available
through :meth:`get_state`.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional


SAFE_RECORDING_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
SAFE_MAP_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
JETSON_HOSTS = ("192.168.137.33", "192.168.0.157")


def select_jetson_host(timeout: float = 0.75) -> str:
    """Select a reachable Jetson address once during Delta startup."""
    for host in JETSON_HOSTS:
        try:
            connection = socket.create_connection((host, 22), timeout=timeout)
            connection.close()
            return host
        except OSError:
            continue
    return JETSON_HOSTS[0]


@dataclass(frozen=True)
class BackendConfig:
    jetson_host: str = "192.168.137.33"
    jetson_username: str = "nvidia"
    jetson_password: str = "nvidia"
    wsl_distro: str = "Ubuntu-22.04"
    ros_workspace: str = "/home/ernie/Capstone-Project-P004021Eng"
    ros_domain_id: int = 7
    worker_url: str = "http://127.0.0.1:8766"
    worker_port: int = 8766
    windows_map_directory: Path = Path.home() / "Documents" / "DELTA Maps"

    @classmethod
    def from_environment(cls) -> "BackendConfig":
        documents_root = Path(os.environ.get("OneDrive", Path.home())) / "Documents"
        default_maps = documents_root / "DELTA Maps"
        return cls(
            jetson_host=os.environ.get("DELTA_JETSON_HOST") or select_jetson_host(),
            jetson_username=os.environ.get("DELTA_JETSON_USERNAME", "nvidia"),
            jetson_password=os.environ.get("DELTA_JETSON_PASSWORD", "nvidia"),
            wsl_distro=os.environ.get("DELTA_WSL_DISTRO", "Ubuntu-22.04"),
            ros_workspace=os.environ.get(
                "DELTA_ROS_WS", "/home/ernie/Capstone-Project-P004021Eng"
            ),
            ros_domain_id=int(os.environ.get("DELTA_ROS_DOMAIN_ID", "7")),
            worker_url=os.environ.get(
                "DELTA_WORKER_URL", "http://127.0.0.1:8766"
            ).rstrip("/"),
            worker_port=int(os.environ.get("DELTA_WORKER_PORT", "8766")),
            windows_map_directory=Path(
                os.environ.get("DELTA_MAPS_WINDOWS_DIR", str(default_maps))
            ).expanduser(),
        )


class WorkerClient:
    def __init__(self, base_url: str, timeout: float = 2.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(
        self, path: str, payload: Optional[dict[str, Any]] = None
    ) -> tuple[bytes, dict[str, str], int]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            headers={"Content-Type": "application/json"} if body else {},
            method="POST" if body is not None else "GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read(), dict(response.headers), response.status
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(detail).get("error", detail)
            except json.JSONDecodeError:
                pass
            raise RuntimeError(str(detail)) from error
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            raise RuntimeError(f"WSL mapping worker is unavailable: {error}") from error

    def health(self) -> bool:
        body, _headers, status = self._request("/health")
        return status == 200 and json.loads(body).get("ok") is True

    def state(self) -> dict[str, Any]:
        body, _headers, _status = self._request("/state")
        return json.loads(body)

    def map_image(self) -> tuple[int, Optional[bytes]]:
        body, headers, status = self._request("/map.ppm")
        version = int(headers.get("X-Map-Version", "0"))
        return version, body if status == 200 and body else None

    def camera_image(self) -> tuple[int, Optional[bytes]]:
        body, headers, status = self._request("/camera.ppm")
        version = int(headers.get("X-Camera-Version", "0"))
        return version, body if status == 200 and body else None

    def post(self, path: str, payload: Optional[dict[str, Any]] = None) -> None:
        self._request(path, {} if payload is None else payload)


class ParamikoJetsonClient:
    def __init__(self, config: BackendConfig) -> None:
        self.config = config

    def run(self, command: str, timeout: float = 8.0) -> tuple[int, str, str]:
        try:
            import paramiko
        except ImportError as error:
            raise RuntimeError(
                "Paramiko is not installed; install QBot_Platform/requirements.txt"
            ) from error

        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            ssh.connect(
                hostname=self.config.jetson_host,
                username=self.config.jetson_username,
                password=self.config.jetson_password,
                timeout=timeout,
                banner_timeout=timeout,
                auth_timeout=timeout,
            )
            _stdin, stdout, stderr = ssh.exec_command(command, timeout=timeout)
            status = stdout.channel.recv_exit_status()
            return (
                status,
                stdout.read().decode("utf-8", errors="replace").strip(),
                stderr.read().decode("utf-8", errors="replace").strip(),
            )
        except TimeoutError as error:
            raise RuntimeError(
                f"SSH command timed out after {timeout:g} seconds"
            ) from error
        finally:
            ssh.close()

    def probe(self, recording_name: Optional[str]) -> dict[str, Any]:
        lines = [
            "pgrep -f '[r]un_qbot.sh' >/dev/null && echo qbot=1 || echo qbot=0",
            "pgrep -f '[r]os2 bag record' >/dev/null && echo recording=1 || echo recording=0",
            (
                "pgrep -f '[t]opic_tools throttle messages /ouster/points 5.0 "
                "/ouster/points_viz' >/dev/null && echo throttle=1 || echo throttle=0"
            ),
        ]
        if recording_name:
            lines.append(
                "test -d \"$HOME/ros2/bags/test_"
                + recording_name
                + "\" && du -sb \"$HOME/ros2/bags/test_"
                + recording_name
                + "\" | awk '{print \"recording_bytes=\"$1}' || "
                "echo recording_bytes=0"
            )
        command = "; ".join(lines)
        status, output, error = self.run(command)
        if status != 0:
            raise RuntimeError(error or f"Jetson probe exited with {status}")
        values: dict[str, Any] = {}
        for line in output.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                values[key] = int(value) if value.isdigit() else value
        return values


class DeltaBackend:
    def __init__(
        self,
        config: Optional[BackendConfig] = None,
        worker_client: Optional[WorkerClient] = None,
        jetson_client: Optional[ParamikoJetsonClient] = None,
        process_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        start_monitor: bool = True,
    ) -> None:
        self.config = config or BackendConfig.from_environment()
        self.worker = worker_client or WorkerClient(self.config.worker_url)
        self.jetson = jetson_client or ParamikoJetsonClient(self.config)
        self._process_runner = process_runner
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._executor = ThreadPoolExecutor(max_workers=5, thread_name_prefix="delta-ui")
        self._monitor_thread: Optional[threading.Thread] = None
        self._camera_thread: Optional[threading.Thread] = None
        self._worker_start_attempt = 0.0
        self._jetson_poll_time = 0.0
        self._jetson_poll_in_flight = False
        self._qbot_requested_at = 0.0
        self._qbot_stop_requested_at = 0.0
        self._recording_requested_at = 0.0
        self._recording_name: Optional[str] = None
        self._recording_bytes = 0
        self._recording_bytes_changed_at = 0.0
        self._managed_throttle = False
        self._throttle_request_in_flight = False
        self._copy_in_flight = False
        self._copied_save_name: Optional[str] = None
        self._copy_retry_at = 0.0
        self._worker_event_id = 0
        self._map_version = -1
        self._map_image: Optional[bytes] = None
        self._camera_version = -1
        self._camera_image: Optional[bytes] = None
        self._event_id = 0
        self._events: list[dict[str, Any]] = []
        self._state: dict[str, Any] = {
            "backend": "starting",
            "worker": {"state": "starting", "error": None},
            "qbot": {"state": "unknown", "process_running": False, "error": None},
            "recording": {
                "state": "unknown",
                "process_running": False,
                "name": None,
                "bytes": 0,
                "error": None,
            },
            "visualization": {"state": "starting"},
            "mapping": {
                "mapping": "unavailable",
                "detail": "Connecting to the WSL mapping worker",
                "map_version": 0,
            },
            "windows_saved_paths": None,
            "copy_error": None,
            "events": self._events,
        }
        self._record_event("Delta backend started")
        if start_monitor:
            self._monitor_thread = threading.Thread(
                target=self._monitor_loop, name="delta-monitor", daemon=True
            )
            self._monitor_thread.start()
            self._camera_thread = threading.Thread(
                target=self._camera_loop, name="delta-camera", daemon=True
            )
            self._camera_thread.start()

    @staticmethod
    def default_map_name() -> str:
        return datetime.now().strftime("delta_map_%Y%m%d_%H%M%S")

    @staticmethod
    def validate_map_name(name: str) -> str:
        value = name.strip()
        if not SAFE_MAP_NAME.fullmatch(value):
            raise ValueError(
                "Map name must be 1-64 characters and use only letters, numbers, "
                "underscores, or hyphens"
            )
        return value

    @staticmethod
    def validate_recording_name(name: str) -> str:
        value = name.strip()
        if not SAFE_RECORDING_NAME.fullmatch(value):
            raise ValueError(
                "Recording number must be 1-32 safe filename characters"
            )
        return value

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
            del self._events[:-150]

    def _monitor_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._poll_worker()
            except Exception as error:
                self._set_worker_unavailable(str(error))
                if time.monotonic() - self._worker_start_attempt > 15.0:
                    self._worker_start_attempt = time.monotonic()
                    self._start_worker()

            if time.monotonic() - self._jetson_poll_time >= 3.0:
                self._schedule_jetson_poll()
            self._stop_event.wait(0.5)

    def _camera_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                version, camera_image = self.worker.camera_image()
                with self._lock:
                    if version != self._camera_version:
                        self._camera_version = version
                        self._camera_image = camera_image
            except Exception:
                # Worker availability is reported by the main monitor. Camera
                # polling stays independent so a slow frame cannot stall it.
                pass
            self._stop_event.wait(0.1)

    def _schedule_jetson_poll(self) -> None:
        with self._lock:
            if self._jetson_poll_in_flight:
                return
            self._jetson_poll_in_flight = True
            self._jetson_poll_time = time.monotonic()
        try:
            self._executor.submit(self._poll_jetson_async)
        except RuntimeError:
            with self._lock:
                self._jetson_poll_in_flight = False
            raise

    def _poll_jetson_async(self) -> None:
        try:
            self._poll_jetson()
        finally:
            with self._lock:
                self._jetson_poll_in_flight = False
                self._jetson_poll_time = time.monotonic()

    def _set_worker_unavailable(self, error: str) -> None:
        with self._lock:
            previous = self._state["worker"]["state"]
            self._state["backend"] = "degraded"
            self._state["worker"] = {"state": "offline", "error": error}
            self._state["mapping"] = {
                "mapping": "unavailable",
                "detail": error,
                "error": error,
                "map_version": self._map_version,
            }
            self._state["visualization"] = {"state": "unavailable"}
        if previous != "offline":
            self._record_event(error, "error")

    def _start_worker(self) -> None:
        with self._lock:
            self._state["worker"] = {"state": "starting", "error": None}
        workspace_setup = shlex.quote(
            self.config.ros_workspace.rstrip("/") + "/install/setup.bash"
        )
        script = (
            "source /opt/ros/humble/setup.bash && "
            f"if [ -f {workspace_setup} ]; then source {workspace_setup}; "
            "elif [ -f ~/ros2_ws/install/setup.bash ]; then "
            "source ~/ros2_ws/install/setup.bash; "
            "elif [ -f ~/ros2/install/setup.bash ]; then "
            "source ~/ros2/install/setup.bash; "
            "else echo \"No DELTA ROS workspace setup was found\" >&2; exit 4; fi && "
            f"export ROS_DOMAIN_ID={self.config.ros_domain_id} && "
            "export ROS_LOCALHOST_ONLY=0 && "
            "nohup ros2 run qbot_navigation_visualization delta_mapping_worker "
            f"--port {self.config.worker_port} "
            ">/tmp/delta_mapping_worker.log 2>&1 </dev/null & "
            "sleep 2; "
            f"curl --fail --silent --max-time 3 "
            f"http://127.0.0.1:{self.config.worker_port}/health >/dev/null"
        )
        try:
            result = self._process_runner(
                [
                    "wsl.exe",
                    "-d",
                    self.config.wsl_distro,
                    "--",
                    "bash",
                    "-lc",
                    script,
                ],
                capture_output=True,
                text=True,
                timeout=12.0,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    result.stderr.strip()
                    or f"wsl.exe exited with code {result.returncode}"
                )
            self._record_event("WSL mapping worker start requested")
        except Exception as error:
            self._set_worker_unavailable(f"Could not start WSL worker: {error}")

    def _poll_worker(self) -> None:
        worker_state = self.worker.state()
        worker_events = worker_state.get("events", [])
        for event in worker_events:
            if event.get("id", 0) > self._worker_event_id:
                self._worker_event_id = event["id"]
                self._record_event(
                    "WSL: " + event.get("message", ""), event.get("level", "info")
                )

        version = int(worker_state.get("map_version", 0))
        if version != self._map_version:
            map_version, map_image = self.worker.map_image()
            with self._lock:
                self._map_version = map_version
                self._map_image = map_image

        with self._lock:
            self._state["backend"] = "running"
            self._state["worker"] = {"state": "running", "error": None}
            self._state["mapping"] = worker_state
            self._state["visualization"] = {
                "state": worker_state.get("visualization", "unknown")
            }

        saved_paths = worker_state.get("saved_paths")
        if (
            saved_paths
            and saved_paths.get("name") != self._copied_save_name
            and not self._copy_in_flight
            and time.monotonic() >= self._copy_retry_at
        ):
            self._copy_in_flight = True
            self._executor.submit(self._copy_saved_outputs, saved_paths)

    def _poll_jetson(self) -> None:
        try:
            probe = self.jetson.probe(self._recording_name)
        except Exception as error:
            with self._lock:
                previous = self._state["qbot"]["state"]
                self._state["qbot"] = {
                    "state": "offline",
                    "process_running": False,
                    "error": str(error),
                }
                self._state["recording"].update(
                    {"state": "offline", "process_running": False, "error": str(error)}
                )
            if previous != "offline":
                self._record_event(f"Jetson is unavailable: {error}", "error")
            return

        process_running = bool(probe.get("qbot", 0))
        recorder_running = bool(probe.get("recording", 0))
        throttle_running = bool(probe.get("throttle", 0))
        recording_bytes = int(probe.get("recording_bytes", 0))
        now = time.monotonic()

        with self._lock:
            topic_ages = self._state.get("mapping", {}).get("topic_ages", {})
            stale_topics = [
                topic
                for topic in (
                    "/ouster/scan",
                    "/ouster/imu",
                    "/qbot_speed_feedback",
                )
                if topic_ages.get(topic) is None or topic_ages.get(topic, 999.0) > 3.0
            ]
            previous_qbot = self._state["qbot"]["state"]
            if process_running and not stale_topics:
                qbot_state = "running"
            elif process_running and now - self._qbot_requested_at < 90.0:
                qbot_state = "starting"
            elif process_running:
                qbot_state = "degraded"
            elif previous_qbot == "stopping" and now - self._qbot_stop_requested_at < 15.0:
                qbot_state = "stopping"
            elif previous_qbot == "starting" and now - self._qbot_requested_at < 10.0:
                qbot_state = "starting"
            else:
                qbot_state = "stopped"
            self._state["qbot"] = {
                "state": qbot_state,
                "process_running": process_running,
                "stale_topics": stale_topics,
                "throttle_running": throttle_running,
                "error": None,
            }

            if recording_bytes > self._recording_bytes:
                self._recording_bytes_changed_at = now
            self._recording_bytes = recording_bytes
            if recorder_running and now - self._recording_bytes_changed_at < 8.0:
                recording_state = "recording"
            elif recorder_running and now - self._recording_requested_at < 12.0:
                recording_state = "starting"
            elif recorder_running:
                recording_state = "degraded"
            elif (
                self._state["recording"]["state"] == "stopping"
                and now - self._recording_requested_at < 8.0
            ):
                recording_state = "stopping"
            else:
                recording_state = "stopped"
            self._state["recording"] = {
                "state": recording_state,
                "process_running": recorder_running,
                "name": self._recording_name,
                "bytes": recording_bytes,
                "error": None,
            }

        if process_running and not throttle_running and not self._throttle_request_in_flight:
            self._throttle_request_in_flight = True
            self._executor.submit(self._ensure_throttle)

        if previous_qbot != qbot_state:
            self._record_event(f"QBot state verified as {qbot_state}")

    def _run_jetson_action(self, description: str, command: str) -> None:
        try:
            status, output, error = self.jetson.run(command)
            if status != 0:
                raise RuntimeError(error or output or f"remote command exited with {status}")
            self._record_event(f"{description} command accepted; awaiting verified state")
        except Exception as error:
            self._record_event(f"{description} failed: {error}", "error")

    def launch_script(self) -> None:
        with self._lock:
            self._qbot_requested_at = time.monotonic()
            self._state["qbot"]["state"] = "starting"
        command = (
            "cd /home/nvidia && "
            f"export ROS_DOMAIN_ID={self.config.ros_domain_id} ROS_LOCALHOST_ONLY=0 && "
            "setsid --fork ./run_qbot.sh >/tmp/delta_run_qbot.log 2>&1 </dev/null"
        )
        self._record_event("QBot start requested")
        self._executor.submit(self._run_jetson_action, "QBot start", command)

    def stop_script(self) -> None:
        with self._lock:
            self._qbot_stop_requested_at = time.monotonic()
            self._state["qbot"]["state"] = "stopping"
        throttle_stop = ""
        if self._managed_throttle:
            throttle_stop = (
                "; pkill -INT -f '[t]opic_tools throttle messages /ouster/points "
                "5.0 /ouster/points_viz' || true"
            )
        command = "pkill -INT -f '[r]un_qbot.sh' || true" + throttle_stop
        self._record_event("QBot stop requested")
        self._executor.submit(self._run_jetson_action, "QBot stop", command)

    def _ensure_throttle(self) -> None:
        command = (
            "source /opt/ros/humble/setup.bash && "
            "source ~/ros2/install/setup.bash && "
            f"export ROS_DOMAIN_ID={self.config.ros_domain_id} ROS_LOCALHOST_ONLY=0 && "
            "setsid --fork ros2 run topic_tools throttle messages /ouster/points 5.0 "
            "/ouster/points_viz --ros-args -p lazy:=false "
            ">/tmp/delta_points_throttle.log 2>&1 </dev/null"
        )
        try:
            status, output, error = self.jetson.run(command)
            if status != 0:
                raise RuntimeError(error or output or f"remote command exited with {status}")
            self._managed_throttle = True
            self._record_event("Jetson point-cloud throttle start requested")
        except Exception as error:
            self._record_event(f"Point-cloud throttle could not start: {error}", "error")
        finally:
            self._throttle_request_in_flight = False

    def start_recording(self, recording_name: str) -> None:
        name = self.validate_recording_name(recording_name)
        with self._lock:
            self._recording_name = name
            self._recording_requested_at = time.monotonic()
            self._recording_bytes = 0
            self._recording_bytes_changed_at = time.monotonic()
            self._state["recording"].update(
                {"state": "starting", "name": name, "error": None}
            )
        command = (
            "cd /home/nvidia && "
            f"export ROS_DOMAIN_ID={self.config.ros_domain_id} ROS_LOCALHOST_ONLY=0 && "
            f"setsid --fork ./run_record.sh {name} >/tmp/delta_record_{name}.log "
            "2>&1 </dev/null"
        )
        self._record_event(f"Recording {name} start requested")
        self._executor.submit(self._run_jetson_action, "Recording start", command)

    def stop_recording(self) -> None:
        with self._lock:
            self._recording_requested_at = time.monotonic()
            self._state["recording"]["state"] = "stopping"
        self._record_event("Recording stop requested")
        self._executor.submit(
            self._run_jetson_action,
            "Recording stop",
            "pkill -INT -f '[r]os2 bag record' || true",
        )

    def _post_worker(self, path: str, payload: Optional[dict[str, Any]] = None) -> None:
        try:
            self.worker.post(path, payload)
        except Exception as error:
            self._record_event(f"Mapping command failed: {error}", "error")

    def start_mapping(self) -> None:
        self._record_event("Live mapping start requested")
        self._executor.submit(
            self._post_worker, "/mapping/start", {"source": "live"}
        )

    def finish_and_save(self, map_name: str) -> None:
        name = self.validate_map_name(map_name)
        with self._lock:
            self._state["windows_saved_paths"] = None
            self._state["copy_error"] = None
            self._copied_save_name = None
            self._copy_retry_at = 0.0
        self._record_event(f"Finish and save requested for {name}")
        self._executor.submit(
            self._post_worker, "/mapping/finish-save", {"name": name}
        )

    def cancel_mapping(self) -> None:
        self._record_event("Mapping cancel requested")
        self._executor.submit(self._post_worker, "/mapping/cancel", None)

    def new_map(self) -> None:
        self._record_event("New map reset requested")
        self._executor.submit(self._post_worker, "/mapping/new", None)

    def _wsl_windows_path(self, linux_path: str) -> Path:
        result = self._process_runner(
            [
                "wsl.exe",
                "-d",
                self.config.wsl_distro,
                "--",
                "wslpath",
                "-w",
                linux_path,
            ],
            capture_output=True,
            text=True,
            timeout=8.0,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "wslpath failed")
        return Path(result.stdout.strip())

    def _copy_saved_outputs(self, saved_paths: dict[str, str]) -> None:
        name = saved_paths["name"]
        try:
            destination = self.config.windows_map_directory
            destination.mkdir(parents=True, exist_ok=True)
            final_pgm = destination / f"{name}.pgm"
            final_yaml = destination / f"{name}.yaml"
            if final_pgm.exists() or final_yaml.exists():
                raise RuntimeError(f"Windows map output already exists: {name}")

            source_pgm = self._wsl_windows_path(saved_paths["pgm"])
            source_yaml = self._wsl_windows_path(saved_paths["yaml"])
            temporary_pgm = destination / f".{name}.pgm.tmp"
            temporary_yaml = destination / f".{name}.yaml.tmp"
            try:
                shutil.copy2(source_pgm, temporary_pgm)
                shutil.copy2(source_yaml, temporary_yaml)
                os.replace(temporary_pgm, final_pgm)
                os.replace(temporary_yaml, final_yaml)
            finally:
                temporary_pgm.unlink(missing_ok=True)
                temporary_yaml.unlink(missing_ok=True)

            with self._lock:
                self._copied_save_name = name
                self._state["windows_saved_paths"] = {
                    "pgm": str(final_pgm),
                    "yaml": str(final_yaml),
                }
                self._state["copy_error"] = None
                self._copy_retry_at = 0.0
            self._record_event(f"Map {name} copied to {destination}")
        except Exception as error:
            with self._lock:
                self._state["copy_error"] = str(error)
                self._copy_retry_at = time.monotonic() + 10.0
            self._record_event(
                f"Map is safe in WSL but the Windows copy failed: {error}", "error"
            )
        finally:
            self._copy_in_flight = False

    def get_state(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._state)

    def get_map(self) -> tuple[int, Optional[bytes]]:
        with self._lock:
            return self._map_version, self._map_image

    def get_camera(self) -> tuple[int, Optional[bytes]]:
        with self._lock:
            return self._camera_version, self._camera_image

    def close(self) -> None:
        self._stop_event.set()
        if self._monitor_thread is not None:
            self._monitor_thread.join(timeout=1.0)
        if self._camera_thread is not None:
            self._camera_thread.join(timeout=1.0)
        self._executor.shutdown(wait=False, cancel_futures=False)
