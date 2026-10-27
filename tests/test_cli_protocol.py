"""Command dispatch with no sockets or host shaping."""
import unittest
from unittest.mock import Mock

from bandctl.cli_protocol import dispatch
from bandctl.shaper import ShaperError


class ProtocolTests(unittest.TestCase):
    def test_limit_and_remove_dispatch(self):
        session = Mock()
        session.limit.return_value = {"pid": 42}
        response, stop = dispatch(session, {"command": "limit", "target": "firefox", "down": "30", "up": None})
        self.assertEqual(response, {"ok": True, "result": {"pid": 42}})
        self.assertFalse(stop)
        session.limit.assert_called_once_with("firefox", "30", None)
        session.remove.return_value = {"pid": 42}
        self.assertEqual(dispatch(session, {"command": "remove", "target": "42"})[0]["result"], {"pid": 42})

    def test_invalid_fields_and_types_do_not_call_session(self):
        session = Mock()
        requests = [None, {"command": "stop", "extra": True}, {"command": "limit", "target": 42, "down": "30", "up": None},
                    {"command": "limit", "target": "42", "down": True, "up": None},
                    {"command": "export", "path": "bad\x00file"}, {"command": "unknown"}]
        for request in requests:
            with self.subTest(request=request):
                response, stop = dispatch(session, request)
                self.assertFalse(response["ok"])
                self.assertFalse(stop)
        self.assertEqual(session.mock_calls, [])

    def test_errors_are_returned_and_stop_is_explicit(self):
        session = Mock()
        session.limit.side_effect = ShaperError("cannot apply")
        response, stop = dispatch(session, {"command": "limit", "target": "42", "down": "10", "up": None})
        self.assertEqual(response, {"ok": False, "error": "cannot apply"})
        self.assertFalse(stop)
        self.assertEqual(dispatch(session, {"command": "stop"}), ({"ok": True, "result": {}}, True))

    def test_settings_requests_use_explicit_paths(self):
        session = Mock()
        for command in ("export", "import"):
            response, stop = dispatch(session, {"command": command, "path": "/tmp/settings.json"})
            self.assertTrue(response["ok"])
            self.assertFalse(stop)
        session.export.assert_called_once_with("/tmp/settings.json")
        session.import_file.assert_called_once_with("/tmp/settings.json")
