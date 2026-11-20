"""Tests for the scrcpy control-socket input encoder (no device needed).

The preview used to shell out to ``adb shell input`` for every tap, swipe and
key.  These tests lock in the binary control protocol used instead, so a
broken encoder can never silently disable input.
"""

import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.scrcpy import ScrcpyController  # noqa: E402


class _FakeSocket:
    def __init__(self):
        self.data = bytearray()
        self.closed = False

    def sendall(self, payload):
        self.data += payload

    def close(self):
        self.closed = True


def _touch_messages(blob):
    """Split a byte stream into touch messages (32 bytes each)."""
    messages = []
    for offset in range(0, len(blob), 32):
        chunk = blob[offset:offset + 32]
        if len(chunk) < 32:
            break
        (mtype, action, pointer_id, x, y, sw, sh, pressure,
         action_button, buttons) = struct.unpack(">BBqiiHHHii", chunk)
        messages.append({
            "type": mtype, "action": action, "pointer_id": pointer_id,
            "x": x, "y": y, "screen_w": sw, "screen_h": sh,
            "pressure": pressure, "action_button": action_button,
            "buttons": buttons,
        })
    return messages


class TestScrcpyController(unittest.TestCase):
    def setUp(self):
        self.sock = _FakeSocket()
        self.ctrl = ScrcpyController(self.sock)

    def test_tap_sends_down_then_up(self):
        ok, _ = self.ctrl.tap(100.0, 200.0, 360, 640, lift_ms=0)
        self.assertTrue(ok)
        messages = _touch_messages(bytes(self.sock.data))
        self.assertEqual(len(messages), 2)
        self.assertEqual([m["action"] for m in messages], [0, 1])
        for message in messages:
            self.assertEqual(message["type"], 2)
            self.assertEqual(message["x"], 100)
            self.assertEqual(message["y"], 200)
            self.assertEqual(message["screen_w"], 360)
            self.assertEqual(message["screen_h"], 640)
            self.assertEqual(message["pressure"], 0xffff)
            self.assertEqual(message["pointer_id"], -2)

    def test_swipe_sends_down_moves_and_up(self):
        ok, _ = self.ctrl.swipe(50, 500, 50, 100, 360, 640, duration_ms=120)
        self.assertTrue(ok)
        messages = _touch_messages(bytes(self.sock.data))
        self.assertGreaterEqual(len(messages), 4)
        self.assertEqual(messages[0]["action"], 0)
        self.assertEqual(messages[-1]["action"], 1)
        moves = [m for m in messages if m["action"] == 2]
        self.assertGreaterEqual(len(moves), 2)
        # The interpolated moves travel from y=500 towards y=100.
        ys = [m["y"] for m in moves]
        self.assertGreater(ys[0], ys[-1])
        self.assertEqual(messages[-1]["x"], 50)
        self.assertEqual(messages[-1]["y"], 100)

    def test_coordinates_are_clamped_to_the_screen(self):
        ok, _ = self.ctrl.tap(-20, 9999, 360, 640, lift_ms=0)
        self.assertTrue(ok)
        message = _touch_messages(bytes(self.sock.data))[0]
        self.assertEqual(message["x"], 0)
        self.assertEqual(message["y"], 639)

    def test_scroll_message_layout(self):
        ok, _ = self.ctrl.scroll(180, 320, 0.0, 1.0, 360, 640)
        self.assertTrue(ok)
        blob = bytes(self.sock.data)
        self.assertEqual(len(blob), 21)
        mtype, x, y, sw, sh, hs, vs, buttons = struct.unpack(
            ">BiiHHhhi", blob)
        self.assertEqual(mtype, 3)
        self.assertEqual((x, y, sw, sh, buttons), (180, 320, 360, 640, 0))
        self.assertEqual(hs, 0)
        self.assertGreater(vs, 0)

    def test_key_sends_down_then_up(self):
        ok, _ = self.ctrl.key(4)
        self.assertTrue(ok)
        blob = bytes(self.sock.data)
        self.assertEqual(len(blob), 28)
        first = struct.unpack(">BBiii", blob[:14])
        second = struct.unpack(">BBiii", blob[14:])
        self.assertEqual(first, (0, 0, 4, 0, 0))
        self.assertEqual(second, (0, 1, 4, 0, 0))

    def test_video_size_scales_touch_coordinates(self):
        """The server drops touches tagged with the wrong video size."""
        self.ctrl.set_video_size(324, 720)
        ok, _ = self.ctrl.tap(180, 320, 360, 640, lift_ms=0)
        self.assertTrue(ok)
        message = _touch_messages(bytes(self.sock.data))[0]
        self.assertEqual(message["x"], 162)
        self.assertEqual(message["y"], 360)
        self.assertEqual(message["screen_w"], 324)
        self.assertEqual(message["screen_h"], 720)

    def test_closed_controller_reports_failure(self):
        self.ctrl.close()
        self.assertTrue(self.sock.closed)
        ok, _ = self.ctrl.tap(10, 10, 100, 100, lift_ms=0)
        self.assertFalse(ok)
        self.assertIsNone(self.ctrl._sock)


if __name__ == "__main__":
    unittest.main()
