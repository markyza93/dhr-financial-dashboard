"""
bulbank_mt940.py — DHR Financial Dashboard

Parses the daily "Bulbank Online Report" email attachment sent by
pb@unicreditgroup.bg. The attachment is a plain-text SWIFT MT940 file
containing ONE statement block per account, back-to-back, each block
terminated by a line containing only "-".

Requires: pip install mt-940

Usage:
    from bulbank_mt940 import parse_bulbank_statement
    accounts = parse_bulbank_statement(raw_text)
    # accounts == [
    #   {
    #     "iban": "BG86UNCR70001526136673",
    #     "statement_number": "8",
    #     "currency": "EUR",
    #     "opening_balance": 80491.00,
    #     "closing_balance": 80209.60,
    #     "available_balance": 80209.60,
    #     "value_date": "2026-08-07",
    #     "transactions": [
    #        {"amount": -0.41, "currency": "EUR", "date": "2026-08-07",
    #         "details": "CHH+00Такса за вътрешнобанков превод+30+31+32",
    #         "bank_reference": "963FTRO26219AKGI"},
    #        ...
    #     ],
    #   },
    #   ...
    # ]
"""

import mt940


def _split_statement_blocks(raw_text):
    """
    Bulbank's export concatenates multiple MT940 statements (one per account)
    in a single file, each one terminated by a lone "-" line. The `mt-940`
    library's parser only expects a single statement per input, so we split
    on that delimiter before parsing each block individually.
    """
    blocks = []
    current = []
    for line in raw_text.splitlines():
        if line.strip() == "-":
            if current:
                blocks.append("\n".join(current))
                current = []
        else:
            current.append(line)
    if current and "".join(current).strip():
        blocks.append("\n".join(current))
    return [b for b in blocks if b.strip()]


def _balance_dict(bal):
    if bal is None:
        return None
    return {
        "amount": float(bal.amount.amount),
        "currency": str(bal.amount.currency),
        "date": bal.date.isoformat() if bal.date else None,
    }


def parse_bulbank_statement(raw_text):
    """Parse a raw multi-account Bulbank MT940 export into a list of account dicts."""
    accounts = []
    for block in _split_statement_blocks(raw_text):
        txns = mt940.parse(block)
        d = txns.data

        iban = d.get("account_identification")
        if not iban:
            # Skip malformed/empty blocks rather than failing the whole file
            continue

        opening = _balance_dict(d.get("final_opening_balance"))
        closing = _balance_dict(d.get("final_closing_balance"))
        available = _balance_dict(d.get("available_balance"))

        transactions = []
        for t in txns:
            td = t.data
            amt = td.get("amount")
            transactions.append({
                "amount": float(amt.amount) if amt is not None else None,
                "currency": str(amt.currency) if amt is not None else None,
                "date": td["date"].isoformat() if td.get("date") else None,
                "details": (td.get("transaction_details") or "").strip(),
                "bank_reference": td.get("bank_reference", ""),
            })

        accounts.append({
            "iban": iban,
            "statement_number": d.get("statement_number"),
            "currency": closing["currency"] if closing else (opening["currency"] if opening else None),
            "opening_balance": opening["amount"] if opening else None,
            "closing_balance": closing["amount"] if closing else None,
            "available_balance": available["amount"] if available else (closing["amount"] if closing else None),
            "value_date": closing["date"] if closing else (opening["date"] if opening else None),
            "transactions": transactions,
        })

    return accounts


if __name__ == "__main__":
    import sys, json
    path = sys.argv[1] if len(sys.argv) > 1 else "sample.sta"
    with open(path, encoding="utf-8") as f:
        text = f.read()
    result = parse_bulbank_statement(text)
    print(json.dumps(result, indent=2, ensure_ascii=False))
