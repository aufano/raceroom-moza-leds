#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Derived from lmu-rpm-leds, Copyright (C) 2026 Florian Pauker (GPL-3.0-or-later).
"""Drive a Moza wheel's RPM LEDs from RaceRoom telemetry, on Linux.

The piece Moza Pit House provides on Windows, and that boxflat does not (yet)
implement: feed live engine RPM to the wheelbase so the rev lights work.

How the data is obtained without any third-party plugin:

  RaceRoom publishes its telemetry through a Windows named file mapping called
  "$R3E" (the game's own r3e-api: `#define R3E_SHARED_MEMORY_NAME "$R3E"`).
  Under Proton, Wine backs named file mappings with memfd objects, which have no
  path in the filesystem -- but they are readable through /proc/<pid>/fd/<n> as
  the owning user. See r3e_mem.py for the three traps in that (page-rounded
  size, Wine renaming the process, wineserver holding a second fd).

  The struct is #pragma pack(1), so inside r3e_shared, for the player's car:
      car_speed        @1392 (float32, m/s)
      engine_rps       @1396 (float32, rad/s)   -> rpm = rps * 9.5493
      max_engine_rps   @1400
      upshift_rps      @1404   (the shift-light threshold, ~99 % of max)
      gear             @1408 (int32: -2 N/A, -1 R, 0 N, 1..n)
      num_gears        @1412
  and sizeof(r3e_shared) == 44024 (Wine's memfd is 45056, page-rounded).

LED output speaks the Moza serial protocol (see moza.py); frames were verified
byte-for-byte against boxflat's own encoder.
"""

import argparse
import os
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
import gearscale
import moza
import palette
import r3e_mem
from i18n import _

# How far below the blink threshold the revs must fall before flashing stops,
# as a fraction of the reference.
BLINK_HYSTERESIS = 0.015

# A memfd stays readable through our own fd after the game exits, and the game
# may publish a new mapping on a session change. Re-scanning every few seconds
# is cheap (a readlink per fd, a 32-byte pread per Wine mapping) and keeps the
# daemon from locking onto a dead or stale block.
RESCAN_EVERY = 10.0

# How long the wheelbase stays claimed after the game is gone. A session change
# or a quick restart should not cost a reconnect, but forever is wrong: boxflat
# cannot configure the rim while this daemon holds the port, and re-asserting
# the colour table every 2 s would fight its own. After this, the LEDs are
# handed back (mode 0, bar dark, port closed) and reclaimed on the next start.
IDLE_RELEASE = 60.0

# How often the current mask is resent even though it has not changed.
#
# The rim's firmware plays its own idle effect when it stops receiving frames --
# moza-rev documents the timeout as about two seconds, and Moza's own SimHub
# plugin carries a dedicated LED keepalive for the same reason. Sending only on
# change is not enough: on a long straight in a tall gear the adaptive curve
# keeps the bar full and steady, so nothing was sent for tens of seconds and the
# rim took the LEDs back (blue idle animation) in the middle of a lap.
HEARTBEAT = 0.5


class Wheel:
    """The serial link, held loosely.

    The wheelbase may be switched off when the service starts, and it may be
    unplugged mid-session. Neither is a reason to die -- as a Restart=always
    unit that would just turn into a crash loop -- so the port is opened lazily
    and reopened after a failure.
    """

    RETRY = 3.0
    # The base forgets mode and colours when it is powered off, and boxflat --
    # which writes to the same port -- re-applies its own colour table from
    # time to time. Five frames of about 27 bytes each on a 115200 baud link is
    # nothing, and it keeps the configured look from being overwritten for
    # seconds at a stretch while driving.
    REASSERT = 2.0

    def __init__(self, profile="auto"):
        self._port = None
        self._profile = profile
        self._next_try = 0.0
        self._next_assert = 0.0
        self._complained = False
        self._appearance = None    # (colours, brightness) the rim should show

    def set_profile(self, profile):
        """Wheel generation from the config. A change reopens the port,
        because the device id is decided when it is opened -- and the LEDs are
        handed back first, under the identity they were claimed with."""
        if profile == self._profile:
            return
        self._profile = profile
        self.close()
        self._next_try = 0.0

    def _ensure(self):
        if self._port is not None:
            return True
        now = time.monotonic()
        if now < self._next_try:
            return False
        self._next_try = now + self.RETRY
        try:
            self._port = moza.MozaSerial(profile=self._profile)
            self._assert_mode()
            print(_("Wheel connected: {path} (profile: {profile})").format(
                path=self._port.path, profile=self._port.profile))
            self._complained = False
            return True
        except OSError as exc:
            if not self._complained:
                print(_("Wheel not reachable ({error}) -- will keep trying.")
                      .format(error=exc))
                self._complained = True
            return False

    def set_appearance(self, colors, brightness):
        """Colours and brightness the rim should show.

        A change takes effect on the next frame rather than waiting out the
        re-assert interval, so editing a colour in the app is visible at once
        instead of up to five seconds later.
        """
        wanted = (tuple(tuple(c) for c in colors), int(brightness))
        if wanted != self._appearance:
            self._appearance = wanted
            self._next_assert = 0.0

    def _assert_mode(self):
        """Claim the LEDs, restate the colour table and the brightness."""
        self._port.set_indicator_mode(1)
        if self._appearance:
            colors, brightness = self._appearance
            # A bitmask with no colour table lights every segment black.
            self._port.set_rpm_colors(colors)
            self._port.set_rpm_brightness(brightness)
        else:
            self._port.set_rpm_colors()
        self._next_assert = time.monotonic() + self.REASSERT

    def send(self, mask):
        """True if the frame went out."""
        if not self._ensure():
            return False
        try:
            if time.monotonic() >= self._next_assert:
                self._assert_mode()
            self._port.set_leds(mask)
            return True
        except OSError as exc:
            print(_("Wheel lost ({error}) -- reconnecting.").format(error=exc))
            self.close(restore=False)
            self._complained = True
            return False

    @property
    def connected(self):
        return self._port is not None

    def close(self, restore=True):
        port, self._port = self._port, None
        if port is None:
            return
        try:
            if restore:
                port.set_leds(0)
                port.set_indicator_mode(0)
            port.close()
        except OSError:
            pass


class NullWheel:
    """Stand-in for --dry-run: never opens the serial port.

    The wheelbase is a single exclusive-ish resource that boxflat and the LMU
    daemon also write to, so there has to be a way to watch what the daemon
    would send without touching the hardware.
    """

    connected = False

    def set_profile(self, profile):
        pass

    def set_appearance(self, colors, brightness):
        pass

    def send(self, mask):
        return True

    def close(self, restore=True):
        pass


def leds_lit(frac, start, end, leds):
    """How many LEDs the curve calls for at this fraction of the reference."""
    if frac < start:
        return 0
    span = max(end - start, 1e-6)
    return min(leds, int((frac - start) / span * leds) + 1)


def mask_for(lit, leds, mode="bar"):
    """Turn a count of lit LEDs into the bitmask for that fill style.

    bar     fills left to right, the usual rev bar.
    center  grows inward from both ends and meets in the middle. With an odd
    count the left side takes the extra LED, so the bar never jumps
    sideways as it fills.
    """
    if lit <= 0:
        return 0
    if lit >= leds:
        return (1 << leds) - 1
    if mode == "center":
        left = (lit + 1) // 2
        right = lit // 2
        mask = (1 << left) - 1
        for i in range(right):
            mask |= 1 << (leds - 1 - i)
        return mask
    return (1 << lit) - 1


def reference_rpm(sample, cfg):
    """The rev figure the curve is pinned to for this sample."""
    if cfg["reference"] == "upshift" and sample.upshift_rpm > 0.0:
        return sample.upshift_rpm
    return sample.max_rpm


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", type=float,
                    help=_("fraction of maximum RPM where the first LED lights"))
    ap.add_argument("--end", type=float,
                    help=_("fraction where the whole bar is lit"))
    ap.add_argument("--blink", type=float,
                    help=_("fraction where the bar starts flashing (shift point)"))
    ap.add_argument("--rate", type=float, help=_("updates per second"))
    ap.add_argument("--profile", choices=moza.PROFILES, default=None,
                    help=_("wheel generation: modern (R9 class), "
                           "legacy (R3/R5 class), or auto"))
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help=_("read telemetry but never open the serial port"))
    args = ap.parse_args()

    # Settings live in the config file so a GUI can change them on the fly;
    # anything passed on the command line wins for this run.
    watcher = config.Watcher()
    overrides = {k: v for k, v in vars(args).items()
                 if k in config.DEFAULTS and v is not None}
    cfg = dict(watcher.config, **overrides)

    # systemd stops the unit with SIGTERM, which kills the process outright --
    # the cleanup below would never run, leaving the bar frozen at its last
    # mask and the base still handing its LEDs to external telemetry. Turning
    # the signal into SystemExit makes it pass through the finally block.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    wheel = NullWheel() if args.dry_run else Wheel(cfg["profile"])

    def apply_appearance():
        wheel.set_appearance(palette.ramp(cfg["colors"], cfg["leds"]),
                             cfg["brightness"])

    apply_appearance()

    def send(mask):
        """Returns the mask to remember: None when the frame did not go out,
        so the next pass resends it once the wheel is back."""
        return mask if wheel.send(mask) else None

    shared = None
    last_mask = None
    last_sent = 0.0
    blink_phase = False
    blinking = False
    blink_at = 0.0
    scale = gearscale.GearScale()
    last_publish = 0.0
    last_cfg_poll = 0.0
    last_scan = 0.0
    idle_since = None

    def release_if_idle(now):
        """Hand the wheel back after a while with no game.

        Called only on the paths where there is no telemetry at all -- no
        mapping, or the owner died -- not while the game sits in its menus,
        where the bar is simply kept dark but the port stays ours so the lights
        come back the instant the car appears.
        """
        nonlocal idle_since
        if idle_since is None:
            idle_since = now
        elif now - idle_since >= IDLE_RELEASE and wheel.connected:
            print(_("\nNo game for {seconds:.0f} s -- handing the wheel back.")
                  .format(seconds=IDLE_RELEASE))
            wheel.close()
            # Restart the window: if close() did not actually free the port (or
            # the wheel reappears before the game does), this must not turn into
            # a message every two seconds.
            idle_since = now

    print(_("Running. Ctrl+C to stop."))
    print(_("Configuration: {path}").format(path=config.config_path()))
    if args.dry_run:
        print(_("Dry run: the serial port is left alone."))
    try:
        while True:
            # Pick up GUI edits without a restart, but do not stat the file at
            # the full telemetry rate.
            now = time.monotonic()
            if now - last_cfg_poll > 0.5:
                last_cfg_poll = now
                if watcher.poll():
                    cfg = dict(watcher.config, **overrides)
                    wheel.set_profile(cfg["profile"])
                    apply_appearance()
                    last_mask = None  # force a resend under the new curve
                    print(_("\nCurve reloaded: start={start:.2f} end={end:.2f} "
                            "blink={blink:.2f} reference={reference}").format(
                                start=cfg["start"], end=cfg["end"],
                                blink=cfg["blink"], reference=cfg["reference"]))

            interval = 1.0 / cfg["rate"]

            # Switched off in the config, or the config app has taken the wheel
            # for a test. The pause is a short lease, so a crashed GUI cannot
            # leave us parked for good.
            if not cfg["enabled"] or config.paused():
                if last_mask:
                    send(0)
                # Forget what we last sent: the config app writes its own masks
                # to the same port while we are parked, so on resuming we must
                # repaint unconditionally instead of assuming the bar is dark.
                last_mask = None
                time.sleep(0.2)
                continue

            if shared is None or (now - last_scan > RESCAN_EVERY):
                last_scan = now
                found = r3e_mem.find()
                if found is None:
                    if shared is not None:
                        print(_("\nRaceRoom has quit -- waiting for the next start."))
                        shared.close()
                        shared = None
                        scale.reset()
                    if last_mask:
                        last_mask = send(0)
                    release_if_idle(now)
                    time.sleep(2.0)
                    continue
                if shared is None or (found.pid, found.path) != (shared.pid, shared.path):
                    if shared is not None:
                        shared.close()
                        scale.reset()
                    shared = found
                    print(_("Telemetry found: {path} (API {version}, "
                            "owner pid {pid})").format(
                                path=shared.path, version=shared.version,
                                pid=shared.pid))
                    if not shared.layout_known:
                        print(_("Warning: this build reports an unexpected "
                                "layout (all_drivers_offset != {expected}). The "
                                "revs block may have moved; offsets are for API "
                                "3.6.").format(expected=r3e_mem.ALL_DRIVERS_OFFSET))
                else:
                    found.close()

            if not shared.alive():
                print(_("\nRaceRoom has quit -- waiting for the next start."))
                shared.close()
                shared = None
                scale.reset()
                last_mask = send(0)
                release_if_idle(now)
                continue

            try:
                sample = shared.read()
            except OSError:  # game closed
                shared.close()
                shared = None
                continue

            idle_since = None

            if sample is None or not sample.usable:
                # Menus: RaceRoom fills the block with -1.0f. Dark bar, but
                # keep the wheel claimed so the lights come back instantly.
                mask = 0
                blinking = False
            else:
                base = reference_rpm(sample, cfg)
                leds = cfg["leds"]
                # In the tall gears the car never reaches the limiter, so the
                # curve is scaled to what this gear actually revs to. Until a
                # gear has shown its range the reference is used, which is the
                # unadapted behaviour.
                if cfg["adaptive"]:
                    max_gears = (sample.num_gears
                                 if 2 <= sample.num_gears <= 10 else None)
                    reference = scale.observe(sample.rpm, base, sample.gear,
                                              now, max_gears)
                    if now - last_publish > 2.0:
                        last_publish = now
                        gearscale.publish(scale.table(base), base)
                else:
                    reference = base
                frac = sample.rpm / reference if reference else 0.0
                mask = mask_for(leds_lit(frac, cfg["start"], cfg["end"], leds),
                                leds, cfg["mode"])

                # Hysteresis on the blink threshold. Sitting on the limiter in
                # the top gear the revs bounce across it many times a second,
                # and without this the bar alternates between flashing and
                # showing the plain bar -- which reads as a broken display.
                if frac >= cfg["blink"]:
                    blinking = True
                elif frac < cfg["blink"] - BLINK_HYSTERESIS:
                    blinking = False

                if blinking:
                    if now - blink_at > 0.5 / cfg["blink_hz"]:
                        blink_phase = not blink_phase
                        blink_at = now
                    mask = (1 << leds) - 1 if blink_phase else 0
                if args.verbose:
                    print(f"\rrpm={sample.rpm:6.0f}/{base:6.0f} "
                          f"ref={reference:6.0f} gear={sample.gear:2d}"
                          f"/{sample.num_gears:2d} {sample.speed_kph:5.0f} km/h "
                          f"mode={sample.game_mode:2d} leds={mask:010b}",
                          end="", flush=True)

            # The frame is repeated on a timer as well as on a change: a mask
            # that holds still -- full bar on a straight -- would otherwise
            # leave the rim without data long enough to fall back to its own
            # idle animation. See HEARTBEAT.
            if mask != last_mask or now - last_sent > HEARTBEAT:
                sent = send(mask)
                last_mask = sent
                if sent is not None:
                    last_sent = now
            time.sleep(interval)
    except (KeyboardInterrupt, SystemExit):
        print(_("\nStopping ..."))
    finally:
        wheel.close()
        if shared:
            shared.close()


if __name__ == "__main__":
    main()
