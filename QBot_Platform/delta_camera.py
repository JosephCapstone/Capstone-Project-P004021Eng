"""Read the QBot camera through its existing Foxglove bridge."""

import json
import struct


def compressed_image_data(payload: bytes) -> bytes:
    """Extract JPEG/PNG data from a CDR sensor_msgs/CompressedImage."""
    if len(payload) < 16 or payload[:2] not in (b"\x00\x00", b"\x00\x01"):
        raise ValueError("Invalid camera CDR header")
    endian = "<" if payload[1] == 1 else ">"
    offset = 12  # CDR encapsulation followed by Header.stamp.

    def read_sequence() -> bytes:
        nonlocal offset
        offset = (offset + 3) & ~3
        if offset + 4 > len(payload):
            raise ValueError("Truncated camera message")
        length = struct.unpack_from(endian + "I", payload, offset)[0]
        offset += 4
        if length > len(payload) - offset:
            raise ValueError("Truncated camera data")
        value = payload[offset:offset + length]
        offset += length
        return value

    read_sequence()  # Header.frame_id
    image_format = read_sequence().rstrip(b"\x00").lower()
    if not any(encoding in image_format for encoding in (b"jpeg", b"jpg", b"png")):
        raise ValueError("Unsupported compressed camera format")
    image = read_sequence()
    if not image:
        raise ValueError("Empty camera image")
    return image


class FoxgloveCameraClient:
    def __init__(self, host: str, port: int = 8765) -> None:
        self.host = host
        self.port = port
        self._socket = None
        self._channel_id = None
        self._version = 0

    def camera_image(self) -> tuple[int, bytes | None]:
        try:
            import websocket
        except ImportError as error:
            raise RuntimeError(
                "Camera requires websocket-client; install QBot_Platform/requirements.txt"
            ) from error

        try:
            if self._socket is None:
                self._socket = websocket.create_connection(
                    f"ws://{self.host}:{self.port}",
                    subprotocols=["foxglove.websocket.v1"],
                    timeout=1.0,
                    http_no_proxy=[self.host],
                )
            while True:
                packet = self._socket.recv()
                if not packet:
                    raise ConnectionError("QBot camera bridge disconnected")
                if isinstance(packet, str):
                    message = json.loads(packet)
                    if message.get("op") == "advertise":
                        for channel in message.get("channels", []):
                            if (
                                channel.get("topic") == "/camera/color_image/compressed"
                                and channel.get("encoding") == "cdr"
                                and channel.get("schemaName") == "sensor_msgs/msg/CompressedImage"
                                and channel["id"] != self._channel_id
                            ):
                                self._socket.send(json.dumps({
                                    "op": "subscribe",
                                    "subscriptions": [{"id": 1, "channelId": channel["id"]}],
                                }))
                                self._channel_id = channel["id"]
                    elif (
                        message.get("op") == "unadvertise"
                        and self._channel_id in message.get("channelIds", [])
                    ):
                        raise ConnectionError("QBot camera topic disappeared")
                    elif message.get("op") == "status" and message.get("level") == 2:
                        raise RuntimeError(message.get("message", "Camera bridge error"))
                elif (
                    self._channel_id is not None
                    and len(packet) >= 13
                    and packet[0] == 1
                    and struct.unpack_from("<I", packet, 1)[0] == 1
                ):
                    image = compressed_image_data(packet[13:])
                    self._version += 1
                    return self._version, image
        except websocket.WebSocketTimeoutException:
            return self._version, None
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        connection = self._socket
        self._socket = None
        self._channel_id = None
        if connection is not None:
            connection.close(timeout=0.1)
