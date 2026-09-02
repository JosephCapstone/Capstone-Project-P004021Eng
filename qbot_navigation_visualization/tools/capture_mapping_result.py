#!/usr/bin/env python3

import argparse
import json
import math
import time
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import rclpy
from geometry_msgs.msg import PoseStamped
from nav_msgs.msg import OccupancyGrid
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy


class MappingCapture(Node):
    def __init__(self):
        super().__init__("mapping_tuning_capture")
        map_qos = QoSProfile(depth=1)
        map_qos.durability = DurabilityPolicy.TRANSIENT_LOCAL
        map_qos.reliability = ReliabilityPolicy.RELIABLE
        self.latest_map = None
        self.path = []
        self.last_pose_time = -math.inf
        self.create_subscription(
            OccupancyGrid, "/navigation/global_map", self.map_callback, map_qos
        )
        self.create_subscription(
            PoseStamped, "/navigation/global_pose", self.pose_callback, 50
        )

    def map_callback(self, message):
        self.latest_map = message

    def pose_callback(self, message):
        stamp = message.header.stamp.sec + message.header.stamp.nanosec * 1e-9
        if stamp - self.last_pose_time >= 0.05:
            self.path.append((message.pose.position.x, message.pose.position.y))
            self.last_pose_time = stamp


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--seconds", type=float, default=33.0)
    args = parser.parse_args()

    rclpy.init()
    node = MappingCapture()
    deadline = time.monotonic() + args.seconds
    while time.monotonic() < deadline:
        rclpy.spin_once(node, timeout_sec=0.1)

    if node.latest_map is None:
        raise RuntimeError("No /navigation/global_map was received")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    message = node.latest_map
    grid = np.asarray(message.data, dtype=np.int16).reshape(
        message.info.height, message.info.width
    )
    image = np.full(grid.shape, 0.80, dtype=float)
    image[grid == 0] = 1.0
    image[grid > 0] = 1.0 - (grid[grid > 0] / 100.0)

    resolution = message.info.resolution
    origin_x = message.info.origin.position.x
    origin_y = message.info.origin.position.y
    extent = [
        origin_x,
        origin_x + message.info.width * resolution,
        origin_y,
        origin_y + message.info.height * resolution,
    ]

    fig, axis = plt.subplots(figsize=(8, 8), dpi=150)
    axis.imshow(image, cmap="gray", origin="lower", extent=extent, vmin=0, vmax=1)
    if node.path:
        points = np.asarray(node.path)
        axis.plot(points[:, 0], points[:, 1], color="#e53935", linewidth=1.4)
        axis.scatter(points[0, 0], points[0, 1], color="#16a34a", s=35, label="start")
        axis.scatter(points[-1, 0], points[-1, 1], color="#2563eb", s=35, label="end")
        axis.legend(loc="upper right")
    axis.set_title(args.name)
    axis.set_xlabel("map x (m)")
    axis.set_ylabel("map y (m)")
    axis.set_aspect("equal")
    fig.tight_layout()
    fig.savefig(output_dir / f"{args.name}.png")
    plt.close(fig)

    path_length = 0.0
    closure_error = None
    if len(node.path) > 1:
        path_length = sum(
            math.hypot(x1 - x0, y1 - y0)
            for (x0, y0), (x1, y1) in zip(node.path, node.path[1:])
        )
        closure_error = math.hypot(
            node.path[-1][0] - node.path[0][0],
            node.path[-1][1] - node.path[0][1],
        )

    metrics = {
        "name": args.name,
        "width": message.info.width,
        "height": message.info.height,
        "resolution": resolution,
        "known_cells": int(np.count_nonzero(grid >= 0)),
        "occupied_cells": int(np.count_nonzero(grid >= 65)),
        "pose_samples": len(node.path),
        "path_length_m": path_length,
        "start_end_distance_m": closure_error,
    }
    (output_dir / f"{args.name}.json").write_text(
        json.dumps(metrics, indent=2), encoding="utf-8"
    )
    print(json.dumps(metrics))
    node.destroy_node()
    rclpy.shutdown()


if __name__ == "__main__":
    main()
