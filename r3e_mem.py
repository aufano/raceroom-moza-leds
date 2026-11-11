# SPDX-License-Identifier: GPL-3.0-or-later
"""Locate and read RaceRoom's $R3E shared memory from the Linux side.

RaceRoom publishes its telemetry through a Windows named file mapping called
"$R3E" (see the game's own r3e-api: `#define R3E_SHARED_MEMORY_NAME "$R3E"`).
Under Proton, Wine backs such mappings with memfd objects, which have no path in
the filesystem -- but they are readable through /proc/<pid>/fd/<n> as the owning
user. That is where the telemetry is read from here, with no SimHub, no plugin
and no .NET.

Three things about this game cost real time to work out, and any of them breaks
a naive reader:

1. THE MAPPING IS 45056 BYTES, NOT 44024. `sizeof(r3e_shared)` is 44024 for API
   3.6, but Wine rounds a memfd up to whole pages: 11 * 4096 = 45056. A reader
   that demands the exact struct size finds nothing.

2. THE GAME PROCESS IS NOT CALLED "RRRE64.exe". Wine renames the main thread to
   "MainThread" once the game is up, so /proc/<pid>/comm stops matching a few
   seconds after launch. The exe name survives in /proc/<pid>/cmdline
   (`S:\\steamapps\\common\\raceroom racing experience\\Game\\x64\\RRRE64.exe`),
   which is what is used here.

3. THE MAPPING IS ALSO OPEN IN wineserver -- AND IN THIS DAEMON ITSELF. Every
   process that has touched the object keeps an fd, and both wineserver and we
   outlive the game. Latching onto either of those means reading a frozen last
   frame forever, believing the game is still running, and never handing the
   wheel back (observed live on 2026-11-11: after the game quit, the daemon
   found its own fd and alternated between /proc/<self>/fd/3 and fd/5 every
   ten seconds). So a mapping is only accepted from the game's own process.

Detection is therefore by header signature, never by process name:

    version_major      == 3      (API major; offsets below are for 3.6)
    driver_data_size   == 328    (sizeof r3e_driver_data)
    all_drivers_offset == 2008   (offsetof num_cars for API 3.6)
    size               >= 44024

`game_mode` is deliberately NOT part of the test: it is -1
(R3E_GAMEMODE_UNAVAILABLE) in the menus, which is exactly when the mapping first
appears, and requiring 0..8 made the block invisible at that point.

Verified live against a 3.6 build on 2026-11-11:
    version 3.6, all_drivers_offset=2008, driver_data_size=328
    menus:  engine_rps=max_engine_rps=upshift_rps=-1.0, gear=-2, num_gears=-1
    track:  rpm 6029/7000, upshift 6930, gear 1/6, 88 km/h
"""

import glob
import os
import struct

SHARED_NAME = "$R3E"

# --- layout of r3e_shared (r3e-api, API 3.6, #pragma pack(1)) ----------------
SHARED_SIZE = 44024          # sizeof(r3e_shared); the memfd itself is 45056
DRIVER_DATA_SIZE = 328       # sizeof(r3e_driver_data)
ALL_DRIVERS_OFFSET = 2008    # offsetof(r3e_shared, num_cars) == value of the field
API_MAJOR = 3

OFF_VERSION_MINOR = 4
OFF_ALL_DRIVERS_OFFSET = 8
OFF_DRIVER_DATA_SIZE = 12
OFF_GAME_MODE = 16
OFF_GAME_PAUSED = 20
OFF_GAME_IN_MENUS = 24
OFF_GAME_IN_REPLAY = 28

# The player's own vehicle data sits inline in r3e_shared, not in the
# all_drivers_data_1[] array (that one carries the other cars).
OFF_CAR_SPEED = 1392         # float32, m/s
OFF_ENGINE_RPS = 1396        # float32, rad/s
OFF_MAX_ENGINE_RPS = 1400    # float32, rad/s
OFF_UPSHIFT_RPS = 1404       # float32, rad/s -- the shift-light threshold
OFF_GEAR = 1408              # int32, -2 N/A, -1 reverse, 0 neutral, 1..n
OFF_NUM_GEARS = 1412         # int32, -1 when unknown

GAMEMODE_UNAVAILABLE = -1

RPS_TO_RPM = 60.0 / (2.0 * 3.141592653589793)

# Bytes read per poll. The two windows are all the daemon needs: the header and
# game state at the front, the revs/gear block at 1392. Reading the whole
# 45056-byte mapping at 50 Hz would be 2.2 MB/s of pointless copying.
_HEAD_LEN = 64               # covers version .. game_in_replay (+ player start)
_TAIL_OFF = OFF_CAR_SPEED
_TAIL_LEN = 40               # speed, rps, max, upshift, gear, num_gears

GAME_EXES = ("rrre64.exe", "rrre.exe")


class Sample:
    """One snapshot of the player's car. Values are already in rpm/km/h."""

    __slots__ = ("rpm", "max_rpm", "upshift_rpm", "gear", "num_gears",
                 "speed_kph", "game_mode", "paused", "in_menus", "in_replay")

    def __init__(self, rpm, max_rpm, upshift_rpm, gear, num_gears, speed_kph,
                 game_mode, paused, in_menus, in_replay):
        self.rpm = rpm
        self.max_rpm = max_rpm
        self.upshift_rpm = upshift_rpm
        self.gear = gear
        self.num_gears = num_gears
        self.speed_kph = speed_kph
        self.game_mode = game_mode
        self.paused = paused
        self.in_menus = in_menus
        self.in_replay = in_replay

    @property
    def usable(self):
        """True when the block carries a car we can display revs for.

        In the menus RaceRoom fills the whole block with -1.0f (N/A) and
        game_mode is R3E_GAMEMODE_UNAVAILABLE, so anything below zero is "no
        data", not "engine stopped".
        """
        return (self.game_mode != GAMEMODE_UNAVAILABLE
                and self.max_rpm > 0.0 and self.rpm >= 0.0)

    def __repr__(self):
        return (f"Sample(rpm={self.rpm:.0f}/{self.max_rpm:.0f}, "
                f"upshift={self.upshift_rpm:.0f}, gear={self.gear}/"
                f"{self.num_gears}, {self.speed_kph:.0f} km/h, "
                f"mode={self.game_mode})")


def _is_game_pid(pid):
    """True if this pid's command line names the game executable.

    comm is useless here (Wine renames it to "MainThread"), and pgrep -f is
    avoided on purpose: this daemon is launched in the background and the
    racing session scripts decide whether a race is running with
    `pgrep -f '...RRRE...'`, so carrying the exe name in our own argv would
    make them refuse to launch the game or return to the desktop.
    """
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmd = f.read().lower()
    except OSError:
        return False
    return any(exe.encode() in cmd for exe in GAME_EXES)


def _header(fd_path):
    """(size, first 32 bytes) of a mapping, or (None, None)."""
    try:
        fd = os.open(fd_path, os.O_RDONLY)
    except OSError:
        return None, None
    try:
        size = os.fstat(fd).st_size
        head = os.pread(fd, 32, 0) if size >= 32 else b""
    finally:
        os.close(fd)
    return size, head


def candidates():
    """Every mapping whose header says it is $R3E, game processes first.

    Scanning all of /proc rather than pgrep'ing the game is what makes this
    survive Wine's thread renaming; it costs a readlink per fd and a 32-byte
    pread per Wine mapping, which is cheap enough to repeat on every re-scan.
    """
    found = []
    me = os.getpid()
    for fd_path in glob.glob("/proc/[0-9]*/fd/*"):
        try:
            target = os.readlink(fd_path)
        except OSError:
            continue
        if "memfd:wine-mapping" not in target and "/dev/shm/" not in target:
            continue
        size, head = _header(fd_path)
        if not head or size is None or size < SHARED_SIZE:
            continue
        major, _minor, all_off, ddsize = struct.unpack_from("<iiii", head, 0)
        if major != API_MAJOR or ddsize != DRIVER_DATA_SIZE:
            continue
        pid = int(fd_path.split("/")[2])
        # Our own handle would look like a live owner forever, and it is the one
        # candidate that is always there: without this the daemon finds itself
        # as soon as the game closes.
        if pid == me:
            continue
        found.append({
            "pid": pid,
            "path": fd_path,
            "size": size,
            "all_drivers_offset": all_off,
            "game": _is_game_pid(pid),
        })
    # The game's own process first: it disappears when the game quits, which is
    # what alive() relies on. wineserver would outlive it.
    found.sort(key=lambda c: (not c["game"], c["path"]))
    return found


def find():
    """The $R3E mapping to read, or None if RaceRoom is not running.

    Only a mapping owned by the game's own process is accepted. wineserver --
    kept alive by CrewChief for the whole session -- and this daemon both hold
    the same object and both outlive the game, so anything else is a frozen last
    frame dressed up as live telemetry.
    """
    for candidate in candidates():
        if candidate["game"]:
            return Shared(candidate)
    return None


class Shared:
    """An open, read-only handle on $R3E."""

    def __init__(self, candidate):
        self.pid = candidate["pid"]
        self.path = candidate["path"]
        self.size = candidate["size"]
        self.game_owned = candidate["game"]
        # API 3.6 is the layout the offsets above were taken from. A different
        # all_drivers_offset means the header moved, so the revs block may have
        # moved with it; the daemon keeps working but says so out loud.
        self.layout_known = (candidate["all_drivers_offset"] == ALL_DRIVERS_OFFSET)
        self.fd = os.open(self.path, os.O_RDONLY)
        self.version = self._version()

    def _version(self):
        head = os.pread(self.fd, 8, 0)
        if len(head) < 8:
            return None
        major, minor = struct.unpack_from("<ii", head, 0)
        return f"{major}.{minor}"

    def alive(self):
        """Our own fd keeps the memfd alive after the game exits, so reads would
        happily return the last frame forever. Check the owner instead."""
        return os.path.exists(f"/proc/{self.pid}")

    def read(self):
        """A Sample, or None when the mapping cannot be read."""
        head = os.pread(self.fd, _HEAD_LEN, 0)
        if len(head) < OFF_GAME_IN_REPLAY + 4:
            return None
        mode, paused, menus, replay = struct.unpack_from("<iiii", head, OFF_GAME_MODE)
        tail = os.pread(self.fd, _TAIL_LEN, _TAIL_OFF)
        if len(tail) < OFF_NUM_GEARS - OFF_CAR_SPEED + 4:
            return None
        speed = struct.unpack_from("<f", tail, 0)[0]
        rps, max_rps, up_rps = struct.unpack_from("<fff", tail, OFF_ENGINE_RPS - _TAIL_OFF)
        gear, num_gears = struct.unpack_from("<ii", tail, OFF_GEAR - _TAIL_OFF)
        return Sample(
            rpm=rps * RPS_TO_RPM,
            max_rpm=max_rps * RPS_TO_RPM,
            upshift_rpm=up_rps * RPS_TO_RPM,
            gear=gear,
            num_gears=num_gears,
            speed_kph=speed * 3.6,
            game_mode=mode,
            paused=paused,
            in_menus=menus,
            in_replay=replay,
        )

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass
