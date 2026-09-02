import sys
import tempfile
import time
import unittest
from pathlib import Path
from subprocess import CompletedProcess


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from delta_backend import BackendConfig, DeltaBackend  # noqa: E402


class FakeWorker:
    def __init__(self):
        self.posts = []
        self.worker_state = {
            "mapping": "ready",
            "visualization": "running",
            "detail": "Ready",
            "map_version": 0,
            "topic_ages": {
                "/ouster/scan": None,
                "/ouster/imu": None,
                "/qbot_speed_feedback": None,
            },
            "events": [],
            "saved_paths": None,
        }

    def state(self):
        return dict(self.worker_state)

    def map_image(self):
        return self.worker_state["map_version"], None

    def post(self, path, payload=None):
        self.posts.append((path, payload))


class FakeJetson:
    def __init__(self):
        self.commands = []
        self.probe_value = {
            "qbot": 0,
            "recording": 0,
            "throttle": 0,
            "recording_bytes": 0,
        }

    def run(self, command, timeout=8.0):
        self.commands.append(command)
        return 0, "", ""

    def probe(self, _recording_name):
        return dict(self.probe_value)


def wait_for(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition did not become true")


class DeltaBackendTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.worker = FakeWorker()
        self.jetson = FakeJetson()
        self.config = BackendConfig(
            windows_map_directory=Path(self.temporary.name) / "maps"
        )
        self.backend = DeltaBackend(
            config=self.config,
            worker_client=self.worker,
            jetson_client=self.jetson,
            start_monitor=False,
        )

    def tearDown(self):
        self.backend.close()
        self.temporary.cleanup()

    def test_default_config_uses_current_jetson_address(self):
        self.assertEqual(BackendConfig().jetson_host, "192.168.137.33")
        self.assertEqual(
            BackendConfig().ros_workspace,
            "/home/ernie/Capstone-Project-P004021Eng",
        )

    def test_map_and_recording_names_are_validated(self):
        self.assertEqual(self.backend.validate_map_name("delta_map_1"), "delta_map_1")
        with self.assertRaises(ValueError):
            self.backend.validate_map_name("../unsafe")
        with self.assertRaises(ValueError):
            self.backend.validate_recording_name("recording name")

    def test_qbot_launch_keeps_script_and_sets_ros_domain(self):
        self.backend.launch_script()
        wait_for(lambda: bool(self.jetson.commands))
        command = self.jetson.commands[0]
        self.assertIn("setsid --fork ./run_qbot.sh", command)
        self.assertIn("ROS_DOMAIN_ID=7", command)
        self.assertEqual(self.backend.get_state()["qbot"]["state"], "starting")

    def test_worker_start_discovers_workspace_and_verifies_health(self):
        commands = []

        def runner(command, **_kwargs):
            commands.append(command)
            return CompletedProcess(command, 0, "", "")

        backend = DeltaBackend(
            config=self.config,
            worker_client=self.worker,
            jetson_client=self.jetson,
            process_runner=runner,
            start_monitor=False,
        )
        try:
            backend._start_worker()
        finally:
            backend.close()

        script = commands[0][-1]
        self.assertIn("Capstone-Project-P004021Eng", script)
        self.assertIn("ros2_ws", script)
        self.assertIn("install/setup.bash", script)
        self.assertIn("export ROS_DOMAIN_ID=7", script)
        self.assertIn("delta_mapping_worker --port 8766", script)
        self.assertIn("http://127.0.0.1:8766/health", script)

    def test_mapping_state_changes_only_after_worker_evidence(self):
        self.backend.start_mapping()
        wait_for(lambda: bool(self.worker.posts))
        self.assertEqual(self.worker.posts[0], ("/mapping/start", {"source": "live"}))
        self.assertEqual(
            self.backend.get_state()["mapping"]["mapping"], "unavailable"
        )

        self.worker.worker_state["mapping"] = "mapping"
        self.worker.worker_state["detail"] = "Live mapping is running"
        self.backend._poll_worker()
        self.assertEqual(self.backend.get_state()["mapping"]["mapping"], "mapping")

    def test_qbot_requires_process_and_fresh_topics(self):
        self.backend._poll_worker()
        self.jetson.probe_value["qbot"] = 1
        self.backend._poll_jetson()
        self.assertEqual(self.backend.get_state()["qbot"]["state"], "degraded")

        self.worker.worker_state["topic_ages"] = {
            "/ouster/scan": 0.1,
            "/ouster/imu": 0.1,
            "/qbot_speed_feedback": 0.1,
        }
        self.backend._poll_worker()
        self.backend._poll_jetson()
        self.assertEqual(self.backend.get_state()["qbot"]["state"], "running")

    def test_saved_pair_is_copied_without_overwrite(self):
        source = Path(self.temporary.name) / "source"
        source.mkdir()
        source_pgm = source / "test_map.pgm"
        source_yaml = source / "test_map.yaml"
        source_pgm.write_bytes(b"P5\n1 1\n255\n\x00")
        source_yaml.write_text("image: test_map.pgm\n", encoding="utf-8")
        paths = {"pgm": source_pgm, "yaml": source_yaml}
        self.backend._wsl_windows_path = lambda value: paths[value]

        self.backend._copy_saved_outputs(
            {"name": "test_map", "pgm": "pgm", "yaml": "yaml"}
        )
        destination = self.config.windows_map_directory
        self.assertTrue((destination / "test_map.pgm").is_file())
        self.assertTrue((destination / "test_map.yaml").is_file())
        self.assertIsNone(self.backend.get_state()["copy_error"])

        self.backend._copy_saved_outputs(
            {"name": "test_map", "pgm": "pgm", "yaml": "yaml"}
        )
        self.assertIn("already exists", self.backend.get_state()["copy_error"])

    def test_failed_copy_leaves_no_final_files(self):
        source = Path(self.temporary.name) / "source"
        source.mkdir()
        source_pgm = source / "broken.pgm"
        source_pgm.write_bytes(b"P5\n1 1\n255\n\x00")
        missing_yaml = source / "missing.yaml"
        paths = {"pgm": source_pgm, "yaml": missing_yaml}
        self.backend._wsl_windows_path = lambda value: paths[value]

        self.backend._copy_saved_outputs(
            {"name": "broken", "pgm": "pgm", "yaml": "yaml"}
        )
        destination = self.config.windows_map_directory
        self.assertFalse((destination / "broken.pgm").exists())
        self.assertFalse((destination / "broken.yaml").exists())
        self.assertIsNotNone(self.backend.get_state()["copy_error"])


if __name__ == "__main__":
    unittest.main()
