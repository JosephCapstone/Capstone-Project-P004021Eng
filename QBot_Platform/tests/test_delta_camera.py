import json
import struct
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from delta_camera import FoxgloveCameraClient, compressed_image_data
from delta_backend import BackendConfig, DeltaBackend


def camera_cdr(image, endian="<", frame_id=b"camera\0"):
    payload = bytearray(b"\x00\x01\x00\x00" if endian == "<" else b"\x00\x00\x00\x00")
    payload.extend(struct.pack(endian + "iI", 1, 2))
    for value in (frame_id, b"bgr8; jpeg compressed bgr8\0", image):
        payload.extend(b"\0" * (-len(payload) % 4))
        payload.extend(struct.pack(endian + "I", len(value)))
        payload.extend(value)
    return bytes(payload)


class CameraTest(unittest.TestCase):
    def test_extracts_compressed_bytes_with_cdr_alignment_and_byte_order(self):
        image = b"\xff\xd8example jpeg\xff\xd9"
        for endian in ("<", ">"):
            for frame_id in (b"a\0", b"camera\0", b"rgb_image\0"):
                with self.subTest(endian=endian, frame_id=frame_id):
                    self.assertEqual(compressed_image_data(camera_cdr(image, endian, frame_id)), image)

    def test_rejects_invalid_or_truncated_camera_messages(self):
        payload = camera_cdr(b"jpeg bytes")
        for length in (0, 3, 15, len(payload) - 1):
            with self.subTest(length=length), self.assertRaises(ValueError):
                compressed_image_data(payload[:length])
        with self.assertRaises(ValueError):
            compressed_image_data(b"\x00\x03" + payload[2:])

    def test_subscribes_to_color_camera_and_extracts_its_binary_frame(self):
        connection = Mock()
        advertisement = json.dumps({"op": "advertise", "channels": [
            {"id": 7, "topic": "/camera/color_image/compressed", "encoding": "cdr",
             "schemaName": "sensor_msgs/msg/CompressedImage"},
            {"id": 8, "topic": "/camera/depth_image/compressed", "encoding": "cdr",
             "schemaName": "sensor_msgs/msg/CompressedImage"},
        ]})
        image = b"\xff\xd8color frame\xff\xd9"
        connection.recv.side_effect = [advertisement, struct.pack("<BIQ", 1, 1, 0) + camera_cdr(image)]
        websocket = SimpleNamespace(create_connection=Mock(return_value=connection), WebSocketTimeoutException=TimeoutError)
        client = FoxgloveCameraClient("192.0.2.1")
        with patch.dict(sys.modules, {"websocket": websocket}):
            self.assertEqual(client.camera_image(), (1, image))
        self.assertEqual(json.loads(connection.send.call_args.args[0]), {
            "op": "subscribe", "subscriptions": [{"id": 1, "channelId": 7}],
        })
        client.close()

    def test_idle_connection_is_kept_and_broken_connection_is_retried(self):
        connection = Mock()
        connection.recv.side_effect = [TimeoutError(), ConnectionError("disconnected"), TimeoutError()]
        websocket = SimpleNamespace(create_connection=Mock(return_value=connection), WebSocketTimeoutException=TimeoutError)
        client = FoxgloveCameraClient("192.0.2.1")
        with patch.dict(sys.modules, {"websocket": websocket}):
            self.assertEqual(client.camera_image(), (0, None))
            with self.assertRaises(ConnectionError):
                client.camera_image()
            self.assertEqual(client.camera_image(), (0, None))
        self.assertEqual(websocket.create_connection.call_count, 2)
        client.close()

    def test_camera_freshness_is_independent_of_mapping_worker(self):
        camera = Mock()
        camera.camera_image.return_value = (1, b"image")
        backend = DeltaBackend(config=BackendConfig(), camera_client=camera, start_monitor=False)
        try:
            backend._camera_received_at = time.monotonic()
            self.assertEqual(backend.get_state()["camera"]["state"], "streaming")
            self.assertEqual(backend.get_state()["mapping"]["mapping"], "unavailable")
            backend._camera_received_at -= 3.0
            self.assertEqual(backend.get_state()["camera"]["state"], "waiting")
        finally:
            backend.close()


if __name__ == "__main__":
    unittest.main()
