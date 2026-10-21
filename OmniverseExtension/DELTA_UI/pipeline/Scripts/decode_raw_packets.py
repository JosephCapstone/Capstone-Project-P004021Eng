#!/usr/bin/env python3
"""Decode Ouster packet recordings into point clouds."""

import argparse
import functools
import sys
from pathlib import Path

import numpy as np

# Forces every print() in this module to flush immediately. Needed because
# this script is normally launched as a subprocess with stdout piped into
# the applet's log window (not a real terminal) - Python defaults to full
# block buffering in that case, not line buffering. Confirmed necessary on
# 2026-08-14: a real run against test_6 crashed with a native access
# violation (Windows exit code 3221225477 / 0xC0000005 - a C++-level
# segfault inside ouster-sdk's compiled bindings, not a catchable Python
# exception) and every print() from "Reading sensor metadata..." onward
# was lost - never appeared in the console at all - because the buffer
# holding them hadn't been flushed yet when the process was killed. Without
# this, a crash can wipe out the exact diagnostic output needed to debug it.
print = functools.partial(print, flush=True)

# A commonly-published .msg definition for ouster_sensor_msgs/msg/PacketMsg
# (header + raw UDP packet bytes) - kept here only for reference / as a
# fallback for register_ouster_types(), NOT confirmed correct for this
# project's actual captures.
#
# NOT USED by this script's own reading path any more, for two separate
# reasons found against real data on 2026-08-14: (1) registering this with
# rosbags' generic get_types_from_msg()/typestore.register() machinery hit
# a UnicodeDecodeError, consistent with rosbags mis-resolving the
# unqualified "std_msgs/Header" cross-reference against the installed
# rosbags version's ROS2_HUMBLE store; and (2), once parse_packet_msg_buf()
# below was reading the bytes by hand instead, a hex dump of a real
# message proved this bag's actual PacketMsg has NO header field at all -
# just uint8[] buf directly after the CDR encapsulation header (see
# parse_packet_msg_buf()'s docstring for the exact byte-accounting that
# proved it). So this text is wrong for at least this capture, on top of
# rosbags not resolving it correctly. register_ouster_types() is left in
# place only in case some other bag really does use this shape and a
# future rosbags version resolves the cross-reference correctly.
PACKET_MSG_DEFINITION = """
std_msgs/Header header
uint8[] buf
"""


def register_ouster_types():
    """Registers ouster_sensor_msgs/msg/PacketMsg with the 'rosbags'
    typestore, so its topics can be deserialized like any built-in ROS
    message type. NOT called by this script's own decode path any more
    - see the note above PACKET_MSG_DEFINITION."""
    from rosbags.typesys import Stores, get_types_from_msg, get_typestore

    typestore = get_typestore(Stores.ROS2_HUMBLE)
    types = get_types_from_msg(
        PACKET_MSG_DEFINITION, "ouster_sensor_msgs/msg/PacketMsg"
    )
    typestore.register(types)
    return typestore


def parse_packet_msg_buf(rawdata):
    """Manually parses a CDR-encoded ouster_sensor_msgs/msg/PacketMsg message and returns the raw 'buf' bytes - the one complete raw sensor UDP packet this message carries."""
    if len(rawdata) < 4:
        raise ValueError(
            f"PacketMsg payload too short ({len(rawdata)} bytes) to hold even "
            "a CDR encapsulation header."
        )
    # Byte 0 is reserved (0x00). Byte 1 is the representation identifier:
    # 0x00/0x02 = big-endian CDR, 0x01/0x03 = little-endian CDR. Bytes 2-3
    # are options, unused here.
    if rawdata[1] not in (0x00, 0x01, 0x02, 0x03):
        raise ValueError(
            f"Unrecognized CDR representation identifier (byte 1 = "
            f"0x{rawdata[1]:02x}) - this message may not be CDR-encoded the "
            "way this function expects. See this function's own docstring."
        )
    byteorder = "little" if rawdata[1] in (0x01, 0x03) else "big"

    offset = 4  # past the encapsulation header

    if offset + 4 > len(rawdata):
        raise ValueError("PacketMsg payload too short to hold buf's length prefix.")
    buf_len = int.from_bytes(rawdata[offset : offset + 4], byteorder)
    offset += 4

    if offset + buf_len > len(rawdata):
        raise ValueError(
            f"PacketMsg payload too short: buf claims {buf_len} bytes but only "
            f"{len(rawdata) - offset} remain after the encapsulation header. "
            "This usually means this function's manual CDR layout does not "
            "match this bag's actual message layout - see its own docstring."
        )
    return rawdata[offset : offset + buf_len]


def read_metadata_json(bag_path, metadata_topic, typestore):
    """Reads the single std_msgs/msg/String message on `metadata_topic` -
    the sensor's own JSON metadata, the same content normally saved
    alongside a .pcap capture as a separate metadata.json file. Returns
    the raw JSON string."""
    from rosbags.rosbag2 import Reader

    with Reader(bag_path) as reader:
        connections = [c for c in reader.connections if c.topic == metadata_topic]
        if not connections:
            raise ValueError(f"Topic '{metadata_topic}' not found in this bag.")
        for connection, _timestamp, rawdata in reader.messages(connections=connections):
            msg = typestore.deserialize_cdr(rawdata, connection.msgtype)
            return msg.data
    raise ValueError(f"No message found on '{metadata_topic}' - expected exactly one.")


def iter_raw_packets(bag_path, topic, on_first_message=None):
    """Yields (timestamp_ns, raw_bytes) for every message on `topic`, in bag order - raw_bytes is that message's 'buf' field, i.e."""
    from rosbags.rosbag2 import Reader

    with Reader(bag_path) as reader:
        connections = [c for c in reader.connections if c.topic == topic]
        if not connections:
            raise ValueError(f"Topic '{topic}' not found in this bag.")
        first = True
        for _connection, timestamp, rawdata in reader.messages(connections=connections):
            rawdata = bytes(rawdata)
            if first:
                first = False
                if on_first_message:
                    on_first_message(rawdata)
            yield timestamp, parse_packet_msg_buf(rawdata)


def build_pointcloud2_message(
    typestore, points_xyz, timestamp_ns, frame_id="os_sensor"
):
    """Builds a sensor_msgs/msg/PointCloud2 message from an Nx3 float32 array - x/y/z fields only, matching what slam_kiss_icp.py's own .ply writer already keeps (no intensity/reflectivity carried through, to keep this as."""
    PointField = typestore.types["sensor_msgs/msg/PointField"]
    PointCloud2 = typestore.types["sensor_msgs/msg/PointCloud2"]
    Header = typestore.types["std_msgs/msg/Header"]
    Time = typestore.types["builtin_interfaces/msg/Time"]

    sec = int(timestamp_ns // 1_000_000_000)
    nanosec = int(timestamp_ns % 1_000_000_000)

    FLOAT32 = 7  # sensor_msgs/msg/PointField.FLOAT32
    fields = [
        PointField(name="x", offset=0, datatype=FLOAT32, count=1),
        PointField(name="y", offset=4, datatype=FLOAT32, count=1),
        PointField(name="z", offset=8, datatype=FLOAT32, count=1),
    ]
    point_step = 12
    data = np.ascontiguousarray(points_xyz, dtype=np.float32).tobytes()

    return PointCloud2(
        header=Header(stamp=Time(sec=sec, nanosec=nanosec), frame_id=frame_id),
        height=1,
        width=len(points_xyz),
        fields=fields,
        is_bigendian=False,
        point_step=point_step,
        row_step=point_step * len(points_xyz),
        data=np.frombuffer(data, dtype=np.uint8),
        is_dense=True,
    )


REQUIRED_CLIENT_NAMES = (
    "SensorInfo",
    "XYZLut",
    "ScanBatcher",
    "LidarPacket",
    "LidarScan",
)


def _import_ouster_client():
    """Returns the ouster-sdk module holding SensorInfo, XYZLut, ScanBatcher, LidarPacket, and LidarScan."""
    errors = []
    for import_desc, do_import in [
        (
            "ouster.sdk.client",
            lambda: __import__("ouster.sdk.client", fromlist=["client"]),
        ),
        ("ouster.sdk.core", lambda: __import__("ouster.sdk.core", fromlist=["core"])),
        ("ouster.client", lambda: __import__("ouster.client", fromlist=["client"])),
    ]:
        try:
            module = do_import()
        except ImportError as e:
            errors.append(f"  {import_desc}: {e}")
            continue
        missing = [name for name in REQUIRED_CLIENT_NAMES if not hasattr(module, name)]
        if missing:
            errors.append(
                f"  {import_desc}: found the module, but it is missing "
                f"{', '.join(missing)}"
            )
            continue
        print(f"  Using ouster-sdk client API from: {import_desc}")
        return module
    raise ImportError(
        "Could not find the ouster-sdk client API (SensorInfo, XYZLut, "
        "ScanBatcher, LidarPacket, LidarScan) under any known location. "
        "Tried:\n" + "\n".join(errors) + "\n"
        "Run this to see what your installed ouster-sdk actually exposes, "
        "then update _import_ouster_client() in this script to match:\n"
        '  python -c "import ouster.sdk as s, pkgutil; '
        'print([m.name for m in pkgutil.iter_modules(s.__path__)])"'
    )


def _fill_sized_packet(client, size, raw_array):
    packet = client.LidarPacket(size)
    packet.buf[:] = raw_array
    return packet


def _fill_format_packet(client, packet_format, raw_array):
    packet = client.LidarPacket(packet_format)
    packet.buf[: len(raw_array)] = raw_array
    return packet


def _batch_packet(batcher, packet, scan):
    """Calls whichever of FrameBatcher.batch(packet, scan) (current) or
    batcher(packet, scan) (older, deprecated as of the 2026-08-14 run's
    own FutureWarning: "FrameBatcher.__call__() is deprecated, use
    FrameBatcher.batch() instead") this install's batcher actually has."""
    batch_method = getattr(batcher, "batch", None)
    if callable(batch_method):
        return batch_method(packet, scan)
    return batcher(packet, scan)


def _print_first_raw_message_diagnostics(topic, rawdata):
    """Prints the first raw message's total length, a hex dump of its first 64 bytes, and whether parse_packet_msg_buf()'s layout is internally consistent for this message (encapsulation header + length prefix + payload."""
    print(f"  [debug] first raw message on '{topic}': {len(rawdata)} bytes total")
    print(f"  [debug] first 64 bytes (hex): {rawdata[:64].hex(' ')}")
    try:
        buf = parse_packet_msg_buf(rawdata)
    except ValueError as e:
        print(f"  [debug] parse_packet_msg_buf() could not parse this message: {e}")
        return
    leftover = len(rawdata) - (4 + 4 + len(buf))
    if leftover == 0:
        print(
            f"  [debug] parse_packet_msg_buf() extracted {len(buf)} bytes and "
            f"fully accounts for this message (4-byte encapsulation + 4-byte "
            f"length prefix + {len(buf)}-byte payload = {len(rawdata)} bytes total, "
            f"no leftover) - this bag's PacketMsg layout looks right."
        )
    else:
        print(
            f"  [debug] WARNING: parse_packet_msg_buf() extracted {len(buf)} bytes, "
            f"but that leaves {leftover} bytes of this message unaccounted for - "
            f"this bag's PacketMsg layout may differ from what this function "
            f"assumes (see its own docstring)."
        )


def count_raw_messages(bag_path, topic):
    """Counts messages on `topic` without attempting to parse their content at all - used by count_imu_packets(), which only needs a count, not the packet bytes, so it shouldn't depend on parse_packet_msg_buf()'s CDR layout."""
    from rosbags.rosbag2 import Reader

    with Reader(bag_path) as reader:
        connections = [c for c in reader.connections if c.topic == topic]
        if not connections:
            return 0
        return sum(1 for _ in reader.messages(connections=connections))


def _build_lidar_packet(client, info, raw_array, packet_format, announce):
    """Builds a client.LidarPacket carrying raw_array's bytes."""
    n = len(raw_array)
    attempts = []
    if packet_format is not None:
        attempts.append(
            (
                "LidarPacket(PacketFormat) + packet.buf[:n] = bytes",
                lambda: _fill_format_packet(client, packet_format, raw_array),
            )
        )
    attempts.append(
        (
            "LidarPacket(size) + packet.buf[:] = bytes",
            lambda: _fill_sized_packet(client, n, raw_array),
        )
    )
    attempts.append(
        ("LidarPacket(bytes, info)", lambda: client.LidarPacket(raw_array, info))
    )

    errors = []
    for desc, fn in attempts:
        try:
            packet = fn()
        except Exception as e:
            errors.append(f"  {desc}: {type(e).__name__}: {e}")
            continue
        if announce:
            print(f"  Building LidarPacket via: {desc}")
        return packet

    raise RuntimeError(
        "Could not construct a client.LidarPacket from this bag's raw packet "
        "bytes using any known method. Tried:\n" + "\n".join(errors) + "\n"
        "Run this to see the real constructor and .buf behavior for your "
        "installed ouster-sdk version, then update _build_lidar_packet() in "
        "this script to match:\n"
        '  python -c "from ouster.sdk import core as c; help(c.LidarPacket)"'
    )


def decode_lidar_packets(
    input_path, output_path, lidar_topic, metadata_topic, points_topic, typestore
):
    """Does the actual packet -> scan -> PointCloud2 conversion. Returns
    (n_scans, n_packets)."""
    client = _import_ouster_client()
    from rosbags.rosbag2 import Writer

    print(f"Reading sensor metadata from: {metadata_topic}")
    metadata_json = read_metadata_json(input_path, metadata_topic, typestore)
    info = client.SensorInfo(metadata_json)
    print(
        f"  Metadata loaded ({info.format.pixels_per_column} x "
        f"{info.format.columns_per_frame} per scan)."
    )

    # Used both to build empty LidarPacket objects (see _build_lidar_packet)
    # and, if it exposes an expected packet size, to sanity-check that the
    # bytes parse_packet_msg_buf() extracted are a plausible lidar packet
    # and not a sign that its CDR layout assumptions are wrong for this bag.
    packet_format = None
    expected_packet_size = None
    try:
        packet_format = client.PacketFormat(info)
        expected_packet_size = getattr(packet_format, "lidar_packet_size", None)
    except Exception as e:
        print(
            f"  (Could not build a PacketFormat to sanity-check packet size: "
            f"{type(e).__name__}: {e} - continuing without that check.)"
        )

    xyz_lut = client.XYZLut(info)
    batcher = client.ScanBatcher(info)
    scan = client.LidarScan(info)

    n_scans = 0
    n_packets = 0
    n_skipped_wrong_size = 0
    size_warned = False

    print(f"Decoding lidar packets from: {lidar_topic}")
    with Writer(output_path) as writer:
        pc2_msgtype = "sensor_msgs/msg/PointCloud2"
        pc2_connection = writer.add_connection(
            points_topic, pc2_msgtype, typestore=typestore
        )

        packets = iter_raw_packets(
            input_path,
            lidar_topic,
            on_first_message=lambda rawdata: _print_first_raw_message_diagnostics(
                lidar_topic, rawdata
            ),
        )
        for timestamp_ns, raw_bytes in packets:
            n_packets += 1
            raw_array = np.frombuffer(raw_bytes, dtype=np.uint8)

            if expected_packet_size and len(raw_array) != expected_packet_size:
                if not size_warned:
                    print(
                        f"  WARNING: packet {n_packets} is {len(raw_array)} bytes, but "
                        f"this sensor's format expects {expected_packet_size} bytes per "
                        f"lidar packet. This most likely means parse_packet_msg_buf()'s "
                        f"CDR layout assumptions (see its own docstring, and the [debug] "
                        f"lines printed above) don't match this bag - probably the "
                        f"frame_id length/padding math. Every wrong-sized packet is "
                        f"SKIPPED rather than handed to ouster-sdk - a wrong-sized buffer "
                        f"reaching the native batcher is what crashed the whole process "
                        f"(access violation) on the 2026-08-14 run that didn't have this "
                        f"check. (This warning only prints once; skipped-packet count is "
                        f"in the final summary.)"
                    )
                    size_warned = True
                n_skipped_wrong_size += 1
                continue

            packet = _build_lidar_packet(
                client, info, raw_array, packet_format, announce=(n_packets == 1)
            )
            if _batch_packet(batcher, packet, scan):
                xyz = xyz_lut(scan).reshape(-1, 3)
                valid = ~np.all(xyz == 0, axis=1)
                points = xyz[valid].astype(np.float32)
                msg = build_pointcloud2_message(typestore, points, timestamp_ns)
                writer.write(
                    pc2_connection,
                    timestamp_ns,
                    typestore.serialize_cdr(msg, pc2_msgtype),
                )
                n_scans += 1
                if n_scans % 20 == 0:
                    print(
                        f"  {n_scans} scans decoded ({n_packets} packets read so far)"
                    )

    print(
        f"  {n_scans} scans decoded from {n_packets} lidar packets "
        f"({n_skipped_wrong_size} skipped for wrong size)."
    )
    if n_skipped_wrong_size and n_scans == 0:
        print(
            "  Every packet was skipped for wrong size, so nothing was decoded. The "
            "[debug] lines printed above (first raw message's hex + parsed frame_id) are "
            "the next thing to look at - they show what parse_packet_msg_buf() is actually "
            "seeing in this bag, which is what its CDR layout needs to be fixed against."
        )
    return n_scans, n_packets


def count_imu_packets(input_path, imu_topic):
    """Counts IMU packets present, without decoding them yet - see the module docstring's IMU note."""
    if not imu_topic:
        return 0
    return count_raw_messages(input_path, imu_topic)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--input", required=True, help="ROS2 bag folder of raw Ouster packets"
    )
    parser.add_argument(
        "--output", required=True, help="New ROS2 bag folder to write (must not exist)"
    )
    parser.add_argument(
        "--lidar-topic",
        default="/ouster/lidar_packets",
        help="Raw lidar packet topic. Default: /ouster/lidar_packets",
    )
    parser.add_argument(
        "--imu-topic",
        default="/ouster/imu_packets",
        help="Raw IMU packet topic, or '' to skip. Default: /ouster/imu_packets "
        "(counted only - not decoded yet, see module docstring)",
    )
    parser.add_argument(
        "--metadata-topic",
        default="/ouster/metadata",
        help="Sensor metadata JSON topic. Default: /ouster/metadata",
    )
    parser.add_argument(
        "--points-topic",
        default="/ouster/points",
        help="Topic name to write decoded points to in the output bag. "
        "Default: /ouster/points",
    )
    args = parser.parse_args()

    try:
        import ouster.sdk  # noqa: F401
    except ImportError as e:
        print(f"ERROR: could not import ouster-sdk ({e}). Run: pip install ouster-sdk")
        return 1
    try:
        import rosbags  # noqa: F401
    except ImportError as e:
        print(f"ERROR: could not import rosbags ({e}). Run: pip install rosbags")
        return 1

    input_path = Path(args.input)
    output_path = Path(args.output)
    if not input_path.exists():
        print(f"ERROR: input bag not found: {input_path}")
        return 1
    if output_path.exists():
        print(f"ERROR: output bag already exists: {output_path}")
        return 1

    from rosbags.typesys import Stores, get_typestore

    typestore = get_typestore(Stores.ROS2_HUMBLE)

    try:
        n_scans, n_packets = decode_lidar_packets(
            str(input_path),
            str(output_path),
            args.lidar_topic,
            args.metadata_topic,
            args.points_topic,
            typestore,
        )
    except Exception as e:
        print(f"ERROR: decoding failed: {type(e).__name__}: {e}")
        print(
            "See this script's STATUS note in its own docstring for the likely causes "
            "(parse_packet_msg_buf's CDR layout assumptions, ouster-sdk version, "
            "rosbags version)."
        )
        return 1

    if n_scans == 0:
        print(
            "WARNING: zero scans decoded - the output bag was still created, but has no "
            "usable data in it. Check --lidar-topic and --metadata-topic are correct for "
            "this bag (see pipeline_core.py's inspect_rosbag_topics() / this bag's own "
            "metadata.yaml for the real topic names)."
        )

    if args.imu_topic:
        n_imu = count_imu_packets(str(input_path), args.imu_topic)
        if n_imu:
            print(
                f"\n{n_imu} IMU packets found on '{args.imu_topic}' - NOT decoded into "
                "this output bag yet (see module docstring). Extend "
                "decode_imu_packet()/decode_lidar_packets() here once a LIO-based method "
                "needs them."
            )

    print(f"\nSaved decoded bag to: {output_path}")
    print(
        f"This bag has a real 'sensor_msgs/msg/PointCloud2' topic ('{args.points_topic}') "
        "and can be read by slam_kiss_icp.py's rosbag dataloader, or played into any "
        "ROS2 node with 'ros2 bag play'."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
