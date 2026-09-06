# OpenMV AE3 as a USB gamepad: camera frames streamed CRT-style.
#
# Runs as a plain main loop (OpenMV style). Camera capture must not happen
# inside a timer callback: it blocks the scheduler that also services USB,
# freezing everything. mpremote can still connect: Ctrl-C stops the loop
# and drops to the REPL; machine.reset() restarts it.
#
# Idle: the left stick draws a sine/cosine circle and button A "heartbeats"
# once a second (Chrome only exposes a gamepad after a button press).
#
# A rumble from the browser toggles scanning. While scanning, frames are
# captured back to back and streamed as HID reports, 4 pixels per report:
#   left stick X/Y       = beam position: column/row of the first pixel
#   right stick X/Y      = pixel 0, pixel 1 as raw RGB565 (16-bit axes)
#   triggers L2/R2       = pixel 2, pixel 3 as raw RGB565
#   L1 (buttons[4])      = report carries pixel data
#   R1 (buttons[5])      = last report of a frame
#   Y  (buttons[3])      = frame start; sticks carry W, H, interleave N, pass p
#   X  (buttons[2])      = frame parity
# The browser samples gamepads at ~60 Hz, so each report is held HOLD_MS.
# Anything it misses is just a hole until the next scan (lossy by design).
#
# Two modes, chosen by the rumble's weak channel (weak >= 1/4 = interleaved):
#   normal      32x20, every row, one pass per frame (~3 s per frame)
#   interleaved 80x50, 10 passes over ONE captured frame (~17 s per frame).
#               The frame is sampled into a buffer at pass 0 and every pass
#               sends from that buffer, so the whole image is a single moment
#               and the camera can move once the capture has happened.
#               Passes go 0, 5, 2, 7, 1, ... so the picture resolves evenly
#               rather than sharpening from the top; the page stretches each
#               row down over the rows still missing, CRT style.

import math
import time
import machine
import os
import sys

MODES = {
    # name: (W, interleave passes)
    "normal": (32, 1),
    "interleaved": (80, 10),
}
# Row offset for each pass, spread out so the image resolves evenly.
PASS_ORDER = {1: (0,), 10: (0, 5, 2, 7, 1, 6, 3, 8, 4, 9)}
W = 32
H = 20                # recomputed from the sensor aspect at first capture
N = 1                 # interleave passes
HOLD_MS = 17
FAIL_LIMIT = 300      # consecutive rejected reports -> SoC reset
OPEN_TIMEOUT_MS = 15000


def log(msg):
    # Boot log on /flash: readable over DFU when serial is not available.
    try:
        with open("/flash/boot.log", "a") as f:
            f.write("%d %s\n" % (time.ticks_ms(), msg))
    except Exception:
        pass
    print(msg)


log("main.py start, reset_cause=%s" % machine.reset_cause())

# Safety hatch: create /flash/nogamepad to boot to a plain REPL (serial only).
if "nogamepad" in os.listdir("/flash"):
    log("nogamepad marker present, staying in REPL")
    sys.exit()

import gamepad
from gamepad import axis, BTN_A, BTN_X, BTN_Y, BTN_L1, BTN_R1, AXIS_CENTER

stats = {"sent": 0, "fail": 0, "frames": 0, "passes": 0, "rumbles": 0, "errors": 0}
rumble_pending = False
rumble_mode = "normal"


def on_rumble(strong, weak):
    global rumble_pending, rumble_mode
    stats["rumbles"] += 1
    if strong or weak:
        rumble_pending = True
        rumble_mode = "interleaved" if weak >= 16384 else "normal"


gp = gamepad.start(on_rumble)
log("gamepad.start ok")

# ---------------------------------------------------------------- camera
csi0 = None
SX = 10
SY = 10


def camera():
    global csi0
    if csi0 is None:
        import csi

        csi0 = csi.CSI()
        csi0.reset()
        csi0.pixformat(csi.RGB565)
        # The AE3 sensor only accepts QVGA here (and delivers 320x200).
        csi0.framesize(csi.QVGA)
        csi0.snapshot(time=500)  # let auto exposure settle
    return csi0


def set_mode(mode):
    global W, N
    W, N = MODES[mode]


def capture():
    # Keep the full frame and sample it with a stride: image scaling raised
    # on this firmware, and the sensor aspect is not 4:3. H follows W.
    global H, SX, SY
    img = camera().snapshot()
    SX = img.width() // W
    H = max(2, min(255, round(W * img.height() / img.width())))
    SY = img.height() // H
    return img


try:
    camera()
    log("camera ok")
except Exception as e:
    stats["errors"] += 1
    log("camera init failed: %r" % e)


# ---------------------------------------------------------------- sending
fail_count = 0


def send(*args):
    global fail_count
    if gp.send(*args, timeout_ms=50):
        fail_count = 0
        stats["sent"] += 1
        return True
    fail_count += 1
    stats["fail"] += 1
    if fail_count >= FAIL_LIMIT:
        log("gamepad: host not responding, resetting")
        time.sleep_ms(100)
        machine.reset()
    return False


def grab(img):
    # Sample the sensor frame into a W x H RGB565 buffer on the heap. The
    # sensor keeps reusing its frame buffer and an interleaved frame takes
    # ~17 s to send, so the pixels have to be copied out up front for the
    # whole image to be one moment in time.
    buf = bytearray(W * H * 2)
    i = 0
    for y in range(H):
        sy = y * SY
        for x in range(W):
            # This firmware's get_pixel takes one (x, y) tuple.
            r, g, b = img.get_pixel((x * SX, sy))
            v = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
            buf[i] = v >> 8
            buf[i + 1] = v & 0xFF
            i += 2
    return buf


def rgb565(buf, x, y):
    i = (y * W + x) * 2
    return (buf[i] << 8) | buf[i + 1]


def send_frame(buf, parity, seq=0):
    # One pass of the frozen frame. Frame start carries W, H, N and the pass
    # sequence number (0 = first pass of a new frame).
    send((1 << BTN_Y) | (parity << BTN_X), W * 257, H * 257, N * 257, seq * 257, 0, 0)
    time.sleep_ms(HOLD_MS)
    rows = list(range(PASS_ORDER[N][seq], H, N))
    for ri, y in enumerate(rows):
        for x in range(0, W, 4):
            px = [rgb565(buf, min(x + k, W - 1), y) for k in range(4)]
            last = ri == len(rows) - 1 and x + 4 >= W
            buttons = (1 << BTN_L1) | (parity << BTN_X) | ((1 << BTN_R1) if last else 0)
            send(buttons, axis(x / (W - 1) * 2 - 1), axis(y / (H - 1) * 2 - 1), px[0], px[1], px[2], px[3])
            time.sleep_ms(HOLD_MS)
            if rumble_pending:
                return False  # mode change or stop requested mid-pass
    time.sleep_ms(HOLD_MS)  # hold the end report for one more sample
    return True


# ---------------------------------------------------------------- main loop
log("gamepad: waiting for host")
t_open = time.ticks_ms()
while not gp.is_open():
    if time.ticks_diff(time.ticks_ms(), t_open) > OPEN_TIMEOUT_MS:
        log("gamepad: not opened by host for %d ms, resetting" % OPEN_TIMEOUT_MS)
        time.sleep_ms(100)
        machine.reset()
    time.sleep_ms(50)
log("gamepad: open, streaming")

scanning = False
mode = "normal"
parity = 0
pass_i = 0
frame_buf = None
t0 = time.ticks_ms()
last_beat = -1


def loop():
    global rumble_pending, scanning, mode, parity, pass_i, frame_buf, last_beat
    if rumble_pending:
        rumble_pending = False
        if scanning and rumble_mode != mode:
            mode = rumble_mode      # switch mode, keep scanning
        else:
            scanning = not scanning
            mode = rumble_mode
        set_mode(mode)
        pass_i = 0
        frame_buf = None
        log("rumble -> scanning %s mode %s (strong=%d weak=%d)" % (scanning, mode, gp.strong, gp.weak))

    if scanning:
        if frame_buf is None:
            try:
                t = time.ticks_ms()
                frame_buf = grab(capture())
            except Exception as e:
                stats["errors"] += 1
                log("capture failed: %r" % e)
                scanning = False
                return
            parity ^= 1
            pass_i = 0
            log("captured %dx%d in %d ms" % (W, H, time.ticks_diff(time.ticks_ms(), t)))
        if send_frame(frame_buf, parity, pass_i):
            stats["passes"] += 1
            pass_i += 1
            if pass_i >= N:
                pass_i = 0
                frame_buf = None
                stats["frames"] += 1
        return

    # Idle animation + heartbeat.
    t = time.ticks_diff(time.ticks_ms(), t0) / 1000
    sec = int(t)
    beat = sec != last_beat and (t - sec) < 0.1
    if beat:
        last_beat = sec
    send((1 << BTN_A) if beat else 0, axis(math.sin(t * 2)), axis(math.cos(t * 2)), AXIS_CENTER, AXIS_CENTER, 0, 0)
    time.sleep_ms(10)


while True:
    try:
        loop()
    except KeyboardInterrupt:
        raise
    except Exception as e:
        stats["errors"] += 1
        log("main loop error: %r" % e)
        time.sleep_ms(500)
        machine.reset()
