#!/usr/bin/env python3
"""
decode_raw_packets.py
========================
Decodes a ROS2 bag of raw Ouster sensor packets (ouster_sensor_msgs/msg/
PacketMsg on a lidar packets topic) into a new ROS2 bag carrying decoded
sensor_msgs/msg/PointCloud2 messages instead.

Why this exists: recording raw packets on a bandwidth/storage-constrained
capture device (e.g. a Jetson) is far cheaper than recording decoded
point clouds live - a raw ouster_sensor_msgs/msg/PacketMsg is close to
the sensor's own UDP wire format, while a sensor_msgs/msg/PointCloud2
carries a full float32 XYZ per point plus ROS message overhead. This
script does the decode step ONCE, after transfer off the capture device,
producing a standard decoded bag that ANY point-cloud-consuming SLAM
method can read - not just this pipeline's own backends.

slam_kiss_icp.py's rosbag dataloader is the immediate motivation (it
needs a real sensor_msgs/msg/PointCloud2 topic - see pipeline_core.py's
inspect_rosbag_topics(), which is what notices a bag needs this before
that dataloader gets a chance to fail on it), but the output here is
exactly what most ROS-based SLAM packages already expect as input, via a
plain `ros2 bag play` - this is deliberately NOT a KISS-ICP-specific
conversion.

Ouster CLI does NOT need this - it reads raw-packet bags directly
(pipeline_core.py's build_slam_command already relies on that). Use this
script specifically to unlock backends that only understand decoded
points.

Requires:
    pip install ouster-sdk rosbags numpy

Usage:
    python decode_raw_packets.py --input test_6 --output test_6_decoded
    python decode_raw_packets.py --input test_6 --output test_6_decoded \
        --lidar-topic /ouster/lidar_packets --metadata-topic /ouster/metadata

STATUS: RESOLVED AND CONFIRMED WORKING END TO END. Six real-data runs
against test_6, all on 2026-08-14 - summarized briefly here; see this
script's version history for each run's full detail if needed:

  Run 1: `from ouster.sdk import client` failed - fixed by
  _import_ouster_client() below (resolves to ouster.sdk.core on this
  install, confirmed by Run 2).

  Run 2: rosbags' generic typestore.deserialize_cdr(), fed a
  hand-written PacketMsg definition referencing "std_msgs/Header"
  unqualified, hit a UnicodeDecodeError - fixed by parse_packet_msg_buf()
  below, which reads the raw CDR bytes by hand instead of asking rosbags
  to understand this message type at all.

  Run 3: `client.LidarPacket(raw_bytes, info)` doesn't exist on this
  ouster-sdk version (TypeError) - fixed by _build_lidar_packet() below,
  which builds an empty packet (from a PacketFormat or a size) and fills
  its .buf attribute instead. This run's error also showed
  parse_packet_msg_buf() was extracting only 319 bytes - a first sign
  its layout was still wrong.

  Run 4: the process crashed with a native access violation (Windows
  exit code 3221225477 / 0xC0000005 - a C++-level segfault, not a
  catchable Python exception) instead of raising a normal error, and
  every print() before the crash was lost to stdout buffering (this
  script's output is piped into the applet's log window, not a real
  terminal). Fixed on two fronts: print() is now forced to flush
  immediately (right after the imports below), and a packet whose size
  doesn't match PacketFormat's expected lidar_packet_size is now SKIPPED
  before ever reaching ouster-sdk's native code, rather than risking
  another crash by handing it a wrong-sized buffer.

  Run 5 - RESOLVED, confirmed against real bytes: with the crash gone,
  _debug_dump_first_raw_message() printed a hex dump of a real message
  (24904 bytes total) and proved parse_packet_msg_buf()'s layout was
  wrong in a specific, fixable way - bytes 4-7 (right after the 4-byte
  CDR encapsulation header) decoded to 24896, exactly this sensor's
  PacketFormat.lidar_packet_size, and 4 + 4 + 24896 exactly equals
  24904 with nothing left over. That means this bag's PacketMsg has NO
  header field at all, just `uint8[] buf` directly after the
  encapsulation header - not the Header-then-buf shape this script
  assumed from the start (copied from a commonly-published .msg
  definition that turned out not to match this build). Fixed by
  removing the Header-reading code from parse_packet_msg_buf() entirely
  - see that function's own docstring for the exact byte accounting.

  This last fix was grounded directly in real message bytes, not a
  guess, so confidence was already high - Run 6 below is the
  confirmation that it actually produces correct output end to end.

  Run 6 - CONFIRMED WORKING, full end-to-end success against test_6:
  _debug_dump_first_raw_message() reported "fully accounts for this
  message, no leftover", and the full decode ran to completion - 748
  scans decoded from 47906 lidar packets, 0 packets skipped for wrong
  size, output bag saved with a real sensor_msgs/msg/PointCloud2 topic
  ('/ouster/points'), script exited with "[finished successfully]".
  47906 packets / 748 scans is about 64 packets per scan, a plausible
  ratio for this sensor's column count - nothing here looks truncated
  or wrong. The CDR layer, LidarPacket construction, batching, and
  XYZLut projection are all confirmed working together against this
  real capture. No further action needed on the decode path itself.

You may also see lines like `[ouster::sdk::core] error: Duplicate
metadata type? Already registered type found: ...`, and FutureWarning
lines about ScanBatcher/LidarScan/LidarPacket(size)/FrameBatcher.__call__
being deprecated, printed before or during this script's own output.
All of these come from ouster-sdk's own library at import/call time, not
from this script, and were not fatal on any of the six 2026-08-14
runs (including the fully successful Run 6) - treat them as noise
unless the script also exits with an error right next to them. The
deprecated names still work, just flagged for removal in a future
"1.0" release with no date given yet - this script now prefers their
replacements where that was a simple substitution (PacketFormat-based
LidarPacket, FrameBatcher.batch()).

If it still fails, the most likely remaining culprits are:
  1. An installed ouster-sdk version whose SensorInfo / PacketFormat /
     ScanBatcher / XYZLut / LidarPacket / LidarScan constructor
     signatures or attribute names (e.g. .buf) differ from what is
     assumed here, even once the right module is found - check
     `python -c "from ouster.sdk import core as c; help(c.LidarPacket)"`
     and `help(c.PacketFormat)` (swap in whichever module name the
     console output reported using) against your installed version and
     adjust _build_lidar_packet() below.
  2. An installed 'rosbags' version whose Reader/Writer or
     serialize_cdr API differs from what's called here for reading
     metadata and writing the output PointCloud2 bag - check the
     'rosbags' changelog for your installed version if
     read_metadata_json() or build_pointcloud2_message() raise an
     AttributeError.
  3. If the "[debug] first raw message" diagnostic (printed by
     _print_first_raw_message_diagnostics(), called from inside
     iter_raw_packets() - formerly a separate _debug_dump_first_raw_message()
     function, folded in on 2026-08-14 to stop opening two Readers over
     the same bag/topic) itself starts reporting a leftover/mismatch
     again (a different bag, a different ouster_ros build),
     parse_packet_msg_buf()'s layout needs adjusting again - see that
     function's docstring for how to add a header back in if a future
     capture actually has one.

IMU packets (ouster_sensor_msgs/msg/PacketMsg on an IMU topic, e.g.
/ouster/imu_packets) are read and counted but NOT yet decoded into
sensor_msgs/msg/Imu messages in the output bag - that only matters once
a LIO (lidar-inertial) method is actually wired into this pipeline.
Extend decode_imu_packet() below when that's needed.
"""

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
    types = get_types_from_msg(PACKET_MSG_DEFINITION, "ouster_sensor_msgs/msg/PacketMsg")
    typestore.register(types)
    return typestore


def parse_packet_msg_buf(rawdata):
    """Manually parses a CDR-encoded ouster_sensor_msgs/msg/PacketMsg
    message and returns the raw 'buf' bytes - the one complete raw
    sensor UDP packet this message carries.

    Reads the fixed CDR (XCDR1) wire layout directly instead of going
    through rosbags' dynamic message-type system - see the note above
    PACKET_MSG_DEFINITION for why. Layout, after the standard 4-byte CDR
    encapsulation header:

        uint32 buf_len          (4 bytes)
        bytes  buf               (buf_len bytes)  -- this is the return value

    CONFIRMED on 2026-08-14 against a real message from test_6, by
    hex-dumping it (see _print_first_raw_message_diagnostics()): the message
    was 24904 bytes total, and bytes 4-7 (little-endian) decoded to
    24896 - exactly this sensor's PacketFormat.lidar_packet_size - with
    4 (encapsulation) + 4 (this length prefix) + 24896 (payload) exactly
    accounting for all 24904 bytes, no leftover and no gap. That means
    THIS FUNCTION'S EARLIER LAYOUT WAS WRONG: it assumed a leading
    std_msgs/Header (stamp + frame_id string) before buf, matching the
    ouster-ros PACKET_MSG_DEFINITION comment below - but the real
    on-the-wire message for /ouster/lidar_packets in this capture has NO
    header at all, just the buf sequence directly after the
    encapsulation header. Whatever generated this bag's PacketMsg either
    used a header-less variant of the message, or the header field
    exists in the .msg source but isn't actually serialized onto the
    wire in this ouster_ros build - either way, the bytes prove there
    are only 8 bytes of overhead before the payload, not the header's
    ~12+ bytes this function assumed until now.

    ouster_sensor_msgs/msg/PacketMsg is the same message type on both
    the lidar and IMU packet topics (see this bag's own metadata.yaml),
    so this layout should apply to IMU packets too, not just lidar ones
    - not yet separately confirmed with its own hex dump, since IMU
    packets are only counted (see count_raw_messages()), not parsed for
    content.

    If this ever needs to change again for a different capture/ouster_ros
    build (e.g. one that DOES serialize a header), the fix is straightforward: read an
    extra Time (8 bytes) + length-prefixed frame_id string (with padding
    to a 4-byte boundary) before this function's buf_len field - see this
    function's git/version history for that exact code, removed here
    once real data proved it wrong for this bag."""
    if len(rawdata) < 4:
        raise ValueError(
            f"PacketMsg payload too short ({len(rawdata)} bytes) to hold even "
            "a CDR encapsulation header.")
    # Byte 0 is reserved (0x00). Byte 1 is the representation identifier:
    # 0x00/0x02 = big-endian CDR, 0x01/0x03 = little-endian CDR. Bytes 2-3
    # are options, unused here.
    if rawdata[1] not in (0x00, 0x01, 0x02, 0x03):
        raise ValueError(
            f"Unrecognized CDR representation identifier (byte 1 = "
            f"0x{rawdata[1]:02x}) - this message may not be CDR-encoded the "
            "way this function expects. See this function's own docstring.")
    byteorder = "little" if rawdata[1] in (0x01, 0x03) else "big"

    offset = 4  # past the encapsulation header

    if offset + 4 > len(rawdata):
        raise ValueError("PacketMsg payload too short to hold buf's length prefix.")
    buf_len = int.from_bytes(rawdata[offset:offset + 4], byteorder)
    offset += 4

    if offset + buf_len > len(rawdata):
        raise ValueError(
            f"PacketMsg payload too short: buf claims {buf_len} bytes but only "
            f"{len(rawdata) - offset} remain after the encapsulation header. "
            "This usually means this function's manual CDR layout does not "
            "match this bag's actual message layout - see its own docstring.")
    return rawdata[offset:offset + buf_len]


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
    """Yields (timestamp_ns, raw_bytes) for every message on `topic`, in
    bag order - raw_bytes is that message's 'buf' field, i.e. one
    complete raw sensor UDP packet. Parses each message's raw CDR bytes
    directly through parse_packet_msg_buf() - see that function's
    docstring for why this does not go through rosbags' typestore.

    on_first_message: optional callback, invoked once with the FIRST
    message's raw (pre-parse) CDR bytes, before this generator parses and
    yields it. decode_lidar_packets() uses this to print the diagnostic
    hex dump that used to live in a separate _debug_dump_first_raw_message()
    function - that function opened its OWN Reader over the same bag and
    topic just to look at this same first message a second time, before
    this generator opened a second Reader to actually do the real decode
    pass. Routing the diagnostic through this single pass instead removes
    that redundant Reader-open and duplicate first-message read."""
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


def build_pointcloud2_message(typestore, points_xyz, timestamp_ns, frame_id="os_sensor"):
    """Builds a sensor_msgs/msg/PointCloud2 message from an Nx3 float32
    array - x/y/z fields only, matching what slam_kiss_icp.py's own .ply
    writer already keeps (no intensity/reflectivity carried through, to
    keep this as close as possible to what the simplest downstream
    reader - kiss-icp's rosbag dataloader - actually needs)."""
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


REQUIRED_CLIENT_NAMES = ("SensorInfo", "XYZLut", "ScanBatcher", "LidarPacket", "LidarScan")


def _import_ouster_client():
    """Returns the ouster-sdk module holding SensorInfo, XYZLut,
    ScanBatcher, LidarPacket, and LidarScan.

    Confirmed on 2026-08-14 against real hardware data (test_6): the
    ouster-sdk release installed at that time does NOT expose these
    under ouster.sdk.client - `from ouster.sdk import client` fails with
    `ImportError: cannot import name 'client' from 'ouster.sdk'`. The
    [ouster::sdk::core] name in that same run's console output pointed
    at ouster.sdk.core as the likely new location, consistent with
    ouster-sdk's own restructuring around this data (client split into
    core/pcap/osf/sensor). Older, pre-unification installs instead
    expose this as the standalone ouster.client package. This function
    tries all three, in the order most to least likely for a current
    install, so one script keeps working across ouster-sdk versions
    without the caller needing to know which one is installed.

    NOT yet confirmed which of the three actually succeeds against a
    real ouster-sdk install - only that the previous single hard-coded
    import path fails on at least one real, currently-installed version.
    Whichever path succeeds gets printed to the console, so a real run's
    output tells us if this needs another fallback added."""
    errors = []
    for import_desc, do_import in [
        ("ouster.sdk.client", lambda: __import__("ouster.sdk.client", fromlist=["client"])),
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
            errors.append(f"  {import_desc}: found the module, but it is missing "
                           f"{', '.join(missing)}")
            continue
        print(f"  Using ouster-sdk client API from: {import_desc}")
        return module
    raise ImportError(
        "Could not find the ouster-sdk client API (SensorInfo, XYZLut, "
        "ScanBatcher, LidarPacket, LidarScan) under any known location. "
        "Tried:\n" + "\n".join(errors) + "\n"
        "Run this to see what your installed ouster-sdk actually exposes, "
        "then update _import_ouster_client() in this script to match:\n"
        "  python -c \"import ouster.sdk as s, pkgutil; "
        "print([m.name for m in pkgutil.iter_modules(s.__path__)])\""
    )


def _fill_sized_packet(client, size, raw_array):
    packet = client.LidarPacket(size)
    packet.buf[:] = raw_array
    return packet


def _fill_format_packet(client, packet_format, raw_array):
    packet = client.LidarPacket(packet_format)
    packet.buf[:len(raw_array)] = raw_array
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
    """Prints the first raw message's total length, a hex dump of its
    first 64 bytes, and whether parse_packet_msg_buf()'s layout is
    internally consistent for this message (encapsulation header +
    length prefix + payload should exactly account for every byte, with
    nothing left over).

    Formerly _debug_dump_first_raw_message(bag_path, topic), which opened
    its own Reader over the bag to fetch this same first message a second
    time - iter_raw_packets() now calls this (via its on_first_message
    parameter) with the message it already has from its own single pass,
    so decoding a bag no longer opens two Readers over the same topic.
    Behavior/output is otherwise unchanged: still pure printing, still
    called before any packet is handed to ouster-sdk's native code, so
    this ground truth still survives even if something downstream
    crashes, and it still cannot itself cause a native crash.

    This is what caught the real bug on 2026-08-14: the layout this
    function used to assume (a std_msgs/Header before buf) put buf's
    length prefix at byte 12, which read as 136 - clearly not a real
    frame_id length. The actual bytes showed the length prefix sitting
    at byte 4 instead (value 24896, exactly matching this sensor's
    PacketFormat.lidar_packet_size, with 4 + 4 + 24896 exactly equal to
    the message's real 24904-byte total) - proving PacketMsg has no
    header on this bag at all. parse_packet_msg_buf() now reflects that.
    If a future bag/ouster_ros build goes back to including a header,
    the length-mismatch warning below is what will catch it."""
    print(f"  [debug] first raw message on '{topic}': {len(rawdata)} bytes total")
    print(f"  [debug] first 64 bytes (hex): {rawdata[:64].hex(' ')}")
    try:
        buf = parse_packet_msg_buf(rawdata)
    except ValueError as e:
        print(f"  [debug] parse_packet_msg_buf() could not parse this message: {e}")
        return
    leftover = len(rawdata) - (4 + 4 + len(buf))
    if leftover == 0:
        print(f"  [debug] parse_packet_msg_buf() extracted {len(buf)} bytes and "
              f"fully accounts for this message (4-byte encapsulation + 4-byte "
              f"length prefix + {len(buf)}-byte payload = {len(rawdata)} bytes total, "
              f"no leftover) - this bag's PacketMsg layout looks right.")
    else:
        print(f"  [debug] WARNING: parse_packet_msg_buf() extracted {len(buf)} bytes, "
              f"but that leaves {leftover} bytes of this message unaccounted for - "
              f"this bag's PacketMsg layout may differ from what this function "
              f"assumes (see its own docstring).")


def count_raw_messages(bag_path, topic):
    """Counts messages on `topic` without attempting to parse their
    content at all - used by count_imu_packets(), which only needs a
    count, not the packet bytes, so it shouldn't depend on
    parse_packet_msg_buf()'s CDR layout assumptions succeeding (those
    are only confirmed against a real lidar packet so far - see that
    function's docstring)."""
    from rosbags.rosbag2 import Reader

    with Reader(bag_path) as reader:
        connections = [c for c in reader.connections if c.topic == topic]
        if not connections:
            return 0
        return sum(1 for _ in reader.messages(connections=connections))


def _build_lidar_packet(client, info, raw_array, packet_format, announce):
    """Builds a client.LidarPacket carrying raw_array's bytes.

    Confirmed on 2026-08-14 against real hardware data: this ouster-sdk
    version's LidarPacket constructor does NOT accept raw bytes directly
    - `client.LidarPacket(raw_bytes_array, info)`, the API this script
    originally assumed, raised `TypeError: __init__(): incompatible
    function arguments`. The error message itself showed the real
    constructor only accepts a buffer size (`LidarPacket(size: int)`) or
    a PacketFormat (`LidarPacket(packet_format)`) - both allocate an
    EMPTY packet; the actual bytes then need to be copied in afterward,
    assumed here to be through a settable `.buf` attribute (not
    confirmed). PacketFormat-based construction is tried FIRST here,
    since a follow-up real run showed `LidarPacket(size: int)` is itself
    deprecated ("use LidarPacket(fmt: PacketFormat) instead") - and,
    more importantly, that same run then crashed the whole process with
    a native access violation right after building a size-based packet,
    consistent with a wrong buffer size reaching the native batcher.
    Building from PacketFormat instead sizes the packet from the
    sensor's own real format rather than from this script's own
    (possibly wrong) byte count.

    NOT yet confirmed against a real ouster-sdk install - each attempt
    here only removes the specific error seen on its own run. If every
    attempt below fails, the error message says exactly what to check
    next. See decode_lidar_packets()'s packet-size check for why a
    mismatched-size packet is skipped before ever reaching this
    function, rather than being handed to ouster-sdk's native code."""
    n = len(raw_array)
    attempts = []
    if packet_format is not None:
        attempts.append(
            ("LidarPacket(PacketFormat) + packet.buf[:n] = bytes",
             lambda: _fill_format_packet(client, packet_format, raw_array)))
    attempts.append(
        ("LidarPacket(size) + packet.buf[:] = bytes",
         lambda: _fill_sized_packet(client, n, raw_array)))
    attempts.append(("LidarPacket(bytes, info)", lambda: client.LidarPacket(raw_array, info)))

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
        "  python -c \"from ouster.sdk import core as c; help(c.LidarPacket)\""
    )


def decode_lidar_packets(input_path, output_path, lidar_topic, metadata_topic,
                          points_topic, typestore):
    """Does the actual packet -> scan -> PointCloud2 conversion. Returns
    (n_scans, n_packets)."""
    client = _import_ouster_client()
    from rosbags.rosbag2 import Writer

    print(f"Reading sensor metadata from: {metadata_topic}")
    metadata_json = read_metadata_json(input_path, metadata_topic, typestore)
    info = client.SensorInfo(metadata_json)
    print(f"  Metadata loaded ({info.format.pixels_per_column} x "
          f"{info.format.columns_per_frame} per scan).")

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
        print(f"  (Could not build a PacketFormat to sanity-check packet size: "
              f"{type(e).__name__}: {e} - continuing without that check.)")

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
        pc2_connection = writer.add_connection(points_topic, pc2_msgtype,
                                                typestore=typestore)

        packets = iter_raw_packets(
            input_path, lidar_topic,
            on_first_message=lambda rawdata: _print_first_raw_message_diagnostics(
                lidar_topic, rawdata))
        for timestamp_ns, raw_bytes in packets:
            n_packets += 1
            raw_array = np.frombuffer(raw_bytes, dtype=np.uint8)

            if expected_packet_size and len(raw_array) != expected_packet_size:
                if not size_warned:
                    print(f"  WARNING: packet {n_packets} is {len(raw_array)} bytes, but "
                          f"this sensor's format expects {expected_packet_size} bytes per "
                          f"lidar packet. This most likely means parse_packet_msg_buf()'s "
                          f"CDR layout assumptions (see its own docstring, and the [debug] "
                          f"lines printed above) don't match this bag - probably the "
                          f"frame_id length/padding math. Every wrong-sized packet is "
                          f"SKIPPED rather than handed to ouster-sdk - a wrong-sized buffer "
                          f"reaching the native batcher is what crashed the whole process "
                          f"(access violation) on the 2026-08-14 run that didn't have this "
                          f"check. (This warning only prints once; skipped-packet count is "
                          f"in the final summary.)")
                    size_warned = True
                n_skipped_wrong_size += 1
                continue

            packet = _build_lidar_packet(client, info, raw_array, packet_format,
                                          announce=(n_packets == 1))
            if _batch_packet(batcher, packet, scan):
                xyz = xyz_lut(scan).reshape(-1, 3)
                valid = ~np.all(xyz == 0, axis=1)
                points = xyz[valid].astype(np.float32)
                msg = build_pointcloud2_message(typestore, points, timestamp_ns)
                writer.write(pc2_connection, timestamp_ns,
                             typestore.serialize_cdr(msg, pc2_msgtype))
                n_scans += 1
                if n_scans % 20 == 0:
                    print(f"  {n_scans} scans decoded ({n_packets} packets read so far)")

    print(f"  {n_scans} scans decoded from {n_packets} lidar packets "
          f"({n_skipped_wrong_size} skipped for wrong size).")
    if n_skipped_wrong_size and n_scans == 0:
        print("  Every packet was skipped for wrong size, so nothing was decoded. The "
              "[debug] lines printed above (first raw message's hex + parsed frame_id) are "
              "the next thing to look at - they show what parse_packet_msg_buf() is actually "
              "seeing in this bag, which is what its CDR layout needs to be fixed against.")
    return n_scans, n_packets


def count_imu_packets(input_path, imu_topic):
    """Counts IMU packets present, without decoding them yet - see the
    module docstring's IMU note. Uses count_raw_messages() (a plain
    message count, no CDR parsing) rather than iter_raw_packets(), since
    a count doesn't need parse_packet_msg_buf() to succeed - and
    previously would have silently reported 0 if it didn't, since both
    "topic not found" and "this bag's PacketMsg layout doesn't parse"
    raised the same ValueError."""
    if not imu_topic:
        return 0
    return count_raw_messages(input_path, imu_topic)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", required=True, help="ROS2 bag folder of raw Ouster packets")
    parser.add_argument("--output", required=True, help="New ROS2 bag folder to write (must not exist)")
    parser.add_argument("--lidar-topic", default="/ouster/lidar_packets",
                         help="Raw lidar packet topic. Default: /ouster/lidar_packets")
    parser.add_argument("--imu-topic", default="/ouster/imu_packets",
                         help="Raw IMU packet topic, or '' to skip. Default: /ouster/imu_packets "
                              "(counted only - not decoded yet, see module docstring)")
    parser.add_argument("--metadata-topic", default="/ouster/metadata",
                         help="Sensor metadata JSON topic. Default: /ouster/metadata")
    parser.add_argument("--points-topic", default="/ouster/points",
                         help="Topic name to write decoded points to in the output bag. "
                              "Default: /ouster/points")
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
            str(input_path), str(output_path), args.lidar_topic, args.metadata_topic,
            args.points_topic, typestore)
    except Exception as e:
        print(f"ERROR: decoding failed: {type(e).__name__}: {e}")
        print("See this script's STATUS note in its own docstring for the likely causes "
              "(parse_packet_msg_buf's CDR layout assumptions, ouster-sdk version, "
              "rosbags version).")
        return 1

    if n_scans == 0:
        print("WARNING: zero scans decoded - the output bag was still created, but has no "
              "usable data in it. Check --lidar-topic and --metadata-topic are correct for "
              "this bag (see pipeline_core.py's inspect_rosbag_topics() / this bag's own "
              "metadata.yaml for the real topic names).")

    if args.imu_topic:
        n_imu = count_imu_packets(str(input_path), args.imu_topic)
        if n_imu:
            print(f"\n{n_imu} IMU packets found on '{args.imu_topic}' - NOT decoded into "
                  "this output bag yet (see module docstring). Extend "
                  "decode_imu_packet()/decode_lidar_packets() here once a LIO-based method "
                  "needs them.")

    print(f"\nSaved decoded bag to: {output_path}")
    print(f"This bag has a real 'sensor_msgs/msg/PointCloud2' topic ('{args.points_topic}') "
          "and can be read by slam_kiss_icp.py's rosbag dataloader, or played into any "
          "ROS2 node with 'ros2 bag play'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
