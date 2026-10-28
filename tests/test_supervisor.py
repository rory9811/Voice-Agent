import os
import subprocess
import sys
import unittest

from demos.launcher.supervisor import process_start


class ProcessStartTests(unittest.TestCase):
    def test_process_start_is_stable(self):
        first = process_start(os.getpid())
        self.assertTrue(first)
        self.assertEqual(process_start(os.getpid()), first)

    def test_reaped_child_has_no_start(self):
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        self.assertIsNone(process_start(child.pid))



class HealthProbeTests(unittest.TestCase):
    def test_healthy_ignores_proxy_env(self):
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from unittest.mock import patch

        from demos.launcher.supervisor import healthy

        class Ready(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"ready": True}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Ready)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_port}/healthz"
        with patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9", "no_proxy": "", "NO_PROXY": ""}):
            self.assertTrue(healthy(url))


if __name__ == "__main__":
    unittest.main()
