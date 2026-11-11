#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Inspect RaceRoom's $R3E shared memory: header, live revs, offset check.

The daemon reads telemetry through ../r3e_mem.py. This is the tool that was used
to work out that layout, and it is what to run again if a future build bumps the
API: the offsets in r3e_mem.py are for API 3.6, and a shifted block shows up here
as a "signature rpm" hit at an offset other than 1396.

    python3 tools/find_r3e.py            # one shot: header + current values
    python3 tools/find_r3e.py --watch    # 20 s of live samples
    python3 tools/find_r3e.py --wait     # wait for the game and for a real
                                         # engine_rps, then report and sample

Read-only: it never writes to the game, to the wheel or to any file. It also
carries no game executable name in its own command line -- the racing session
scripts decide whether a race is running with `pgrep -f`, and a stray match makes
them refuse to launch the game or return to the desktop.
"""

import argparse
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import r3e_mem

GAME_MODES = {
    -1: "UNAVAILABLE (menus)",
    0: "TRACKTEST", 1: "LEADERBOARDCHALLENGE", 2: "COMPETITION",
    3: "SINGLERACE", 4: "CHAMPIONSHIP", 5: "MULTIPLAYER",
    6: "MULTIPLAYERRANKED", 7: "TRYBEFOREYOUBUY",
}

# Offsets a signature hit is compared against; the first is the one r3e_mem uses.
KNOWN_RPM_OFFSET = r3e_mem.OFF_ENGINE_RPS


def header(shared):
    """Print the fields that identify the block and the game state."""
    head = os.pread(shared.fd, 64, 0)
    major, minor, all_off, ddsize = struct.unpack_from("<iiii", head, 0)
    mode, paused, menus, replay = struct.unpack_from("<iiii", head, 16)
    print(f"  owner pid {shared.pid}, {shared.size} bytes "
          f"(struct is {r3e_mem.SHARED_SIZE}, Wine rounds up to a page)")
    print(f"  version {major}.{minor}   all_drivers_offset {all_off} "
          f"(expected {r3e_mem.ALL_DRIVERS_OFFSET})   "
          f"driver_data_size {ddsize} (expected {r3e_mem.DRIVER_DATA_SIZE})")
    print(f"  game_mode {mode} ({GAME_MODES.get(mode, '?')})  "
          f"paused {paused}  in_menus {menus}  in_replay {replay}")
    if (major, all_off, ddsize) != (r3e_mem.API_MAJOR,
                                    r3e_mem.ALL_DRIVERS_OFFSET,
                                    r3e_mem.DRIVER_DATA_SIZE):
        print("  WARNING: this build does not match the API 3.6 layout; the "
              "offsets in r3e_mem.py may be wrong.")
    return major, minor, all_off, ddsize


def sample(shared):
    s = shared.read()
    if s is None:
        print("  no sample")
        return None
    print(f"  engine_rps @{r3e_mem.OFF_ENGINE_RPS}: {s.rpm:6.0f} rpm "
          f"of {s.max_rpm:6.0f} (upshift {s.upshift_rpm:6.0f})  "
          f"gear {s.gear}/{s.num_gears}  {s.speed_kph:5.0f} km/h  "
          f"usable={s.usable}")
    return s


def scan(buf):
    """Offsets where the (rps, max, upshift, gear, num_gears) pattern appears."""
    hits = []
    for off in range(0, max(0, len(buf) - 24), 4):
        rps, mx, up = struct.unpack_from("<fff", buf, off)
        if not (0.0 <= rps <= 1500.0 and 100.0 <= mx <= 1500.0):
            continue
        if rps > mx * 1.15 or not (0.4 * mx <= up <= 1.05 * mx):
            continue
        gear, ngears = struct.unpack_from("<ii", buf, off + 12)
        if not (-2 <= gear <= 12 and 1 <= ngears <= 12):
            continue
        hits.append((off, rps, mx, up, gear, ngears))
    return hits


def scan_report(shared):
    buf = os.pread(shared.fd, min(shared.size, 1 << 20), 0)
    hits = scan(buf)
    if not hits:
        print("  no revs signature found (menus? the block is all -1.0f there)")
        return
    for off, rps, mx, up, gear, ngears in hits:
        mark = "  <== the offset r3e_mem.py uses" if off == KNOWN_RPM_OFFSET else ""
        print(f"  revs signature @{off}: {rps * r3e_mem.RPS_TO_RPM:6.0f} / "
              f"{mx * r3e_mem.RPS_TO_RPM:6.0f} rpm, upshift "
              f"{up * r3e_mem.RPS_TO_RPM:6.0f}, gear {gear}/{ngears}{mark}")


def watch(shared, seconds):
    print(f"\nSampling for {seconds:.0f} s (Ctrl+C to stop) ...")
    end = time.monotonic() + seconds
    last = None
    try:
        while time.monotonic() < end:
            s = shared.read()
            if s is not None:
                current = (round(s.rpm), s.gear, s.num_gears)
                if current != last:
                    print(f"  rpm {s.rpm:6.0f}/{s.max_rpm:<6.0f} "
                          f"upshift {s.upshift_rpm:5.0f}  "
                          f"gear {s.gear:2d}/{s.num_gears}  "
                          f"{s.speed_kph:5.0f} km/h", flush=True)
                    last = current
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass


def wait_for_track(game_timeout, track_timeout):
    """Wait for the block, then for real revs -- menus carry -1.0f."""
    print(f"Waiting for $R3E (up to {game_timeout:.0f} s) ...", flush=True)
    end = time.monotonic() + game_timeout
    shared = None
    while time.monotonic() < end:
        shared = r3e_mem.find()
        if shared is not None:
            print(f"Found: owner pid {shared.pid}, API {shared.version}",
                  flush=True)
            break
        time.sleep(2.0)
    if shared is None:
        print("Timed out: no shared memory. Is the game running?")
        return 1

    print(f"Waiting for the car to be on track (up to {track_timeout:.0f} s) "
          "and tolerating restarts ...", flush=True)
    end = time.monotonic() + track_timeout
    while time.monotonic() < end:
        if not shared.alive():
            shared.close()
            shared = None
            print("The game closed; waiting for the next start ...", flush=True)
        if shared is None:
            shared = r3e_mem.find()
            if shared is None:
                time.sleep(2.0)
                continue
        s = shared.read()
        if s is not None and s.rpm > 100.0:
            print("On track.\n")
            header(shared)
            sample(shared)
            scan_report(shared)
            watch(shared, 20.0)
            shared.close()
            return 0
        time.sleep(0.2)
    print("Timed out: never saw a running engine.")
    if shared is not None:
        shared.close()
    return 1


def main():
    ap = argparse.ArgumentParser(
        description="Inspect RaceRoom's $R3E shared memory (read-only).")
    ap.add_argument("--watch", action="store_true",
                    help="sample live values for 20 s")
    ap.add_argument("--wait", action="store_true",
                    help="wait for the game and for a running engine, then "
                         "report and sample")
    ap.add_argument("--game-timeout", type=float, default=7200.0,
                    help="seconds to wait for the game (default 7200)")
    ap.add_argument("--track-timeout", type=float, default=7200.0,
                    help="seconds to wait for a running engine (default 7200)")
    args = ap.parse_args()

    if args.wait:
        return wait_for_track(args.game_timeout, args.track_timeout)

    shared = r3e_mem.find()
    if shared is None:
        print("No $R3E found. Is RaceRoom running?")
        return 1
    header(shared)
    sample(shared)
    scan_report(shared)
    if args.watch:
        watch(shared, 20.0)
    shared.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
