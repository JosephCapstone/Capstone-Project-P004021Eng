import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, PropertyMock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from delta_backend import BackendConfig, DeltaBackend, ParamikoJetsonClient


class DeltaLidarTest(unittest.TestCase):
    def test_environment_uses_config_default_and_allows_explicit_selection(self):
        with patch.dict(os.environ, {"DELTA_JETSON_HOST": "192.0.2.1"}, clear=True):
            self.assertEqual(
                BackendConfig.from_environment().lidar_type, BackendConfig().lidar_type
            )
            with patch.object(BackendConfig, "lidar_type", "ouster"):
                self.assertEqual(BackendConfig.from_environment().lidar_type, "ouster")
            for value in ("hesai", "ouster"):
                os.environ["DELTA_LIDAR_TYPE"] = f" {value.upper()} "
                self.assertEqual(BackendConfig.from_environment().lidar_type, value)

    def test_launch_record_and_stop_select_the_same_lidar(self):
        for lidar, launch, record in (
            ("hesai", "run_qbot_h.sh", "run_record_h.sh"),
            ("ouster", "run_qbot.sh", "run_record.sh"),
        ):
            with self.subTest(lidar=lidar):
                jetson = Mock()
                jetson.run.return_value = (0, "", "")
                backend = DeltaBackend(
                    config=BackendConfig(lidar_type=lidar, ros_domain_id=7),
                    jetson_client=jetson,
                    start_monitor=False,
                )
                try:
                    backend.launch_script()
                    backend.start_recording("trial")
                    backend.stop_script()
                    backend._executor.shutdown(wait=True)
                    commands = [call.args[0] for call in jetson.run.call_args_list]
                    self.assertTrue(any(f"setsid --fork bash ./{launch} " in cmd for cmd in commands))
                    self.assertTrue(any(f"setsid --fork bash ./{record} trial " in cmd for cmd in commands))
                    self.assertTrue(any("pkill -" in cmd for cmd in commands))
                    self.assertTrue(all(
                        "ROS_DOMAIN_ID=7" in cmd
                        for cmd in commands if "setsid --fork" in cmd
                    ))
                    wrong_launch = "run_qbot.sh" if lidar == "hesai" else "run_qbot_h.sh"
                    self.assertTrue(all(wrong_launch not in cmd for cmd in commands))
                finally:
                    backend.close()

    def test_probe_measures_the_directory_written_by_each_recorder(self):
        for lidar, directory in (("hesai", "test_hesai_trial"), ("ouster", "test_trial")):
            with self.subTest(lidar=lidar), tempfile.TemporaryDirectory() as temporary:
                bag = Path(temporary) / "ros2" / "bags" / directory
                bag.mkdir(parents=True)
                (bag / "data.db3").write_bytes(b"recorded data")
                client = ParamikoJetsonClient(BackendConfig(lidar_type=lidar))

                def run_locally(command, timeout=8.0):
                    result = subprocess.run(
                        ["bash", "-c", "pgrep() { return 1; }\n" + command.replace("$HOME", temporary)],
                        capture_output=True,
                        text=True,
                        timeout=timeout,
                    )
                    return result.returncode, result.stdout, result.stderr

                client.run = run_locally
                self.assertGreater(client.probe("trial")["recording_bytes"], 0)

    def test_hesai_requires_wrapper_and_live_data(self):
        jetson = Mock()
        backend = DeltaBackend(
            config=BackendConfig(lidar_type="hesai"),
            jetson_client=jetson,
            start_monitor=False,
        )
        try:
            for wrapper, points, expected in (
                (1, 0, "degraded"),
                (1, 1, "running"),
                (0, 1, "stopped"),
            ):
                with self.subTest(wrapper=wrapper, points=points):
                    jetson.probe.return_value = {"qbot": wrapper, "hesai_data": points}
                    backend._poll_jetson()
                    state = backend.get_state()["qbot"]
                    self.assertEqual(state["state"], expected)
                    self.assertEqual(state["stale_topics"], [] if points else ["/lidar_points", "/lidar_packets"])

            backend._qbot_requested_at = time.monotonic()
            jetson.probe.return_value = {"qbot": 1, "hesai_data": 0}
            backend._poll_jetson()
            self.assertEqual(backend.get_state()["qbot"]["state"], "starting")
        finally:
            backend.close()

    def test_hesai_health_check_bypasses_shared_cli_daemon(self):
        client = ParamikoJetsonClient(BackendConfig(lidar_type="hesai"))
        with patch.object(client, "run", return_value=(0, "qbot=1\nhesai_data=0", "")) as run:
            client.probe(None)
        self.assertIn("ros2 topic echo /lidar_points", run.call_args.args[0])
        self.assertIn("ros2 topic echo /lidar_packets", run.call_args.args[0])
        self.assertIn("--no-daemon", run.call_args.args[0])

    def test_hesai_reports_started_once_when_live_data_arrives(self):
        jetson = Mock()
        backend = DeltaBackend(
            config=BackendConfig(lidar_type="hesai"),
            jetson_client=jetson,
            start_monitor=False,
        )
        try:
            backend._qbot_requested_at = time.monotonic()
            jetson.probe.return_value = {"qbot": 1, "hesai_driver": 1, "hesai_data": 0}
            backend._poll_jetson()
            self.assertEqual(backend.get_state()["qbot"]["state"], "starting")
            self.assertNotIn("QBot started", [e["message"] for e in backend.get_state()["events"]])

            jetson.probe.return_value["hesai_data"] = 1
            backend._poll_jetson()
            backend._poll_jetson()
            state = backend.get_state()
            self.assertEqual(state["qbot"]["state"], "running")
            self.assertEqual(state["qbot"]["stale_topics"], [])
            self.assertEqual([e["message"] for e in state["events"]].count("QBot started"), 1)

            backend._qbot_requested_at -= 100.0
            jetson.probe.return_value["hesai_data"] = 0
            backend._poll_jetson()
            self.assertEqual(backend.get_state()["qbot"]["state"], "degraded")
        finally:
            backend.close()

    def test_hesai_recording_uses_driver_readiness_even_without_launcher_or_points(self):
        jetson = Mock()
        backend = DeltaBackend(
            config=BackendConfig(lidar_type="hesai"),
            jetson_client=jetson,
            start_monitor=False,
        )
        try:
            for wrapper, driver in ((0, 1), (1, 1), (0, 0), (1, 0)):
                with self.subTest(wrapper=wrapper, driver=driver):
                    backend._state["qbot"]["state"] = "stopped"
                    backend._qbot_requested_at = time.monotonic()
                    jetson.probe.return_value = {
                        "qbot": wrapper, "hesai_driver": driver, "hesai_data": 0,
                    }
                    backend._poll_jetson()
                    self.assertEqual(
                        backend.get_state()["qbot"]["recording_ready"], bool(driver)
                    )
            backend._state["qbot"]["state"] = "stopping"
            backend._qbot_stop_requested_at = time.monotonic()
            jetson.probe.return_value = {"qbot": 0, "hesai_driver": 1}
            backend._poll_jetson()
            self.assertFalse(backend.get_state()["qbot"]["recording_ready"])
        finally:
            backend.close()

    def test_hesai_stop_waits_for_driver_exit_after_wrapper_exits(self):
        jetson = Mock()
        backend = DeltaBackend(
            config=BackendConfig(lidar_type="hesai"),
            jetson_client=jetson,
            start_monitor=False,
        )
        try:
            backend._state["qbot"]["state"] = "stopping"
            backend._qbot_stop_requested_at = time.monotonic()
            for wrapper in (1, 0):
                jetson.probe.return_value = {"qbot": wrapper, "hesai_driver": 1, "hesai_data": 1}
                backend._poll_jetson()
                self.assertEqual(backend.get_state()["qbot"]["state"], "stopping")
                self.assertFalse(backend.get_state()["qbot"]["recording_ready"])
            jetson.probe.return_value = {"qbot": 0, "hesai_driver": 0}
            backend._poll_jetson()
            self.assertEqual(backend.get_state()["qbot"]["state"], "stopped")
        finally:
            backend.close()

    def test_hesai_stop_cleans_up_orphans_that_ignore_graceful_signals(self):
        marker = "delta_stop_test_" + uuid.uuid4().hex
        labels = [marker + suffix for suffix in ("_wrapper", "_launch", "_driver", "_ouster")]
        program = (
            "import signal,time; "
            "signal.signal(signal.SIGINT,signal.SIG_IGN); "
            "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
            "print('ready',flush=True); time.sleep(60)"
        )
        processes = []
        jetson = Mock()
        jetson.run.return_value = (0, "", "")
        backend = DeltaBackend(
            config=BackendConfig(lidar_type="hesai"),
            jetson_client=jetson,
            start_monitor=False,
        )
        try:
            for label in labels:
                process = subprocess.Popen([sys.executable, "-u", "-c", program, label], stdout=subprocess.PIPE)
                processes.append(process)
                self.assertEqual(process.stdout.readline().strip(), b"ready")
            patterns = ["[d]" + label[1:] for label in labels]
            with (
                patch.object(BackendConfig, "jetson_run_pattern", new_callable=PropertyMock, return_value=patterns[0]),
                patch("delta_backend.HESAI_LAUNCH_PATTERN", patterns[1]),
                patch("delta_backend.HESAI_DRIVER_PATTERN", patterns[2]),
            ):
                backend.stop_script()
                backend._executor.shutdown(wait=True)
            command = jetson.run.call_args.args[0]
            result = subprocess.run(["bash", "-c", command], capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            for process in processes[:3]:
                self.assertEqual(process.wait(timeout=2), -signal.SIGKILL)
            self.assertIsNone(processes[3].poll(), "Unrelated Ouster process must remain running")
        finally:
            backend.close()
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=2)
                process.stdout.close()


if __name__ == "__main__":
    unittest.main()
