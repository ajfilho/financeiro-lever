#!/usr/bin/env python3
"""
Importa 4 extratos novos:
  - Wise LLP (PDF)            → wise-llp_<period>
  - Payonner LLP (CSV)        → payonner-llp_<period>
  - LootRush LLP (xlsx)       → lootrush-llp_<period>            (transferências)
  - LootRush LLP (CSV)        → lootrush-llp_<period>_card       (cartão)

Lê cada arquivo, normaliza pra mesma estrutura que os parsers JS produzem,
atualiza imports.json e regenera data.js.
"""
import csv, json, re, sys
from datetime import datetime
from pathlib import Path

import openpyxl
import pdfplumber

DASHBOARD = Path(__file__).resolve().parent
IMPORTS_JSON = DASHBOARD / "imports.json"
DATA_JS = DASHBOARD / "data.js"

DOWNLOADS = Path("/Users/achrafjalal/Downloads/drive-download-20260505T130838Z-3-001")
WISE_PDF = DOWNLOADS / "statement_126692175_USD_2026-04-01_2026-04-30.pdf"
PAYONNER_CSV = DOWNLOADS / "Transactions_04-2026.csv"
LOOTRUSH_XLSX = DOWNLOADS / "5f1bc3b6-2e7e-4a5b-bf4b-beb713ea0cd5.xlsx"
LOOTRUSH_CARD_CSV = DOWNLOADS / "b6de6098-7328-4c00-a7ae-56a581dd5959-card-transactions.csv"


# ─── Helpers ────────────────────────────────────────────────────────────────

def to_float(s, default=0.0):
    if s is None or s == "":
        return default
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).strip()
    if not s:
        return default
    # Brazilian format: "1.234,56" → "1234.56"
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s and s.count(",") == 1 and re.match(r"^-?\d+(\.\d{3})*,\d+$", s):
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return default


def title_case(s):
    if not s:
        return ""
    return " ".join(w[0].upper() + w[1:].lower() if len(w) > 2 and w.isupper() else w for w in s.split())


def parse_category(src):
    cleaned = re.sub(r"\s*\(USD balance\)\s*$", "", src or "", flags=re.I).strip()
    if " - " in cleaned:
        cat, sub = cleaned.split(" - ", 1)
        return cat.strip(), sub.strip()
    return cleaned, ""


def extract_merchant(desc):
    m = re.search(r"\((.*?)\)", desc or "")
    return m.group(1) if m else ""


# ─── Payoneer parser ────────────────────────────────────────────────────────

def parse_payoneer(rows):
    out = []
    for row in rows:
        date_raw = (row.get("Transaction Date") or "").strip()
        if not date_raw:
            continue
        m, d, y = date_raw.split("/")
        iso = f"{y}-{m.zfill(2)}-{d.zfill(2)}"

        credit = to_float(row.get("Credit Amount"))
        debit = to_float(row.get("Debit Amount"))
        desc = (row.get("Description") or "").strip()
        source = (row.get("Source") or "").strip()
        target = (row.get("Target") or "").strip()

        is_payment = desc.lower().startswith("payment from")
        is_card = desc.lower().startswith("card charge")

        if is_payment and credit > 0:
            tx_type = "income"
            category = "Receita"
            subcategory = re.sub(r"\s*\(.*?\)\s*", "", source).strip().title() or "Outros"
            merchant = subcategory
        elif is_card and credit > 0:
            tx_type = "refund"
            category, subcategory = parse_category(source)
            merchant = target or extract_merchant(desc)
        elif is_card and debit > 0:
            tx_type = "expense"
            category, subcategory = parse_category(source)
            merchant = target or extract_merchant(desc)
        else:
            tx_type = "other"
            category, subcategory = parse_category(source) if source else ("Outros", "")
            merchant = target or ""

        out.append({
            "date": iso,
            "time": (row.get("Transaction Time") or "00:00:00").strip(),
            "id": (row.get("Transaction ID") or "").strip() or f"pay_{len(out)}",
            "description": desc,
            "credit": credit, "debit": debit,
            "currency": (row.get("Currency") or "USD").strip(),
            "status": (row.get("Status") or "Completed").strip(),
            "runningBalance": to_float(row.get("Running Balance")),
            "additional": (row.get("Additional Description") or "").strip(),
            "category": category, "subcategory": subcategory,
            "merchant": merchant, "type": tx_type,
        })
    return out


# ─── LootRush Transfer parser (xlsx) ────────────────────────────────────────

def parse_lootrush_transfer(rows):
    out = []
    for row in rows:
        date_raw = row.get("Transaction Date")
        if date_raw is None:
            continue
        if isinstance(date_raw, datetime):
            iso = date_raw.strftime("%Y-%m-%d")
            time = date_raw.strftime("%H:%M:%S")
        else:
            s = str(date_raw).strip()
            if not s:
                continue
            iso = s[:10]
            time = s[11:19] if len(s) >= 19 else "00:00:00"

        net_change = to_float(row.get("Net Change"))
        if net_change == 0:
            continue
        desc = str(row.get("Description") or "").strip()
        tx_type_field = str(row.get("Transaction Type") or "").strip()

        # ── Sanity-correct LootRush unit mismatch ───────────────────────
        # LootRush sometimes stores Net Change in raw token microunits
        # (×10^6) while the description shows the human-readable amount.
        # When the description amount differs from the column by ≥100x,
        # trust the description.
        m = re.match(r"^(?:Sent|Received)\s+([\d.]+)\s+(USDT|USDC|USD)\b", desc, re.I)
        if m:
            desc_amount = float(m.group(1))
            if abs(net_change) > desc_amount * 100 and desc_amount > 0:
                net_change = -desc_amount if net_change < 0 else desc_amount

        credit = net_change if net_change > 0 else 0
        debit = abs(net_change) if net_change < 0 else 0

        # ── Detect internal transfer (wallet → LootRush card) ───────────
        # "Sent X to 0x... (Credit Card)" is just funding the card, not an expense.
        is_internal_transfer = bool(re.search(r"\(Credit Card\)\s*$|to\s+Credit Card", desc, re.I))

        if is_internal_transfer:
            tx_type = "transfer"
            category = "Transferência Interna"
            merchant = "Credit Card (LootRush)"
            subcategory = "Wallet → Cartão"
        elif credit > 0:
            tx_type = "income"
            category = "Receita"
            mfrom = re.search(r"from\s+([A-Z][A-Z0-9 .,'&-]+?)(?:\s{2,}|\s+\w{2}\s+\d{5}|$)", desc, re.I)
            merchant = mfrom.group(1).strip() if mfrom else (tx_type_field or "Depósito")
            subcategory = merchant
        else:
            tx_type = "expense"
            category = tx_type_field or "Transferência"
            mto = re.search(r"to\s+([A-Z][A-Z0-9 .,'&-]+?)(?:\s{2,}|\s+\w{2}\s+\d{5}|$)", desc, re.I)
            merchant = mto.group(1).strip() if mto else desc[:40]
            subcategory = category

        currency = str(
            row.get("Asset Received") if credit > 0 else row.get("Asset Sent") or ""
        ).strip() or "USD"

        out.append({
            "date": iso, "time": time,
            "id": str(row.get("id") or "").strip() or f"lr_{len(out)}",
            "description": desc, "credit": credit, "debit": debit,
            "currency": currency,
            "status": str(row.get("Status") or "Completed").strip(),
            "runningBalance": to_float(row.get("Account Balance")),
            "additional": str(row.get("Purpose") or "").strip(),
            "category": category, "subcategory": subcategory,
            "merchant": merchant, "type": tx_type,
        })
    return out


# ─── LootRush Card parser (csv) ─────────────────────────────────────────────

CARD_KNOWN = {
    "FACEBK": "Facebook Ads", "GOOGL": "Google Ads", "AMZN": "Amazon",
    "PAYPAL": "PayPal", "TWILIO": "Twilio", "CLKBANK": "ClickBank",
    "LATAM": "LATAM Air", "UBER": "Uber", "NETFLIX": "Netflix",
}

def clean_card_merchant(desc):
    up = desc.upper()
    for k, v in CARD_KNOWN.items():
        if k in up:
            return v
    return re.sub(r"\*[A-Z0-9]+\s*", "", desc).strip() or desc


def parse_lootrush_card(rows):
    out = []
    for row in rows:
        date_raw = (row.get("transaction date") or "").strip()
        if not date_raw:
            continue
        amount = to_float(row.get("amount"))
        if amount == 0:
            continue
        desc = (row.get("description") or "").strip()
        nickname = (row.get("nickname") or "").strip()
        status = (row.get("status") or "").strip()
        # Skip non-real charges: pending (not yet finalized), declined (rejected
        # by issuer — money never left), on hold (frozen, may resolve either way)
        if status.lower() in ("pending", "declined", "on hold"):
            continue
        # Skip internal collateral mechanics — these are temporary holds that
        # net to zero (DEPOSIT locks funds, RETURN releases them).
        if re.match(r"^COLLATERAL\s+(DEPOSIT|RETURN)\b", desc, re.I):
            continue

        merchant = clean_card_merchant(desc)
        out.append({
            "date": date_raw, "time": "00:00:00",
            "id": (row.get("transaction id") or "").strip() or f"lrc_{len(out)}",
            "description": desc, "credit": 0, "debit": amount,
            "currency": "USD",
            "status": status or "Settled",
            "runningBalance": 0,
            "additional": nickname,
            "category": "Cartão", "subcategory": nickname,
            "merchant": merchant, "type": "expense",
        })
    return out


# ─── Wise PDF parser ────────────────────────────────────────────────────────

PT_MONTHS = {
    "janeiro": "01", "fevereiro": "02", "março": "03", "abril": "04",
    "maio": "05", "junho": "06", "julho": "07", "agosto": "08",
    "setembro": "09", "outubro": "10", "novembro": "11", "dezembro": "12",
}

def pt_date(text):
    """'27 de abril de 2026' → '2026-04-27'."""
    m = re.match(r"(\d{1,2})\s+de\s+(\w+)\s+de\s+(\d{4})", text.strip(), re.I)
    if not m:
        return None
    d, mn, y = m.group(1), m.group(2).lower(), m.group(3)
    if mn not in PT_MONTHS:
        return None
    return f"{y}-{PT_MONTHS[mn]}-{d.zfill(2)}"


def parse_wise_pdf(pdf_path):
    with pdfplumber.open(pdf_path) as pdf:
        full_text = "\n".join(p.extract_text() or "" for p in pdf.pages)
    lines = [l for l in full_text.split("\n") if l.strip()]

    transactions = []
    i = 0
    # Find start of transaction list
    while i < len(lines) and "Descrição" not in lines[i]:
        i += 1
    i += 1

    cur = None  # accumulating description lines
    while i < len(lines):
        line = lines[i].strip()

        # Skip footer / asset table
        if any(x in line for x in (
            "Ativo Empresa de investimento", "Wise Assets UK", "Administrador e Escrivão",
            "BlackRock Institutional", "Wise Payments Limited", "wise.com/help",
            "ref:", "Tel: +44",
        )):
            i += 1
            continue

        # Look for "Transação:" — that's the metadata line that closes a transaction
        m_meta = re.search(r"Transação:\s*([A-Z_]+(?:[-_]\d+|-invoice-\d+))", line)
        if m_meta and cur:
            tx_id = m_meta.group(1)
            # Date is at start of this metadata line: "27 de abril de 2026 ..."
            m_date = re.match(r"(\d{1,2}\s+de\s+\w+\s+de\s+\d{4})", line)
            iso = pt_date(m_date.group(1)) if m_date else None

            # Card info
            card_match = re.search(r"Cartão terminado em\s*(\d+)\s+([A-Z\s]+?)(?=\s*Transação:)", line)
            # Reference (from metadata line)
            ref_match = re.search(r"Referência:\s*(.+?)$", line)
            ref = ref_match.group(1).strip() if ref_match else cur.get("ref", "")

            description = cur["desc"]
            amount = cur["amount"]
            balance = cur["balance"]

            credit = amount if amount > 0 else 0
            debit = abs(amount) if amount < 0 else 0

            # Determine type/category/merchant from description + tx_id
            payer_name = ""
            payee_name = ""
            merchant_raw = ""
            detail_type = ""

            if tx_id.startswith("CARD_TRANSACTION_CASHBACK"):
                detail_type = "CARD_CASHBACK"
                tx_type = "refund"
                category = "Cartão"
                subcategory = "Cashback"
                merchant = "Cashback"
            elif tx_id.startswith("CARD-"):
                detail_type = "CARD"
                tx_type = "expense"
                category = "Cartão"
                subcategory = "Cartão Wise"
                # "Transação por cartão de X USD emitida por MERCHANT"
                m_mer = re.search(r"emitida por\s+(.+)$", description, re.I)
                merchant_raw = m_mer.group(1).strip() if m_mer else description
                # JS does: replace trailing /\s+[A-Z][A-Z.]{2,}(\s+[A-Z][A-Z.]{2,})*$/
                merchant = re.sub(r"\s+[A-Z][A-Z.]{2,}(?:\s+[A-Z][A-Z.]{2,})*$", "", merchant_raw).strip() or merchant_raw
            elif tx_id.startswith("ACCRUAL_CHECKOUT") or tx_id.startswith("ACCRUAL"):
                detail_type = "TRANSFER"
                tx_type = "expense"
                category = "Transferência"
                merchant = "Wise (taxa de serviço)"
                subcategory = "Tarifa Wise"
            elif tx_id.startswith("TRANSFER-"):
                detail_type = "TRANSFER"
                if amount > 0:
                    tx_type = "income"
                    category = "Receita"
                    m_from = re.search(r"Recebeu dinheiro de\s+(.+?)(?:\s+com a referência|$)", description, re.I)
                    payer_name = m_from.group(1).strip() if m_from else ""
                    merchant = payer_name or "Wise"
                    subcategory = merchant
                else:
                    tx_type = "expense"
                    category = "Transferência"
                    m_to = re.search(r"Enviou dinheiro para\s+(.+?)$", description, re.I)
                    payee_name = m_to.group(1).strip() if m_to else ""
                    merchant = payee_name or description[:50]
                    subcategory = "Transferência"
            else:
                detail_type = "OTHER"
                tx_type = "expense" if amount < 0 else "income"
                category = "Outros"
                merchant = description[:50]
                subcategory = ""

            transactions.append({
                "date": iso or "", "time": "00:00:00",
                "id": tx_id,
                "description": description,
                "credit": credit, "debit": debit,
                "currency": "USD",
                "status": "Completed",
                "runningBalance": balance,
                "additional": ref,
                "category": category, "subcategory": subcategory,
                "merchant": merchant, "type": tx_type,
            })
            cur = None
            i += 1
            continue

        # Detect description+amount+balance line: ends with "  AMOUNT  BALANCE"
        # Amount: "-50,00" or "6.680,07" or "-3.365,89"
        # Balance: "169.976,97"
        m_amounts = re.search(
            r"^(.+?)\s+(-?\d{1,3}(?:\.\d{3})*,\d{2})\s+(\d{1,3}(?:\.\d{3})*,\d{2})\s*$",
            line,
        )
        if m_amounts:
            desc_part = m_amounts.group(1).strip()
            amount = to_float(m_amounts.group(2))
            balance = to_float(m_amounts.group(3))
            cur = {"desc": desc_part, "amount": amount, "balance": balance}
        elif cur and "Transação:" not in line:
            # Continuation of previous description (e.g., line 21 of "1 - 15, 2026"")
            # Skip lines that look like asset trade info
            if not re.search(r"unidades\s+(compradas|vendidas)", line, re.I):
                cur["desc"] = (cur["desc"] + " " + line).strip()

        i += 1

    return transactions


# ─── Period detection ───────────────────────────────────────────────────────

def period_key(prefix, txs):
    if not txs:
        return prefix
    months = sorted({t["date"][:7] for t in txs if t.get("date")})
    if not months:
        return prefix
    if len(months) == 1:
        return f"{prefix}_{months[0]}"
    return f"{prefix}_{months[0]}_{months[-1]}"


# ─── CSV reader (for Payoneer + LootRush card) ──────────────────────────────

def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return list(reader)


# ─── XLSX reader (for LootRush statement) ───────────────────────────────────

def read_xlsx(path):
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    headers = list(rows[0])
    out = []
    for row in rows[1:]:
        if all(c is None for c in row):
            continue
        out.append({h: v for h, v in zip(headers, row)})
    return out


# ─── Imports.json + data.js ─────────────────────────────────────────────────

def load_imports():
    if not IMPORTS_JSON.exists():
        return {}
    return json.loads(IMPORTS_JSON.read_text("utf-8"))


def save_imports(imports):
    IMPORTS_JSON.write_text(
        json.dumps(imports, ensure_ascii=False, indent=2), "utf-8"
    )


def regenerate_data_js(imports):
    accounts_dict = {}
    for entry in imports.values():
        aid = entry["accId"]
        if aid not in accounts_dict:
            accounts_dict[aid] = {
                "id": aid, "name": entry["account"],
                "currency": "USD", "transactions": [],
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
        "utf-8",
    )


# ─── Main ───────────────────────────────────────────────────────────────────

def main():
    print("→ Lendo Wise PDF...")
    wise_txs = parse_wise_pdf(WISE_PDF)
    print(f"   {len(wise_txs)} transações Wise")

    print("→ Lendo Payoneer CSV...")
    pay_txs = parse_payoneer(read_csv(PAYONNER_CSV))
    print(f"   {len(pay_txs)} transações Payoneer")

    print("→ Lendo LootRush xlsx (transferências)...")
    lr_txs = parse_lootrush_transfer(read_xlsx(LOOTRUSH_XLSX))
    print(f"   {len(lr_txs)} transações LootRush transfer")

    print("→ Lendo LootRush CSV (cartão)...")
    lr_card_txs = parse_lootrush_card(read_csv(LOOTRUSH_CARD_CSV))
    print(f"   {len(lr_card_txs)} transações LootRush card")

    imports = load_imports()
    now = datetime.now().isoformat(timespec="seconds")

    entries = [
        ("wise-llp",          "Wise LLP",                wise_txs,    WISE_PDF.name,          ""),
        ("payonner-llp",      "Payonner LPP",            pay_txs,     PAYONNER_CSV.name,      ""),
        ("lootrush-llp",      "LootRush LLP (Extrato)",  lr_txs,      LOOTRUSH_XLSX.name,     ""),
        ("lootrush-llp-card", "LootRush LLP (Cartão)",   lr_card_txs, LOOTRUSH_CARD_CSV.name, ""),
    ]
    for acc_id, name, txs, filename, suffix in entries:
        if not txs:
            print(f"   ⚠ {name}: nenhuma transação parseada — pulando")
            continue
        key = period_key(acc_id, txs) + suffix
        imports[key] = {
            "account": name, "accId": acc_id, "month": key,
            "filename": filename, "importedAt": now,
            "transactions": txs,
        }
        print(f"   ✓ {key}: {len(txs)} transações")

    save_imports(imports)
    regenerate_data_js(imports)
    print(f"\n✅ Atualizados: {IMPORTS_JSON.name} e {DATA_JS.name}")


if __name__ == "__main__":
    main()
