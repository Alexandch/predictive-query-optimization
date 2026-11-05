from unittest import TestCase
from unittest.mock import patch

from pqo.desktop import _enable_desktop_fault_handler


class DesktopFaultHandlerTests(TestCase):
    def test_windows_keeps_current_trace_without_walking_live_worker_frames(self):
        stream = object()
        with patch("pqo.desktop.sys.platform", "win32"), patch("pqo.desktop.faulthandler.enable") as enable:
            _enable_desktop_fault_handler(stream)
        enable.assert_called_once_with(stream, all_threads=False)

    def test_other_platforms_keep_all_thread_diagnostics(self):
        stream = object()
        for platform in ("linux", "darwin"):
            with self.subTest(platform=platform), patch("pqo.desktop.sys.platform", platform), patch("pqo.desktop.faulthandler.enable") as enable:
                _enable_desktop_fault_handler(stream)
            enable.assert_called_once_with(stream, all_threads=True)
