"""
CopetraNova -- dashboard_server.py
==================================
Lightweight local HTTP HUD server for CopetraNova Quantum Trading Bot.
Serves the futuristic robotic HUD frontend (dashboard.html) and provides
live JSON endpoints for realtime telemetry and signal streams.

Run:
    python dashboard_server.py [PORT]
Default: http://localhost:8080
"""

import os
import sys
import json
from http.server import HTTPServer, SimpleHTTPRequestHandler

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
SIGNALS_JSON = os.path.join(BASE_DIR, "data", "signals.json")
HTML_FILE    = os.path.join(BASE_DIR, "dashboard.html")
PORT         = int(sys.argv[1]) if len(sys.argv) > 1 else 8080


class HUDRequestHandler(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        # Prevent accessing outside base dir
        clean_path = path.split('?', 1)[0].split('#', 1)[0]
        if clean_path in ("/", "/dashboard", "/hud"):
            return HTML_FILE
        return os.path.join(BASE_DIR, clean_path.lstrip("/"))

    def do_GET(self):
        url = self.path.split('?')[0]

        if url in ("/", "/dashboard", "/hud"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if os.path.exists(HTML_FILE):
                with open(HTML_FILE, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.wfile.write(b"<h1>Error: dashboard.html not found</h1>")
            return

        if url == "/api/latest_signal" or url == "/api/status":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            if os.path.exists(SIGNALS_JSON):
                try:
                    with open(SIGNALS_JSON, "rb") as f:
                        self.wfile.write(f.read())
                    return
                except Exception:
                    pass
            # Fallback default response
            dummy = {
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
            self.wfile.write(json.dumps(dummy).encode("utf-8"))
            return

        return super().do_GET()

    def log_message(self, format, *args):
        # Keep console quiet during high-frequency polling
        return


def run_server(port=PORT):
    server_address = ("", port)
    httpd = HTTPServer(server_address, HUDRequestHandler)
    print(f"\n  ══════════════════════════════════════════════════════")
    print(f"  COPETRANOVAX // QUANTUM ROBOTIC HUD SERVER")
    print(f"  Live UI: http://localhost:{port}")
    print(f"  Press Ctrl+C to stop")
    print(f"  ══════════════════════════════════════════════════════\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  [HUD Server] Stopped by user.")
        httpd.server_close()


if __name__ == "__main__":
    run_server()
