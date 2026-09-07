import subprocess
import unittest
from unittest import mock

from router.ac86u import route_check


class RouteCheckTests(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def firewall(*args, **kwargs):
            self.calls.append(args)
            return subprocess.CompletedProcess(args, 1 if args[0] == "-C" else 0, "", "")

        self.raw = mock.MagicMock()
        self.tls = mock.MagicMock()
        self.tls.recv.side_effect = [b"HTTP/1.1 ", b"206 Partial Content\r\n"]
        context = mock.Mock()
        context.wrap_socket.return_value = self.tls
        for patcher in (
            mock.patch.object(route_check, "firewall", side_effect=firewall),
            mock.patch.object(route_check.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("203.0.113.8", 443))]),
            mock.patch.object(route_check.socket, "socket", return_value=self.raw),
            mock.patch.object(route_check.ssl, "create_default_context", return_value=context),
            mock.patch.object(route_check.signal, "alarm"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def assert_rollback(self):
        inserted = next(args for args in self.calls if args[0] == "-I")
        deleted = next(args for args in self.calls if args[0] == "-D")
        self.assertEqual(("-D", "OUTPUT", *inserted[3:]), deleted)
        self.assertIn("203.0.113.8/32", inserted)
        self.raw.close.assert_called_once()

    def test_success_removes_only_the_rule_it_added(self):
        route_check.check_connection()
        self.assert_rollback()
        self.raw.setsockopt.assert_called_once()
        self.tls.close.assert_called_once()
        self.assertIn(b"Range: bytes=0-127", self.tls.sendall.call_args.args[0])

    def test_failed_connection_rolls_back(self):
        self.raw.connect.side_effect = TimeoutError("connection timed out")
        with self.assertRaises(TimeoutError):
            route_check.check_connection()
        self.assert_rollback()

    def test_interrupted_connection_rolls_back(self):
        self.raw.connect.side_effect = InterruptedError("interrupted")
        with self.assertRaises(InterruptedError):
            route_check.check_connection()
        self.assert_rollback()

    def test_unsupported_socket_mark_never_changes_firewall(self):
        self.raw.setsockopt.side_effect = OSError("not supported")
        with self.assertRaises(OSError):
            route_check.check_connection()
        self.assertFalse(any(args[0] in {"-I", "-D"} for args in self.calls))
        self.raw.close.assert_called_once()

    def test_existing_rule_is_never_borrowed_or_removed(self):
        route_check.firewall.side_effect = lambda *args, **kw: subprocess.CompletedProcess(args, 0, "", "")
        with self.assertRaisesRegex(RuntimeError, "already exists"):
            route_check.check_connection()
        calls = [call.args[0] for call in route_check.firewall.call_args_list]
        self.assertNotIn("-I", calls)
        self.assertNotIn("-D", calls)

    def test_http_error_is_not_reported_as_success(self):
        self.tls.recv.side_effect = [b"HTTP/1.1 404 Not Found\r\n"]
        with self.assertRaisesRegex(RuntimeError, "Unexpected HTTP"):
            route_check.check_connection()
        self.assert_rollback()

    def test_cleanup_error_is_explicit_and_provides_exact_removal_rule(self):
        def firewall(*args, **kwargs):
            self.calls.append(args)
            if args[0] == "-D":
                raise subprocess.TimeoutExpired("iptables", 5)
            return subprocess.CompletedProcess(args, 1 if args[0] == "-C" else 0, "", "")
        route_check.firewall.side_effect = firewall
        with mock.patch.object(route_check.sys, "stderr") as stderr:
            with self.assertRaisesRegex(RuntimeError, "cleanup needs attention"):
                route_check.check_connection()
        written = ''.join(c.args[0] for c in stderr.write.call_args_list)
        self.assertIn("iptables -t nat -D OUTPUT", written)
        self.assertIn("203.0.113.8/32", written)
