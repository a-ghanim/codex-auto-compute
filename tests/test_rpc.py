"""RPC diagnostics against disposable subprocesses, without live Codex state."""
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from codex_auto_compute import install


OBSERVED = ("WARNING: could not create PATH aliases: Operation not permitted (os error 1)\n"
            "Error: failed to initialize sqlite state runtime under /private-user/.codex: "
            "failed to initialize state runtime at /private-user/.codex\n")


class RPCTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.binary = self.home / "fake-codex"

    def server(self, code):
        self.binary.write_text(f"#!{sys.executable}\nimport json, sys\n" + code)
        self.binary.chmod(0o700)

    def failure(self, stderr):
        self.server(f"sys.stdin.readline()\nsys.stderr.write({stderr!r})\nsys.stderr.flush()\n")
        with self.assertRaises(RuntimeError) as caught:
            install.RPC(str(self.binary), self.home)
        return str(caught.exception)

    def test_observed_permission_failure_is_specific_and_private(self):
        errfile = tempfile.TemporaryFile()
        with patch.object(install.tempfile, "TemporaryFile", return_value=errfile):
            message = self.failure(OBSERVED + "SECRET_TOKEN_DO_NOT_ECHO\n")
        self.assertIn("SQLite state runtime: permission denied", message)
        self.assertIn("normal permission approval", message)
        self.assertIn("Live catalog remains unavailable", message)
        self.assertNotIn("SECRET_TOKEN", message)
        self.assertNotIn("/private-user", message)
        self.assertTrue(errfile.closed)

    def test_permission_denied_variant_is_recognized(self):
        self.assertIn("normal permission approval", self.failure(
            "failed to initialize sqlite state runtime: Permission denied (os error 13)"))

    def test_unrelated_errors_do_not_suggest_permission_retry_or_leak(self):
        for stderr in ("SECRET_TOKEN failed to initialize sqlite state runtime: database corrupt",
                       "SECRET_TOKEN Operation not permitted", "SECRET_TOKEN network unavailable"):
            with self.subTest(stderr=stderr):
                self.assertEqual(self.failure(stderr), "Codex app-server exited during initialize")

    def test_diagnostics_read_only_bounded_tail(self):
        self.assertEqual(self.failure(OBSERVED + "x" * 20000),
                         "Codex app-server exited during initialize")

    def test_large_stderr_does_not_block_successful_rpc_and_closes_file(self):
        self.server("sys.stderr.write('x' * 262144)\nsys.stderr.flush()\n"
                    "for line in sys.stdin:\n"
                    "    request = json.loads(line)\n"
                    "    if 'id' in request:\n"
                    "        print(json.dumps({'id': request['id'], 'result': {}}), flush=True)\n")
        start = time.monotonic()
        rpc = install.RPC(str(self.binary), self.home)
        try:
            self.assertEqual(rpc.call("model/list", {}, timeout=2), {})
            self.assertLess(time.monotonic() - start, 5)
        finally:
            rpc.close()
        self.assertTrue(rpc.stderr.closed)
        self.assertIsNotNone(rpc.proc.poll())

    def test_capture_is_closed_when_process_cannot_start(self):
        errfile = tempfile.TemporaryFile()
        with patch.object(install.tempfile, "TemporaryFile", return_value=errfile):
            with self.assertRaises(FileNotFoundError):
                install.RPC(str(self.home / "missing"), self.home)
        self.assertTrue(errfile.closed)

    def test_broken_pipe_uses_same_safe_diagnostic(self):
        def send(rpc, message):
            rpc.proc.wait(timeout=2)
            raise BrokenPipeError("private detail")
        self.server(f"sys.stderr.write({OBSERVED!r})\n")
        with patch.object(install.RPC, "send", send):
            with self.assertRaisesRegex(RuntimeError, "normal permission approval"):
                install.RPC(str(self.binary), self.home)


if __name__ == "__main__":
    unittest.main()
