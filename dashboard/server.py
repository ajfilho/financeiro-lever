#!/usr/bin/env python3
"""
Servidor local do Balanço Anual.
Executar: python3 dashboard/server.py
Abre:     http://localhost:8765
"""
import json, webbrowser, urllib.request, urllib.error, urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from datetime import datetime, timedelta

DASHBOARD    = Path(__file__).resolve().parent
IMPORTS_JSON = DASHBOARD / "imports.json"
DATA_JS      = DASHBOARD / "data.js"
CAIXA_JSON   = DASHBOARD / "caixa.json"
SECRETS_JSON = DASHBOARD / "secrets.json"
PORT = 8765

# USDC contract on Base (Coinbase L2)
USDC_BASE_CONTRACT = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
BASE_RPC_URL       = "https://mainnet.base.org"


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

        if path == "caixa":
            self._json(load_caixa())
            return

        if path == "lootrush-balance":
            self._json(fetch_lootrush_balance())
            return

        if path == "wise-cnpj-balance":
            self._json(fetch_wise_balance("cnpj"))
            return

        if path == "wise-llp-balance":
            self._json(fetch_wise_balance("llp"))
            return

        if path == "buygoods-pending":
            self._json(fetch_buygoods_pending())
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

        elif self.path == "/caixa":
            save_caixa(payload)
            self._json({"ok": True})

        elif self.path == "/buygoods-adjust":
            self._json(set_buygoods_adjustment(payload))

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


def load_caixa():
    try:
        return json.loads(CAIXA_JSON.read_text("utf-8")) if CAIXA_JSON.exists() else {}
    except Exception:
        return {}


def save_caixa(payload):
    CAIXA_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")


def load_secrets():
    try:
        return json.loads(SECRETS_JSON.read_text("utf-8")) if SECRETS_JSON.exists() else {}
    except Exception:
        return {}


def fetch_lootrush_balance():
    """
    Retorna saldo total LootRush = USDC na wallet operacional (via Base RPC)
                                 + collateral dos cards (via LootRush API).
    Requer secrets.json com lootrush_api_key e lootrush_wallet.
    """
    secrets = load_secrets()
    api_key = secrets.get("lootrush_api_key", "")
    wallet  = secrets.get("lootrush_wallet", "")
    result = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "wallet_usdc": 0,
        "card_collateral": 0,
        "total": 0,
    }
    if not api_key:
        result["error"] = "Configure lootrush_api_key em dashboard/secrets.json"
        return result

    ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) financeiro-dashboard/1.0"

    # 1. Card collateral pool (LootRush cards-balance API: runningBalance da última entrada)
    try:
        url = "https://history-api.lootrush.com/api/history?resource=cards&feature=cards-balance&currentPage=0&pageSize=1"
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {api_key}",
            "User-Agent": ua,
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
            nodes = data.get("cardIssuerUserCollateralMovement", [])
            if nodes:
                result["card_collateral"] = float(nodes[0].get("runningBalance", 0))
    except Exception as e:
        result["card_collateral_error"] = str(e)

    # 2. Wallet USDC balance (Base RPC: USDC.balanceOf(wallet))
    if wallet:
        try:
            addr_padded = wallet.lower().replace("0x", "").rjust(64, "0")
            call_data   = "0x70a08231" + addr_padded  # balanceOf(address) selector
            payload     = json.dumps({
                "jsonrpc": "2.0",
                "id":      1,
                "method":  "eth_call",
                "params":  [{"to": USDC_BASE_CONTRACT, "data": call_data}, "latest"],
            }).encode()
            req = urllib.request.Request(BASE_RPC_URL, data=payload, headers={
                "Content-Type": "application/json",
                "User-Agent": ua,
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=10) as r:
                rpc = json.loads(r.read())
                hex_result = rpc.get("result", "0x0")
                balance_micro = int(hex_result, 16)
                result["wallet_usdc"]    = balance_micro / 1_000_000  # USDC tem 6 decimais
                result["wallet_address"] = wallet
        except Exception as e:
            result["wallet_usdc_error"] = str(e)

    result["total"] = result["wallet_usdc"] + result["card_collateral"]
    return result


def fetch_bcb_ptax_usd():
    """USD→BRL PTAX, com fallback de até 5 dias (fim de semana/feriado)."""
    ua = "Mozilla/5.0 financeiro-dashboard/1.0"
    for i in range(5):
        d = datetime.now() - timedelta(days=i)
        m, dd, y = d.strftime("%m"), d.strftime("%d"), d.strftime("%Y")
        url = (
            f"https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/odata/"
            f"CotacaoDolarDia(dataCotacao=@d)?@d=%27{m}-{dd}-{y}%27"
            f"&%24format=json&%24select=cotacaoVenda"
        )
        try:
            req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=8) as r:
                data = json.loads(r.read())
                if data.get("value"):
                    return float(data["value"][-1]["cotacaoVenda"])
        except Exception:
            continue
    return None


def fetch_wise_balance(label):
    """
    Saldo total Wise pro label informado (cnpj/llp).
    Lê token em secrets.json[f'wise_{label}_token'].
    Lista profiles → pega o business profile → busca balances → soma em USD via PTAX.
    """
    secrets = load_secrets()
    token = secrets.get(f"wise_{label}_token", "")
    result = {
        "label": label,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "balances": [],
        "total_usd": 0,
    }
    if not token:
        result["error"] = f"Configure wise_{label}_token em dashboard/secrets.json"
        return result

    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent":    "Mozilla/5.0 financeiro-dashboard/1.0",
        "Accept":        "application/json",
    }

    try:
        # 1. List profiles
        req = urllib.request.Request("https://api.wise.com/v1/profiles", headers=headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            profiles = json.loads(r.read())
        biz = next((p for p in profiles if p.get("type") == "business"), None)
        if not biz and profiles:
            biz = profiles[0]
        if not biz:
            result["error"] = "Nenhum profile encontrado nessa conta Wise"
            return result
        profile_id = biz["id"]
        result["profile_id"] = profile_id
        result["profile_name"] = (
            biz.get("details", {}).get("name")
            or biz.get("fullName")
            or biz.get("details", {}).get("businessName")
            or "?"
        )

        # 2. Get standard balances
        req = urllib.request.Request(
            f"https://api.wise.com/v4/profiles/{profile_id}/balances?types=STANDARD",
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            balances_raw = json.loads(r.read())

        balances = []
        for b in balances_raw:
            amt = b.get("amount", {})
            balances.append({
                "currency": b.get("currency"),
                "amount":   float(amt.get("value", 0)),
            })
        result["balances"] = balances

        # 3. Soma em USD (USD direto, BRL via PTAX, demais ignoradas)
        ptax = fetch_bcb_ptax_usd()
        result["ptax_used"] = ptax
        total_usd = 0.0
        skipped = []
        for b in balances:
            if b["currency"] == "USD":
                total_usd += b["amount"]
            elif b["currency"] == "BRL" and ptax:
                total_usd += b["amount"] / ptax
            elif b["amount"] != 0:
                skipped.append(f"{b['currency']}={b['amount']}")
        result["total_usd"] = round(total_usd, 2)
        if skipped:
            result["warnings"] = "Moedas não convertidas pra USD: " + ", ".join(skipped)

    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", errors="ignore")[:300]
        except Exception:
            pass
        result["error"] = f"HTTP {e.code}: {err_body or e.reason}"
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"

    return result


def _http_get_json(url, headers=None, timeout=10, retries=1):
    """GET com retry em timeout (TLS cold start é frequente em Cloudflare workers.dev)."""
    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers or {})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except Exception as e:
            last_err = e
            if attempt < retries:
                continue
            raise last_err


def _find_latest_buygoods_deposit_iso(api_key):
    """Procura o deposit mais recente vindo de BUYGOODS na LootRush. Retorna ISO ou None."""
    if not api_key:
        return None
    ua = "Mozilla/5.0 financeiro-dashboard/1.0"
    try:
        # pageSize=50 já cobre vários dias de transações (LootRush tem dust frequente).
        # timeout maior porque o response pode ser grande e a API deles é lenta.
        url = ("https://history-api.lootrush.com/api/history"
               "?resource=account&feature=account&currentPage=0&pageSize=50")
        data = _http_get_json(url, headers={
            "Authorization": f"Bearer {api_key}",
            "User-Agent":    ua,
            "Accept":        "application/json",
        }, timeout=25)
        for n in data.get("nodes", []):
            desc = (n.get("description") or "").upper()
            if "BUYGOODS" in desc and (n.get("relatedType") or "").startswith("buy"):
                return n.get("createdAt")
    except Exception as e:
        # Log silencioso — pending continua disponível mesmo se a detecção falhar
        return None
    return None


def fetch_buygoods_pending():
    """
    Lê o saldo BuyGoods pendente do Cloudflare Worker.
    Antes de ler, sincroniza o 'last_payout' do worker com o último deposit
    BUYGOODS visto na LootRush — assim o pendente sempre reflete "desde o
    último lote já recebido".
    """
    secrets   = load_secrets()
    worker    = (secrets.get("buygoods_worker_url") or "").rstrip("/")
    read_tok  = secrets.get("buygoods_read_token") or ""
    api_key   = secrets.get("lootrush_api_key") or ""
    result    = {"timestamp": datetime.now().isoformat(timespec="seconds")}

    if not worker or not read_tok:
        result["error"] = ("Configure buygoods_worker_url e buygoods_read_token "
                           "em dashboard/secrets.json. Veja worker/README.md.")
        return result

    ua = "Mozilla/5.0 financeiro-dashboard/1.0"

    # 1) Tenta sincronizar payout via LootRush (se mais recente que o gravado)
    deposit_iso = _find_latest_buygoods_deposit_iso(api_key)
    try:
        cur = _http_get_json(f"{worker}/pending?token={read_tok}", headers={
            "User-Agent": ua, "Accept": "application/json"
        }, timeout=10, retries=2)
        current_last = cur.get("last_payout", "1970-01-01T00:00:00Z")
    except Exception as e:
        result["error"] = f"Erro lendo worker: {e}"
        return result

    if deposit_iso and deposit_iso > current_last:
        try:
            mark_url = f"{worker}/mark-payout?token={read_tok}&ts={urllib.parse.quote(deposit_iso)}"
            req = urllib.request.Request(mark_url, method="POST",
                                          headers={"User-Agent": ua, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                _ = r.read()
            result["auto_marked_payout"] = deposit_iso
            # Re-lê pendente após marcar
            cur = _http_get_json(f"{worker}/pending?token={read_tok}", headers={
                "User-Agent": ua, "Accept": "application/json"
            }, timeout=15)
        except Exception as e:
            result["mark_payout_error"] = str(e)

    # 2) Devolve o resultado
    result.update({
        "pending_usd":           cur.get("pending_usd", 0),
        "sales_usd":             cur.get("sales_usd", 0),
        "manual_adjustment_usd": cur.get("manual_adjustment_usd", 0),
        "manual_adjustment_note": cur.get("manual_adjustment_note", ""),
        "manual_adjustment_updated": cur.get("manual_adjustment_updated", ""),
        "sales_count":           cur.get("sales_count", 0),
        "last_payout":           cur.get("last_payout"),
        "recent_sales":          cur.get("recent_sales", []),
    })
    return result


def set_buygoods_adjustment(payload):
    """Proxia POST pro worker /adjust. payload: {amount: float, note: str}"""
    secrets  = load_secrets()
    worker   = (secrets.get("buygoods_worker_url") or "").rstrip("/")
    read_tok = secrets.get("buygoods_read_token") or ""
    if not worker or not read_tok:
        return {"error": "Configure buygoods_worker_url e buygoods_read_token em secrets.json"}

    try:
        body = json.dumps({
            "amount": float(payload.get("amount", 0)),
            "note":   str(payload.get("note", "")),
        }).encode()
        url = f"{worker}/adjust?token={read_tok}"
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": "application/json",
            "User-Agent":   "Mozilla/5.0 financeiro-dashboard/1.0",
            "Accept":       "application/json",
        })
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}


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
