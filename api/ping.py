"""Dependency-free probe for bisecting a failing deployment.

Imports nothing from this project and nothing from requirements.txt. So:

  /api/ping responds, / does not  -> Vercel's Python runtime is fine and the
                                     failure is in importing the application
                                     (a dependency, or module-level code)
  neither responds                -> the failure is in the Vercel configuration
                                     or the build itself, before our code runs

Safe to delete once the deployment is healthy.
"""

from http.server import BaseHTTPRequestHandler


class handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - name required by BaseHTTPRequestHandler
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"ping ok - the vercel python runtime is working\n")
