#!/bin/bash
# Flash a built OpenMV AE3 firmware over the board's USB DFU bootloader.
#
# Usage: tools/flash.sh [build-dir]   (default: firmware/build)
#
# Steps: ask the running firmware to jump to the bootloader (DFU device
# 37c5:96e3), write the HP + HE core images, both ROMFS images and the
# padded table of contents, then detach so the board reboots into the new
# firmware. If the board is already in DFU (LED blinking) the first step is
# skipped. The user filesystem (RWFS) and the bootloader are not touched.
set -euo pipefail
cd "$(dirname "$0")/.."
BUILD="${1:-firmware/build}"
DFU="37c5:96e3"
PORT="${PORT:-$(ls /dev/cu.usbmodem* 2>/dev/null | head -1 || true)}"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH"

for f in firmware_M55_HP.bin firmware_M55_HE.bin romfs0.img romfs1.img firmware_pad.toc; do
  [ -f "$BUILD/$f" ] || { echo "missing $BUILD/$f"; exit 1; }
done

flash() { echo "-> $2 -> $1"; dfu-util -d "$DFU" -a "$1" -D "$BUILD/$2"; }

if ! dfu-util -l 2>/dev/null | grep -q "$DFU"; then
  [ -n "$PORT" ] || { echo "no serial port and no DFU device found"; exit 1; }
  # The bootloader only stays in DFU for ~1.5 s after enumeration unless a
  # download starts (the "forced" magic is not honoured on this board), so
  # start dfu-util in wait mode *before* rebooting into the bootloader.
  echo "-> waiting for DFU device, then writing HP image"
  dfu-util -w -d "$DFU" -a HP -D "$BUILD/firmware_M55_HP.bin" > /tmp/dfu_hp.log 2>&1 &
  DFU_PID=$!
  sleep 0.5
  echo "-> entering bootloader via $PORT"
  # machine.bootloader() only resets the CPU core; the USB link stays up, so
  # the host never re-enumerates and never sees the DFU device. Force a USB
  # soft-disconnect first (clear RUN_STOP, bit 31 of DWC3 DCTL at
  # USB_BASE 0x48200000 + 0xC704), give the host time to notice, then jump.
  mpremote connect "$PORT" resume exec "
import machine, time
machine.mem32[0x4820C704] = machine.mem32[0x4820C704] & 0x7FFFFFFF
time.sleep_ms(800)
machine.bootloader()
" >/dev/null 2>&1 || true
  if ! wait $DFU_PID; then
    echo "HP download failed:"; tail -5 /tmp/dfu_hp.log; exit 1
  fi
  grep -q "Done\|File downloaded successfully" /tmp/dfu_hp.log && echo "-> HP written" || { tail -5 /tmp/dfu_hp.log; exit 1; }
else
  flash HP firmware_M55_HP.bin
fi

# OpenMV IDE's own manifest (Resources/firmware/OPENMV_AE3/firmware.lst)
# flashes just the two core images. ROMFS is included here because the board
# moves from 4.8.1 to a 5.0.x build; set ROMFS=0 to skip, TOC=1 to also
# rewrite the table of contents (same addresses, normally unnecessary).
flash HE     firmware_M55_HE.bin
if [ "${ROMFS:-1}" = 1 ]; then
  flash ROMFS1 romfs1.img
  flash ROMFS0 romfs0.img
fi
[ "${TOC:-0}" = 1 ] && flash TOC firmware_pad.toc
echo "-> detaching (board reboots)"
dfu-util -d "$DFU" -a HP -e || true
sleep 3
ls /dev/cu.usbmodem* 2>/dev/null && echo "done" || echo "board not back yet; give it a few seconds or replug"
