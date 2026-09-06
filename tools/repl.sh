#!/bin/bash
# Open a REPL on the camera without soft-resetting it (Ctrl-] to exit).
export PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH"
PORT="${PORT:-$(ls /dev/cu.usbmodem* 2>/dev/null | head -1)}"
exec mpremote connect "$PORT" resume repl
