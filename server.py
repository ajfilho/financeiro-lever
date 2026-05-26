#!/usr/bin/env python3
"""
Servidor local do Balanço Anual.
Executar: python3 dashboard/server.py
Abre:     http://localhost:8765
"""
import json, webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from datetime import datetime

DASHBOARD   = Path(__file__).resolve().parent
IMPORTS_JSON = DASHBOARD / "imports.json"
DATA_JS      = DASHBOARD / "data.js"
PORT = 8765


# ── HTTP Handler ────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass  # silencia logs do servidor

    # ── GET ──────────────────────────────────────────────────────────────────
    def do_GET(self):
        path = self.path.split("?")[0].lstrip("/") or "index.html"

        if path == "imports":
            imports = load_imports()
            light = {}
            for k, v in imports.items():
                txs = v.get("transactions", [])
                light[k] = {ek: ev for ek, ev in v.items() if ek != "transactions"}
                light[k]["count"]   = len(txs)
                light[k]["income"]  = sum(t.get("credit", 0) for t in txs)
                light[k]["expense"] = sum(t.get("debit",  0) for t in txs)
            self._json(light)
            return

        file = DASHBOARD / path
        if not file.exists() or not file.is_file():
            self.send_error(404); return

        ext_map = {"html":"text/html","js":"application/javascript",
                   "css":"text/css","json":"application/json"}
        ct = ext_map.get(file.suffix.lstrip("."), "application/octet-stream")
        data = file.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", len(data))
        self.end_headers()
        self.wfile.write(data)

    # ── POST ─────────────────────────────────────────────────────────────────
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length))
        except Exception as e:
            self._json({"error": str(e)}, 400); return

        if self.path == "/import":
            imports = load_imports()
            key = payload["storageKey"]
            imports[key] = {
                "account":    payload["account"],
                "accId":      payload["accId"],
                "month":      key,
                "filename":   payload.get("filename", ""),
                "importedAt": datetime.now().isoformat(timespec="seconds"),
                "transactions": payload["transactions"],
            }
            save_imports(imports)
            regenerate_data_js(imports)
            self._json({"ok": True})

        elif self.path == "/remove":
            imports = load_imports()
            key = payload.get("key", "")
            if key in imports:
                del imports[key]
                save_imports(imports)
                regenerate_data_js(imports)
            self._json({"ok": True})

        else:
            self.send_error(404)

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)


# ── Funções de dados ─────────────────────────────────────────────────────────

def load_imports():
    try:
        return json.loads(IMPORTS_JSON.read_text("utf-8")) if IMPORTS_JSON.exists() else {}
    except Exception:
        return {}


def save_imports(imports):
    IMPORTS_JSON.write_text(json.dumps(imports, ensure_ascii=False, indent=2), "utf-8")


def regenerate_data_js(imports):
    accounts_dict = {}
    for entry in imports.values():
        aid = entry["accId"]
        if aid not in accounts_dict:
            accounts_dict[aid] = {
                "id": aid, "name": entry["account"],
                "currency": "USD", "transactions": []
            }
        existing = {t["id"] for t in accounts_dict[aid]["transactions"]}
        for tx in entry.get("transactions", []):
            if tx["id"] not in existing:
                accounts_dict[aid]["transactions"].append(tx)
                existing.add(tx["id"])

    for acc in accounts_dict.values():
        acc["transactions"].sort(key=lambda t: (t["date"], t["time"]))

    payload = {
        "accounts": list(accounts_dict.values()),
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
    }
    DATA_JS.write_text(
        "window.financialData = " + json.dumps(payload, ensure_ascii=False, indent=2) + ";\n",
        "utf-8"
    )


# ── Entrypoint ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    server = HTTPServer(("localhost", PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"Dashboard rodando em {url}")
    print("Ctrl+C para parar\n")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor parado.")
