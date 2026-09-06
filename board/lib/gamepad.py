# USB HID gamepad for MicroPython's machine.USBDevice (runtime USB).
#
# The device identifies with the Stadia controller's vendor/product IDs so that
# Chrome (a) applies its "standard" gamepad mapping and (b) enables
# gamepad.vibrationActuator via its HID haptics path, which writes a 5-byte
# output report: [0x05, strong_lo, strong_hi, weak_lo, weak_hi].
#
# Input report (ID 3), 15 bytes after the ID:
#   3 bytes : 18 buttons (bit i = button usage i+1), 6 bits padding
#   6 x u16 : X, Y, Z, Rz, Rx, Ry  (0..65535, centre 32768)
#
# Chrome maps axes by HID usage: X->axes[0], Y->axes[1], Z->axes[2],
# Rx->axes[3], Ry->axes[4], Rz->axes[5]. With the Stadia "standard" mapping
# the page sees: axes[0..3] = LX, LY, RX(Z), RY(Rz); Rx/Ry become the trigger
# buttons 6 and 7 (value = (axis + 1) / 2); buttons are reordered as documented
# in the BTN_* constants below (raw HID button index -> mapped index).

from micropython import const
import struct
import machine
import time
from usb.device.hid import HIDInterface

VID = const(0x18D1)
PID = const(0x9400)

_REPORT_ID_IN = const(3)
_REPORT_ID_RUMBLE = const(5)

_INTERFACE_CLASS = const(0x03)
_EP_IN_FLAG = const(1 << 7)

# Raw HID button bit positions (usage - 1) and what Chrome's Stadia
# mapping turns them into on the "standard" gamepad layout.
BTN_A = const(0)        # -> buttons[0]
BTN_B = const(1)        # -> buttons[1]
BTN_X = const(3)        # -> buttons[2]
BTN_Y = const(4)        # -> buttons[3]
BTN_L1 = const(6)       # -> buttons[4]
BTN_R1 = const(7)       # -> buttons[5]
BTN_SELECT = const(10)  # -> buttons[8]
BTN_START = const(11)   # -> buttons[9]
BTN_META = const(12)    # -> buttons[16]
BTN_L3 = const(13)      # -> buttons[10]
BTN_R3 = const(14)      # -> buttons[11]
BTN_EXTRA1 = const(16)  # -> buttons[17]
BTN_EXTRA2 = const(17)  # -> buttons[18]

AXIS_CENTER = const(32768)
AXIS_MAX = const(65535)

# fmt: off
_REPORT_DESC = bytes([
    0x05, 0x01,        # Usage Page (Generic Desktop)
    0x09, 0x05,        # Usage (Game Pad)
    0xA1, 0x01,        # Collection (Application)
    0x85, _REPORT_ID_IN,  # Report ID (3)
    # 18 buttons
    0x05, 0x09,        #   Usage Page (Button)
    0x19, 0x01,        #   Usage Minimum (1)
    0x29, 0x12,        #   Usage Maximum (18)
    0x15, 0x00,        #   Logical Minimum (0)
    0x25, 0x01,        #   Logical Maximum (1)
    0x75, 0x01,        #   Report Size (1)
    0x95, 0x12,        #   Report Count (18)
    0x81, 0x02,        #   Input (Data, Var, Abs)
    0x75, 0x01,        #   Report Size (1)
    0x95, 0x06,        #   Report Count (6)
    0x81, 0x03,        #   Input (Const) - padding
    # 6 x 16-bit axes
    0x05, 0x01,        #   Usage Page (Generic Desktop)
    0x09, 0x30,        #   Usage (X)
    0x09, 0x31,        #   Usage (Y)
    0x09, 0x32,        #   Usage (Z)
    0x09, 0x35,        #   Usage (Rz)
    0x09, 0x33,        #   Usage (Rx)
    0x09, 0x34,        #   Usage (Ry)
    0x15, 0x00,        #   Logical Minimum (0)
    0x27, 0xFF, 0xFF, 0x00, 0x00,  # Logical Maximum (65535)
    0x75, 0x10,        #   Report Size (16)
    0x95, 0x06,        #   Report Count (6)
    0x81, 0x02,        #   Input (Data, Var, Abs)
    # Rumble output report: two 16-bit magnitudes (strong, weak)
    0x85, _REPORT_ID_RUMBLE,  # Report ID (5)
    0x06, 0x00, 0xFF,  #   Usage Page (Vendor 0xFF00)
    0x09, 0x01,        #   Usage (1)
    0x15, 0x00,        #   Logical Minimum (0)
    0x27, 0xFF, 0xFF, 0x00, 0x00,  # Logical Maximum (65535)
    0x75, 0x10,        #   Report Size (16)
    0x95, 0x02,        #   Report Count (2)
    0x91, 0x02,        #   Output (Data, Var, Abs)
    0xC0,              # End Collection
])
# fmt: on


class Gamepad(HIDInterface):
    def __init__(self, on_rumble=None):
        super().__init__(
            _REPORT_DESC,
            set_report_buf=bytearray(8),
            interface_str="OpenMV Gamepad",
        )
        self.on_rumble = on_rumble
        self.strong = 0
        self.weak = 0
        self.rumble_count = 0
        # Two report buffers so we never modify one that is still in flight.
        self._reports = (bytearray(16), bytearray(16))
        self._reports[0][0] = _REPORT_ID_IN
        self._reports[1][0] = _REPORT_ID_IN
        self._cur = 0

    def desc_cfg(self, desc, itf_num, ep_num, strs):
        # Same as HIDInterface.desc_cfg, but with a 64-byte interrupt endpoint
        # polled every 1 ms (bInterval 4 = 2^(4-1) microframes at high speed).
        desc.interface(itf_num, 1, _INTERFACE_CLASS, 0, 0, len(strs) if self.interface_str else 0)
        if self.interface_str:
            strs.append(self.interface_str)
        self.get_hid_descriptor(desc)
        self._int_ep = ep_num | _EP_IN_FLAG
        desc.endpoint(self._int_ep, "interrupt", 64, 4)
        self.idle_rate = 0
        self.protocol = 1

    def on_set_report(self, report_data, report_id, report_type):
        # Chrome on macOS writes the whole report including the ID byte
        # (IOHIDDeviceSetReport). Other hosts may strip it. Handle both.
        b = bytes(report_data)
        if len(b) >= 5 and b[0] == _REPORT_ID_RUMBLE:
            b = b[1:]
        if len(b) < 4:
            return
        self.strong, self.weak = struct.unpack_from("<HH", b, 0)
        self.rumble_count += 1
        if self.on_rumble:
            self.on_rumble(self.strong, self.weak)

    def send(self, buttons=0, x=AXIS_CENTER, y=AXIS_CENTER, z=AXIS_CENTER, rz=AXIS_CENTER, rx=0, ry=0, timeout_ms=100):
        # Queue one input report. Blocks until the previous one has been sent.
        # buttons: 18-bit mask (bit BTN_*), axes: 0..65535.
        deadline = time.ticks_add(time.ticks_ms(), timeout_ms)
        while self.busy():
            if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
                return False
            machine.idle()
        if not self.is_open():
            return False
        self._cur ^= 1
        r = self._reports[self._cur]
        struct.pack_into("<HB6H", r, 1, buttons & 0xFFFF, (buttons >> 16) & 0x03, x, y, z, rz, rx, ry)
        self.submit_xfer(self._int_ep, r)
        return True


def axis(v):
    # Map -1.0..1.0 to 0..65535
    if v < -1.0:
        v = -1.0
    elif v > 1.0:
        v = 1.0
    return int((v + 1.0) * 32767.5)


def start(on_rumble=None, manufacturer="OpenMV", product="OpenMV AE3 Gamepad"):
    # Reconfigure the USB port as CDC serial (kept, so mpremote still works)
    # plus this HID gamepad. The board re-enumerates, so any open serial
    # connection drops for a second or two.
    import usb.device

    gp = Gamepad(on_rumble)
    # Force a host-visible disconnect before switching descriptors. The
    # runtime USB code disconnects and reconnects back-to-back, which the host
    # can miss (seen: "connection lost" and then no re-enumeration, board
    # stuck). Clear RUN_STOP (bit 31 of DWC3 DCTL, USB_BASE 0x48200000 +
    # 0xC704), give the host time to notice, then let init() reconnect.
    machine.mem32[0x4820C704] = machine.mem32[0x4820C704] & 0x7FFFFFFF
    time.sleep_ms(800)
    # The firmware is built with USB mass storage disabled, so the builtin
    # driver is CDC only and our HID interrupt endpoint lands on 0x83. The Alif
    # USB driver only supports IN endpoints 0x81-0x83; with MSC enabled the HID
    # endpoint became 0x84 and the host saw nothing but transaction errors.
    usb.device.get().init(
        gp,
        builtin_driver=True,
        manufacturer_str=manufacturer,
        product_str=product,
        id_vendor=VID,
        id_product=PID,
        bcd_device=0x0100,
    )
    return gp
