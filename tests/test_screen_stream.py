"""Tests for the screen-stream frame pipeline (no emulator required).

These lock in the behaviour that fixes the dead/stalled preview:

* the latest-frame slot never queues (a newer frame replaces an unread one),
* a real scrcpy frame stops the screencap fallback,
* fallback frames do not stop themselves,
* the stall watchdog only aborts when frames really stopped.
"""

import struct
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from PyQt6.QtGui import QImage  # noqa: E402

from core.screen_stream import (  # noqa: E402
    ScreenStream, STALL_TIMEOUT, _raw_screencap_to_qimage)


class _FakeADB:
    serial = "emulator-5554"


def _image(width=54, height=96, color=0x11223344):
    image = QImage(width, height, QImage.Format.Format_RGB888)
    image.fill(color)
    return image


class TestFrameSlot(unittest.TestCase):
    def setUp(self):
        self.stream = ScreenStream(_FakeADB())

    def test_slot_holds_only_the_newest_frame(self):
        self.stream._publish_frame(_image(color=0x111111))
        self.stream._publish_frame(_image(color=0x222222))
        image, seq = self.stream.get_latest_frame()
        self.assertIsNotNone(image)
        self.assertEqual(seq, 2)
        self.assertEqual(self.stream._stats["dropped"], 1)
        # The slot is cleared on read: the same frame is never painted twice.
        image2, seq2 = self.stream.get_latest_frame()
        self.assertIsNone(image2)
        self.assertEqual(seq2, 2)

    def test_real_frame_stops_the_screencap_fallback(self):
        self.assertFalse(self.stream._fallback_stop.is_set())
        self.stream._publish_frame(_image())
        self.assertTrue(self.stream._fallback_stop.is_set())

    def test_fallback_frame_does_not_stop_the_fallback(self):
        self.stream._publish_frame(_image(), from_fallback=True)
        self.assertFalse(self.stream._fallback_stop.is_set())

    def test_publish_marks_the_stream_alive(self):
        before = self.stream._last_frame_at
        time.sleep(0.01)
        self.stream._publish_frame(_image())
        self.assertGreater(self.stream._last_frame_at, before)


class TestStallWatchdog(unittest.TestCase):
    def setUp(self):
        self.stream = ScreenStream(_FakeADB())

    def test_stalled_stream_is_aborted(self):
        # Simulate "connected but no frame for longer than the timeout".
        self.stream._last_frame_at = time.monotonic() - STALL_TIMEOUT - 1
        self.stream._watchdog_loop()
        self.assertTrue(self.stream._stalled)

    def test_healthy_stream_is_not_aborted(self):
        self.stream._last_frame_at = time.monotonic()
        # The loop waits before checking; stop it right away.
        self.stream._watchdog_stop.set()
        self.stream._watchdog_loop()
        self.assertFalse(self.stream._stalled)


class TestRawScreencap(unittest.TestCase):
    def test_rgba_buffer_is_decoded(self):
        width, height = 4, 3
        pixels = bytes([10, 20, 30, 255]) * (width * height)
        data = struct.pack("<III", width, height, 1) + pixels
        image = _raw_screencap_to_qimage(data)
        self.assertIsNotNone(image)
        self.assertEqual((image.width(), image.height()), (width, height))
        self.assertEqual(image.pixelColor(1, 1).red(), 10)
        self.assertEqual(image.pixelColor(1, 1).green(), 20)
        self.assertEqual(image.pixelColor(1, 1).blue(), 30)

    def test_truncated_buffer_is_rejected(self):
        data = struct.pack("<III", 10, 10, 1) + b"\x00" * 8
        self.assertIsNone(_raw_screencap_to_qimage(data))
        self.assertIsNone(_raw_screencap_to_qimage(b""))


if __name__ == "__main__":
    unittest.main()
