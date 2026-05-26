#!/usr/bin/env python3
"""
Lê todos os CSVs em /Extratos/{Banco}/**/*.csv, normaliza e gera data.js.
Roda: python3 dashboard/process.py
"""
import csv, json, os, re, sys
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).resolve().parent.parent
EXTRATOS = ROOT / "Extratos"
OUT = Path(__file__).resolve().parent / "data.js"


def parse_payoneer_row(row):
    """Normaliza uma linha do CSV da Payoneer."""
    date_raw = row.get("Transaction Date", "").strip()
    if not date_raw:
        return None
    # MM/DD/YYYY -> YYYY-MM-DD
    m, d, y = date_raw.split("/")
    iso_date = f"{y}-{m.zfill(2)}-{d.zfill(2)}"

    credit = float(row.get("Credit Amount") or 0)
    debit = float(row.get("Debit Amount") or 0)
    description = row.get("Description", "").strip()
    source = row.get("Source", "").strip()
    target = row.get("Target", "").strip()
    additional = row.get("Additional Description", "").strip()

    # Categorização
    # - Receitas (Payment from...): credit > 0, source contém o pagador
    # - Despesas (Card charge...): debit > 0, source contém categoria
    # - Estornos (Card charge... com credit > 0): refund da categoria
    is_payment = description.lower().startswith("payment from")
    is_card_charge = description.lower().startswith("card charge")

    if is_payment and credit > 0:
        tx_type = "income"
        # source: "DIGISTORE24 INC. (DIGISTORE24)" -> "Digistore24"
        category = "Receita"
        subcategory = re.sub(r"\s*\(.*?\)\s*", "", source).strip().title() or "Outros"
        merchant = subcategory
    elif is_card_charge and credit > 0:
        tx_type = "refund"
        category, subcategory = parse_category(source)
        # target: "CLKBANK*GutDrops6" -> merchant
        merchant = target or extract_merchant(description)
    elif is_card_charge and debit > 0:
        tx_type = "expense"
        category, subcategory = parse_category(source)
        merchant = target or extract_merchant(description)
    else:
        tx_type = "other"
        category, subcategory = parse_category(source) if source else ("Outros", "")
        merchant = target or ""

    return {
        "date": iso_date,
        "time": row.get("Transaction Time", "").strip(),
        "id": row.get("Transaction ID", "").strip(),
        "description": description,
        "credit": credit,
        "debit": debit,
        "currency": row.get("Currency", "USD").strip(),
        "status": row.get("Status", "").strip(),
        "runningBalance": float(row.get("Running Balance") or 0),
        "additional": additional,
        "category": category,
        "subcategory": subcategory,
        "merchant": merchant,
        "type": tx_type,
    }


def parse_category(source):
    """
    'Reco Variável - Twillio, WPP, Call Loop (USD balance)' ->
        ('Reco Variável', 'Twillio, WPP, Call Loop')
    """
    if not source:
        return ("Sem categoria", "")
    cleaned = re.sub(r"\s*\(USD balance\)\s*$", "", source).strip()
    if " - " in cleaned:
        cat, sub = cleaned.split(" - ", 1)
        return (cat.strip(), sub.strip())
    return (cleaned, "")


def extract_merchant(description):
    """'Card charge (TWILIO INC)' -> 'TWILIO INC'"""
    m = re.search(r"\((.*?)\)", description)
    return m.group(1) if m else ""


def load_account(folder):
    """Carrega todas as transações de uma pasta de conta."""
    transactions = []
    for csv_path in sorted(folder.rglob("*.csv")):
        with open(csv_path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                tx = parse_payoneer_row(row)
                if tx:
                    transactions.append(tx)
    # ordena cronologicamente
    transactions.sort(key=lambda t: (t["date"], t["time"]))
    return transactions


def main():
    if not EXTRATOS.exists():
        print(f"Pasta não encontrada: {EXTRATOS}", file=sys.stderr)
        sys.exit(1)

    accounts = []
    for account_folder in sorted(EXTRATOS.iterdir()):
        if not account_folder.is_dir():
            continue
        name = account_folder.name
        txs = load_account(account_folder)
        accounts.append({
            "id": re.sub(r"\W+", "-", name.lower()),
            "name": name,
            "currency": "USD",
            "transactions": txs,
        })
        print(f"  {name}: {len(txs)} transações")

    payload = {
        "accounts": accounts,
        "generatedAt": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
    }

    out_content = "window.financialData = " + json.dumps(payload, ensure_ascii=False, indent=2) + ";\n"
    OUT.write_text(out_content, encoding="utf-8")
    print(f"\nGerado: {OUT}")


if __name__ == "__main__":
    main()
