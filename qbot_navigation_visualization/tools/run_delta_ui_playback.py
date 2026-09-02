#!/usr/bin/env python3
"""Drive the Delta mapping worker from a rosbag without adding playback UI."""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request


def request_json(endpoint: str, path: str, payload=None):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        endpoint.rstrip("/") + path,
        data=body,
        headers={"Content-Type": "application/json"} if body else {},
        method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=3.0) as response:
            data = response.read()
            return json.loads(data) if data else {}
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(detail) from error


def wait_for_state(endpoint, terminal_states, timeout):
    deadline = time.monotonic() + timeout
    last_line = None
    while time.monotonic() < deadline:
        state = request_json(endpoint, "/state")
        line = (
            state.get("mapping"),
            state.get("map_version"),
            (state.get("map") or {}).get("known_cells"),
            state.get("pose"),
        )
        if line != last_line:
            print(
                "state={} map_version={} known_cells={} pose={}".format(*line),
                flush=True,
            )
            last_line = line
        if state.get("mapping") in terminal_states:
            return state
        time.sleep(0.5)
    raise TimeoutError(f"Timed out waiting for {sorted(terminal_states)}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--endpoint", default="http://127.0.0.1:8766"
    )
    parser.add_argument(
        "--bag-path", default="/home/josep/qbot_bags/test_8"
    )
    parser.add_argument("--rate", type=float, default=2.0)
    parser.add_argument(
        "--scan-ring",
        type=int,
        default=64,
        help="Ouster ring reconstructed from raw packets (test_8 uses 64)",
    )
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--finish-and-save")
    parser.add_argument("--cancel-after", type=float)
    args = parser.parse_args()

    health = request_json(args.endpoint, "/health")
    if not health.get("ok"):
        raise RuntimeError("Delta mapping worker did not pass its health check")

    current = request_json(args.endpoint, "/state")
    if current.get("mapping") != "ready":
        raise RuntimeError(
            f"Worker must be ready before playback; current state is {current.get('mapping')}"
        )

    request_json(
        args.endpoint,
        "/mapping/start",
        {
            "source": "playback",
            "bag_path": args.bag_path,
            "playback_rate": args.rate,
            "scan_ring": args.scan_ring,
        },
    )
    print(
        f"Playback mapping accepted using reconstructed ring {args.scan_ring}. "
        "Keep DeltaUI_Joseph open to see the live map."
    )

    if args.cancel_after is not None:
        time.sleep(args.cancel_after)
        request_json(args.endpoint, "/mapping/cancel", {})
        state = wait_for_state(args.endpoint, {"cancelled", "error"}, 30.0)
        if state.get("mapping") != "cancelled":
            raise RuntimeError(state.get("error") or state.get("detail"))
        return

    state = wait_for_state(
        args.endpoint, {"playback_complete", "error"}, args.timeout
    )
    if state.get("mapping") == "error":
        raise RuntimeError(state.get("error") or state.get("detail"))

    if args.finish_and_save:
        request_json(
            args.endpoint,
            "/mapping/finish-save",
            {"name": args.finish_and_save},
        )
        state = wait_for_state(
            args.endpoint, {"saved", "save_failed", "error"}, 60.0
        )
        if state.get("mapping") != "saved":
            raise RuntimeError(state.get("error") or state.get("detail"))
        print(json.dumps(state.get("saved_paths"), indent=2))
    else:
        print("Playback complete. Use Finish & Save or Cancel in DeltaUI_Joseph.")


if __name__ == "__main__":
    main()
