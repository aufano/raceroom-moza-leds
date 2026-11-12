# raceroom-moza-leds

Drive a Moza wheel's RPM LEDs from **RaceRoom Racing Experience** telemetry, on
Linux, natively. No SimHub, no .NET, no Windows plugins.

Ported from [`fpauker/lmu-rpm-leds`](https://github.com/fpauker/lmu-rpm-leds)
(GPL-3.0-or-later), which does the same for Le Mans Ultimate. The Moza serial
protocol (`moza.py`), the settings handling (`config.py`), the per-gear scaling
(`gearscale.py`), the palette and the i18n helper are his work, copied verbatim
except for the app name. What is new here is how the telemetry is found and read
(`r3e_mem.py`) and the main loop (`raceroom_moza_leds.py`).

## Why this exists

No existing native project supports RaceRoom: `lmu-rpm-leds`,
`francisdb/moza-rev` and `jardiacaj/gaugeout2serial` all read OutGauge or
Forza/ACC UDP, and RaceRoom emits **neither** — its only telemetry interface is
the shared memory block `$R3E`. SimHub does support it (`RREReader.dll` reads
`$R3E`, `MozaPlugin.dll` drives the wheel), but under Wine that means installing
SimHub and a real .NET 4.8 into the game's own prefix (appid 211500), roughly
1.3 GB, and hoping the reader survives Proton.

Reading `$R3E` from Linux directly is the short path.

## How the telemetry is obtained

RaceRoom publishes a Windows named file mapping called `$R3E`
(`#define R3E_SHARED_MEMORY_NAME "$R3E"` in the game's own
[r3e-api](https://github.com/kwstudios-sweden/r3e-api)). Under Proton, Wine backs
named file mappings with **memfd** objects: they have no path in the filesystem,
but they are readable through `/proc/<pid>/fd/<n>` as the owning user.

### Layout (API 3.6, `#pragma pack(1)`)

`sizeof(r3e_shared) = 44024`, `sizeof(r3e_driver_data) = 328`.

| Field | Offset | Type | Notes |
|---|---|---|---|
| `version_major` | 0 | int32 | 3 |
| `version_minor` | 4 | int32 | 6 |
| `all_drivers_offset` | 8 | int32 | 2008 == `offsetof(num_cars)` |
| `driver_data_size` | 12 | int32 | 328 |
| `game_mode` | 16 | int32 | -1 = `UNAVAILABLE` (menus) |
| `car_speed` | 1392 | float32 | m/s → ×3.6 = km/h |
| `engine_rps` | 1396 | float32 | rad/s → ×9.5493 = rpm |
| `max_engine_rps` | 1400 | float32 | |
| `upshift_rps` | 1404 | float32 | shift-light threshold, ~99 % of max |
| `gear` | 1408 | int32 | -2 N/A, -1 R, 0 N, 1..n |
| `num_gears` | 1412 | int32 | -1 when unknown |

Verified live on a 3.6 build (2026-11-11): in the menus every field is `-1.0f`
and `gear = -2`; on track `rpm 6029/7000`, `upshift 6930`, `gear 1/6`,
`88 km/h`. The engine does overshoot `max_engine_rps` on the limiter (7260 rpm
was observed), so the bar maths clamps.

### Three traps, all of them cost time

1. **The mapping is 45056 bytes, not 44024.** Wine rounds a memfd up to whole
   pages (11 × 4096). A reader that demands the exact struct size finds nothing.
2. **The game process is not called `RRRE64.exe`.** Wine renames the main thread
   to `MainThread` once the game is up, so `/proc/<pid>/comm` stops matching a
   few seconds after launch. The exe name survives in `/proc/<pid>/cmdline`.
3. **The mapping is also open in `wineserver` — and in this daemon itself.** Every
   process that has touched the object keeps an fd, and both `wineserver` (kept
   alive by CrewChief) and the daemon outlive the game. Latching onto either
   means reading a frozen last frame forever, believing the game is still
   running, and never handing the wheel back. Observed live on 2026-11-11: after
   the game quit, the daemon found **its own** fd and alternated between
   `/proc/<self>/fd/3` and `fd/5` every ten seconds. Detection therefore excludes
   our own pid and accepts only a mapping owned by the game's own process.

Detection is therefore by **header signature**, never by process name:
`version_major == 3`, `driver_data_size == 328`, `all_drivers_offset == 2008`,
`size >= 44024`. `game_mode` is deliberately *not* part of the test: it is -1 in
the menus, which is exactly when the mapping first appears.

### The adaptive curve has a ceiling

`gearscale.py` learns the highest RPM seen in each gear and uses it as the top of
the band, so that gears the car cannot rev out (a long top gear) still fill the
bar. RaceRoom breaks that assumption: `upshift_rps` is only the shift light, and
the engine keeps going past it — the limiter bounce sits a few percent above
`max_engine_rps`. Measured peaks were 6057–6466 against a 5983 base, so the
learned "top of the band" ended up *above* the shift point: the bar went dark
until 90 % of the limiter and never filled or blinked. A learned peak above the
base is therefore clamped back to the base; only lower peaks scale the bar.

### The rim sleeps if you only send on change

The obvious optimisation — write to the port only when the mask changes — is
wrong. The rim's firmware plays its own idle animation when it stops receiving
frames; [moza-rev](https://github.com/francisdb/moza-rev) puts the timeout at
about two seconds, and Moza's own SimHub plugin carries a dedicated LED keepalive
for the same reason.

It showed up exactly where the mask holds still: a long straight in a tall gear,
where the adaptive curve keeps the bar full and steady. With `blink` above
anything the scaled curve can reach there was no flashing either, so the daemon
sent nothing at all for tens of seconds and the rim took the LEDs back mid-lap —
the blue idle animation, with no error in the journal and no reconnect. The
current mask is therefore resent every 0.5 s (`HEARTBEAT`), which is about 27
bytes at 115200 baud and far inside the firmware's window.

### Do not put the exe name in your own command line

The racing-session scripts (`racing-xfce-watch`, `racing-xfce-return`) decide
whether a race is running with `pgrep -f 'Le Mans Ultimate\.exe|RRRE64?\.exe'`.
Any process carrying that string in its argv matches — including a background
watcher whose loop body contains `pgrep -f 'RRRE64\.exe'`, or even a log message
mentioning the exe. That produced a real false positive: `racing-xfce-return`
refused to go back to the desktop with *"Hay una carrera en marcha"* seven times
in a row. Hence: no `pgrep -f` here, and no exe name in this daemon's argv.

## Install

User-level, no root:

```sh
./install.sh                      # ~/.local/share + ~/.local/bin + user unit
systemctl --user enable --now raceroom-moza-leds.service
```

Self-contained in `~/.local/share/raceroom-moza-leds/`,
`~/.local/bin/raceroom-moza-leds-daemon` and
`~/.config/systemd/user/raceroom-moza-leds.service`.

Or system-wide, with the `PKGBUILD` in this repository (Arch, `makepkg -si`).
The launcher resolves its data directory from its own path, so the same file
serves both.

The service is safe to leave enabled: it opens the wheelbase lazily, retries
every 3 s, rescans for the game every 10 s, and keeps the bar dark in the menus.

**The serial port is exclusive.** `raceroom-moza-leds` and `lmu-rpm-leds` both
write to the same Moza port and must not run at the same time. boxflat also
writes to it and may overwrite the colour table; the daemon re-asserts mode,
colours and brightness every 2 s to win that fight.

`raceroom-session` stops `lmu-rpm-leds` and starts this one as soon as it sees
the game come up, which is where that conflict is settled in practice.

### Handing the wheel back

The port is claimed lazily — it is not opened until there is telemetry to
display — and it is released again after **60 s with no game** (`IDLE_RELEASE`):
the bar goes dark, the LEDs are handed back to the wheelbase (indicator mode 0)
and the fd is closed, so boxflat can configure the rim in between sessions. The
next start reclaims it. A quick restart or a session change stays inside the
60 s window, so it does not cost a reconnect.

While the game is *running* but sitting in its menus the port stays claimed: the
bar is simply dark, so the lights come back the instant the car appears.

## Test without touching the wheel

```sh
raceroom-moza-leds-daemon --dry-run --verbose   # reads telemetry, never opens the port
```

With RaceRoom in the menus you will see the source being found and a dark bar;
on track the mask follows the revs.

To inspect the shared memory on its own — header, live revs, and whether the
offset the reader uses still holds:

```sh
python3 tools/find_r3e.py           # one shot
python3 tools/find_r3e.py --watch   # 20 s of live samples
python3 tools/find_r3e.py --wait    # wait for the game and for a running engine
```

## Settings

`~/.config/raceroom-moza-leds/config.json`, reloaded on the fly (mtime polled at
2 Hz). Deleting it just falls back to the defaults in `config.py`.

| Key | Default | Meaning |
|---|---|---|
| `start` | 0.50 | fraction of the reference where the first LED lights |
| `end` | 0.98 | fraction where the whole bar is lit |
| `blink` | 0.99 | fraction where the bar flashes (with 1.5 % hysteresis) |
| `blink_hz` | 8.0 | flashing speed |
| `rate` | 50.0 | telemetry polls per second |
| `leds` | 10 | LEDs on the rim |
| `mode` | `bar` | `bar` fills left to right, `center` grows from both ends |
| `reference` | `max` | `max` = `max_engine_rps`, `upshift` = `upshift_rps` (bar tops out with the game's own shift light) |
| `adaptive` | true | scale the curve per gear to what that gear actually revs to |
| `colors` | 3 stops | low / middle / shift colour ramp |
| `brightness` | 100 | percent |
| `profile` | `auto` | wheel generation: `modern`, `legacy` or `auto` |
| `enabled` | true | feed the LEDs at all |

Command-line flags (`--start`, `--end`, `--blink`, `--rate`, `--profile`)
override the file for that run only.

## Files

| File | Origin |
|---|---|
| `r3e_mem.py` | new — locates and reads `$R3E` |
| `raceroom_moza_leds.py` | new, adapted from `lmu_rpm_leds.py` |
| `tools/find_r3e.py` | new — layout inspection and offset check |
| `moza.py`, `palette.py`, `i18n.py` | copied verbatim from `lmu-rpm-leds` |
| `config.py`, `gearscale.py` | copied, app name changed; `reference` key added, reference clamped to the base |
| `packaging/` | launcher script and the systemd user unit |
| `PKGBUILD` | system-wide Arch package |

License: GPL-3.0-or-later, as the upstream project.
