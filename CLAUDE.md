Make an OpenMV AE3 camera show up as a USB gamepad, then push a captured image
into a web page through the browser Gamepad API. The browser triggers a capture
with a rumble command (the only host-to-gamepad channel the API has).

## Layout

| Path | What |
|---|---|
| `firmware/openmv-usb-runtime.patch` | The firmware change: enables `machine.USBDevice` (MicroPython runtime USB) in the OpenMV AE3 build. |
| `firmware/build/` | Built images from the patched tree (OpenMV master, post 5.0.1). Not committed: rebuild them (below) before flashing. |
| `board/lib/gamepad.py` | HID gamepad class built on `machine.USBDevice`. Uses the Stadia controller's vendor/product IDs so Chrome enables rumble and its "standard" mapping. |
| `board/lib/usb/` | `usb-device` + `usb-device-hid` from micropython-lib (vendored). |
| `board/main.py` | Runs on the camera: idle sine-wave sticks + heartbeat, and the rumble-triggered pixel stream. |
| `docs/index.html` | Gamepad visualiser, scan button, pixel-stream decoder. No build step; also what GitHub Pages serves. |
| `docs/sw.js` | Service worker: caches the page shell so it works offline. |
| `docs/manifest.webmanifest`, `docs/icon.svg` | Web app manifest and icon, so the page can be installed. |
| `tools/flash.sh` | Flash `firmware/build` over DFU. |
| `tools/deploy.sh` | Copy `board/` to the camera's `/flash` and reset it. |
| `tools/repl.sh` | REPL without a soft reset. |

The patched firmware source tree lives in `~/code/openmv-firmware` (OpenMV repo
with submodules, SDK 1.6.0 in `~/openmv-sdk-1.6.0`).

## Why a firmware change

Stock OpenMV 4.8.1 / 5.0.x for the AE3 exposes neither `pyb.USB_HID` nor
`machine.USBDevice`, so Python cannot add a HID interface. The patch turns on
`MICROPY_HW_ENABLE_USB_RUNTIME_DEVICE`, compiles `shared/tinyusb/mp_usbd_runtime.c`,
and calls `mp_usbd_deinit()` on soft reset. The built-in CDC serial stays, so
`mpremote` and the IDE keep working next to the HID interface.

Rebuild after editing the tree:

```bash
cd ~/code/openmv-firmware
export PATH="/opt/homebrew/opt/make/libexec/gnubin:/opt/homebrew/opt/coreutils/libexec/gnubin:/opt/homebrew/bin:$PATH"
make -j8 TARGET=OPENMV_AE3
cp build/OPENMV_AE3/bin/{firmware_M55_HP.bin,firmware_M55_HE.bin,romfs0.img,romfs1.img,firmware_pad.toc} ~/code/gamepad-camera/firmware/build/
```

## Flashing

Build the firmware first (above), then quit OpenMV IDE (it holds the serial
port) and run:

```bash
tools/flash.sh
```

This jumps to the DFU bootloader (`37c5:96e3`), writes the HP and HE core
images plus both ROMFS images (`ROMFS=0` skips them, `TOC=1` also rewrites the
table of contents), and detaches. The user filesystem and bootloader are
untouched. To go back to stock firmware, use
the IDE's `Tools -> Run Bootloader` with an official release.

## Board code

```bash
tools/deploy.sh      # copies board/ to /flash and resets
tools/repl.sh        # watch prints; Ctrl-] exits
```

Note: the scripts use `mpremote ... resume` (no soft reset on connect).
Connecting Ctrl-Cs the running main loop and drops to the REPL; `deploy.sh`
resets the board afterwards. When the gamepad code starts, the board
re-enumerates, so any open serial session drops for a couple of seconds.
The USB wedges seen during development were driver bugs, fixed in the patch
(see `firmware/openmv-usb-runtime.patch`).

## Protocol (CRT scan, 4 pixels per report)

Rumble from the browser toggles scanning; the rumble's weak channel selects
the mode (weak >= 0.25 = interleaved). While scanning the board captures
frames back to back and streams them as HID reports.

| Signal (standard mapping) | Meaning |
|---|---|
| buttons[3] (Y) | frame/pass start; left stick = W, H; right stick = interleave N, pass p (value/255) |
| buttons[4] (L1) | report carries pixel data |
| buttons[5] (R1) | last report of the pass |
| buttons[2] (X) | frame parity |
| buttons[0] (A) | idle heartbeat (Chrome only exposes a gamepad after a button press) |
| axes[0], axes[1] | beam position: column, row of the first pixel |
| axes[2], axes[3] | pixel 0, pixel 1 as raw RGB565 (16-bit) |
| buttons[6].value, buttons[7].value | pixel 2, pixel 3 as raw RGB565 (16-bit) |

Modes: normal is 32x20 in one pass (~3 s per frame); interleaved is 80x50 in
10 passes, pass p sending rows p, p+10, ... from a fresh capture, and the page
stretches each row down over rows still missing.

Chrome samples gamepads at ~60 Hz, so each report is held 17 ms (about 240
pixels per second). Missed samples are holes until the next scan: lossy by
design. Rumble output report from Chrome: `[0x05, strong_lo, strong_hi,
weak_lo, weak_hi]`.

## Web page

Published from `docs/` by GitHub Pages. To run it locally instead:

```bash
python3 -m http.server -d docs 8000
```

Open http://localhost:8000 in Chrome. Chrome only exposes a gamepad after a
button press, which the board's heartbeat provides.

## Offline

`docs/sw.js` caches the page shell (`./`, `index.html`, the manifest and the
icon) so everything after the first load runs with no network — handy when the
camera is the only thing plugged in. The page is self-contained, so that is the
whole app.

The strategy is stale-while-revalidate: the cached copy is served immediately
and refreshed in the background, so an edit to `index.html` shows up on the
*second* load after it ships. Editing `sw.js` installs a new worker, and the
page then offers an "update ready" chip with a reload button in the header
rather than swapping under a running scan; `VERSION` in `sw.js` names the cache and old
ones are deleted on activate.

Service workers need `https://` or `localhost`, so `file://` and plain-HTTP
hosts silently skip registration (the page still works, just not offline).
While developing, "Update on reload" in DevTools → Application avoids having to
think about any of this.
