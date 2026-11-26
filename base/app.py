import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

ROLE = os.environ["APP_ROLE"]
VERSION = os.environ["APP_VERSION"]
POD = os.environ.get("HOSTNAME", "local")
INVENTORY_URL = os.environ.get("INVENTORY_URL", "http://inventory:8080/")
COHORT_HEADER = "x-release-cohort"
HTTP = build_opener(ProxyHandler({}))


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/healthz":
            self.reply(200, {"status": "ok", "service": ROLE})
        elif ROLE == "inventory" and path == "/":
            self.reply(200, {
                "service": ROLE,
                "version": VERSION,
                "pod": POD,
                "cohort_received": self.headers.get(COHORT_HEADER),
                "mesh_route": self.headers.get("x-lab-mesh-route"),
            })
        elif ROLE == "storefront" and path in ("/shop", "/shop/no-context"):
            self.call_inventory(forward_cohort=(path == "/shop"))
        else:
            self.reply(404, {"error": "application path not found", "service": ROLE})

    def call_inventory(self, forward_cohort):
        cohort = self.headers.get(COHORT_HEADER)
        headers = {COHORT_HEADER: cohort} if forward_cohort and cohort else {}
        try:
            with HTTP.open(Request(INVENTORY_URL, headers=headers), timeout=3) as response:
                inventory = json.load(response)
        except (URLError, TimeoutError, ValueError) as error:
            self.log_error("inventory request failed: %s", error)
            self.reply(502, {"error": "inventory upstream failed", "service": ROLE})
            return

        self.reply(200, {
            "service": ROLE,
            "version": VERSION,
            "ingress_route": self.headers.get("x-lab-ingress-route"),
            "received_cohort": cohort,
            "forwarded_cohort": headers.get(COHORT_HEADER),
            "inventory": inventory,
        })


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
