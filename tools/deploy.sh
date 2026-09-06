#!/bin/bash
# Copy the board/ scripts to the camera's /flash and reset it.
#
# Uses a single `mpremote exec` session that writes every file from embedded
# base64 data. mpremote's `fs cp` (and back-to-back sessions in general) have
# wedged this board's USB stack; plain exec sessions have been reliable.
# `resume` avoids the soft reset on connect for the same reason.
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH"
PORT="${PORT:-$(ls /dev/cu.usbmodem* 2>/dev/null | head -1)}"
[ -n "$PORT" ] || { echo "no /dev/cu.usbmodem* port"; exit 1; }

FILES="board/lib/usb/device/__init__.py board/lib/usb/device/core.py board/lib/usb/device/hid.py board/lib/gamepad.py board/main.py"

CODE=$(python3 - $FILES <<'PY'
import base64, sys
out = ["import os, binascii",
       "def _mk(d):",
       "    try: os.mkdir(d)",
       "    except OSError: pass",
       "for d in ('/flash/lib', '/flash/lib/usb', '/flash/lib/usb/device'): _mk(d)"]
for f in sys.argv[1:]:
    dest = '/flash/' + f.split('board/', 1)[1]
    data = base64.b64encode(open(f, 'rb').read()).decode()
    out.append(f"_f = open({dest!r}, 'wb'); _f.write(binascii.a2b_base64({data!r})); _f.close(); print('wrote', {dest!r}, os.stat({dest!r})[6], 'bytes')")
print("\n".join(out))
PY
)
# Retry: raw-paste transfers occasionally fail while the board streams.
for attempt in 1 2 3; do
  mpremote connect "$PORT" resume exec "$CODE" && break
  echo "-> exec attempt $attempt failed, retrying"; sleep 2
done
echo "-> copied; resetting board (it will re-enumerate as a gamepad)"
mpremote connect "$PORT" resume exec "import machine; machine.reset()" >/dev/null 2>&1 || true
