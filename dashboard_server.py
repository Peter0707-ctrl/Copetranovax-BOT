"""
CopetraNova -- dashboard_server.py
==================================
Hardened HTTP HUD server for CopetraNova Quantum Trading Bot.
Strictly whitelists dashboard static assets and JSON telemetry endpoints.
Blocks all source code (.py), model weights, .git, .env, and path traversal probes.
"""

import os
import sys
import json
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler

BASE_DIR     = os.path.abspath(os.path.dirname(__file__))
ICONS_DIR    = os.path.abspath(os.path.join(BASE_DIR, "icons"))
ASSETS_DIR   = os.path.abspath(os.path.join(BASE_DIR, "assets"))
SIGNALS_JSON = os.path.join(BASE_DIR, "data", "signals.json")
HTML_FILE    = os.path.join(BASE_DIR, "dashboard.html")
PORT         = int(sys.argv[1]) if len(sys.argv) > 1 else 8080


class HUDRequestHandler(BaseHTTPRequestHandler):
    server_version = "CopetraHUD/2.0"
    sys_version = ""

    def _send_security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-XSS-Protection", "1; mode=block")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("X-Permitted-Cross-Domain-Policies", "none")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' data: https://fonts.gstatic.com; img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; object-src 'none';"
        )

    def do_GET(self):
        raw_path = urllib.parse.unquote(self.path.split("?")[0].split("#")[0])

        # Block any path traversal, hidden files, or source code probes immediately
        if (
            ".." in raw_path
            or "\0" in raw_path
            or raw_path.endswith(".py")
            or raw_path.endswith(".pkl")
            or raw_path.endswith(".env")
            or "/.git" in raw_path
            or "/.env" in raw_path
        ):
            self.send_response(403)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self._send_security_headers()
            self.end_headers()
            self.wfile.write(b"403 Forbidden: Access Denied")
            return

        if raw_path in ("/", "/dashboard", "/hud"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store, max-age=0")
            self._send_security_headers()
            self.end_headers()
            if os.path.exists(HTML_FILE):
                with open(HTML_FILE, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.wfile.write(b"<h1>Error: dashboard.html not found</h1>")
            return

        if raw_path == "/manifest.json":
            manifest_file = os.path.join(BASE_DIR, "manifest.json")
            if os.path.exists(manifest_file):
                self.send_response(200)
                self.send_header("Content-Type", "application/manifest+json")
                self._send_security_headers()
                self.end_headers()
                with open(manifest_file, "rb") as f:
                    self.wfile.write(f.read())
                return

        if raw_path == "/sw.js":
            sw_file = os.path.join(BASE_DIR, "sw.js")
            if os.path.exists(sw_file):
                self.send_response(200)
                self.send_header("Content-Type", "application/javascript; charset=utf-8")
                self.send_header("Service-Worker-Allowed", "/")
                self._send_security_headers()
                self.end_headers()
                with open(sw_file, "rb") as f:
                    self.wfile.write(f.read())
                return

        if raw_path.startswith("/icons/") or raw_path.startswith("/assets/"):
            target_file = os.path.abspath(os.path.join(BASE_DIR, raw_path.lstrip("/\\")))
            allowed_root = ICONS_DIR if raw_path.startswith("/icons/") else ASSETS_DIR
            if (
                target_file.startswith(allowed_root + os.sep)
                and os.path.isfile(target_file)
                and target_file.lower().endswith((".png", ".svg", ".jpg", ".jpeg", ".webp", ".ico"))
            ):
                self.send_response(200)
                if target_file.endswith(".png"):
                    self.send_header("Content-Type", "image/png")
                elif target_file.endswith(".svg"):
                    self.send_header("Content-Type", "image/svg+xml")
                elif target_file.endswith((".jpg", ".jpeg")):
                    self.send_header("Content-Type", "image/jpeg")
                elif target_file.endswith(".webp"):
                    self.send_header("Content-Type", "image/webp")
                else:
                    self.send_header("Content-Type", "image/x-icon")
                self.send_header("Cache-Control", "public, max-age=86400")
                self._send_security_headers()
                self.end_headers()
                with open(target_file, "rb") as f:
                    self.wfile.write(f.read())
                return

        if raw_path == "/api/recent_signals":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store, max-age=0")
            self._send_security_headers()
            self.end_headers()
            if os.path.exists(SIGNALS_JSON):
                try:
                    with open(SIGNALS_JSON, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    self.wfile.write(json.dumps(data.get("history", [])).encode("utf-8"))
                    return
                except Exception:
                    pass
            self.wfile.write(b"[]")
            return

        if raw_path in ("/api/latest_signal", "/api/status"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store, max-age=0")
            self._send_security_headers()
            self.end_headers()
            if os.path.exists(SIGNALS_JSON):
                try:
                    with open(SIGNALS_JSON, "rb") as f:
                        self.wfile.write(f.read())
                    return
                except Exception:
                    pass
            default_state = {
                "latest_signal": {
                    "direction": "SCANNING",
                    "trade_type": "M15 CYCLE ACTIVE",
                    "tier": "SCANNER",
                    "accuracy": 85.0,
                    "grade": "A",
                    "entry": 0.0,
                    "sl": 0.0,
                    "tp": 0.0,
                    "sl_pips": 15,
                    "tp_pips": 30,
                    "rr": 2.0,
                    "lot": 0.01,
                    "reasoning": "Bot is actively scanning M15 candle sequences. Next signal will broadcast at candle close.",
                    "session": "LONDON",
                    "timestamp": ""
                },
                "market_status": {
                    "symbol": "XAUUSD",
                    "price": 0.0,
                    "session": "ACTIVE",
                    "h1_bias": "BULL",
                    "next_candle_sec": 450
                },
                "history": []
            }
            self.wfile.write(json.dumps(default_state).encode("utf-8"))
            return

        # Strict deny-by-default: never fall back to directory/file listing
        self.send_response(404)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(b"404 Not Found")

    def log_message(self, format, *args):
        return


def run_server(port=PORT):
    host = os.getenv("HUD_BIND_HOST", "127.0.0.1")
    server_address = (host, port)
    httpd = HTTPServer(server_address, HUDRequestHandler)
    print("\n  ======================================================")
    print("  COPETRANOVAX // QUANTUM ROBOTIC HUD SERVER (HARDENED)")
    print(f"  Live UI: http://{host}:{port}")
    print("  Press Ctrl+C to stop")
    print("  ======================================================\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  [HUD Server] Stopped by user.")
        httpd.server_close()


if __name__ == "__main__":
    run_server()
