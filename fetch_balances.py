"""
fetch_balances.py  —  DHR Financial Dashboard
Standalone script for GitHub Actions (no Flask).
Reads credentials from environment variables, fetches Mercury + Revolut
balances, and writes balances.json to the current directory.

Required environment variables:
    MERCURY_API_KEY          Mercury production API key (secret-token:mercury_production_rma_...)
    REVOLUT_CLIENT_ID        Revolut Business client ID (UUID)
    REVOLUT_DOMAIN           Registered domain for Revolut JWT (e.g. dhr.is — no https://)
    REVOLUT_PRIVATE_KEY      Contents of revolut_private.pem (the RSA private key, multi-line)
    REVOLUT_REFRESH_TOKEN    Current Revolut OAuth refresh token
    BULBANK_GMAIL_USER       Gmail address that receives the daily "Bulbank Online Report"
                             email from pb@unicreditgroup.bg (App Password login)
    BULBANK_GMAIL_APP_PASSWORD   16-char Google App Password for that Gmail account
                             (Google Account → Security → 2-Step Verification → App passwords)

Optional:
    BULBANK_STATIC           Last-resort USD fallback if no Bulbank email can be read at all
                              (default 54663 = Jun 30 2026 closing, from the dashboard)

Bulbank balances come from the MT940 statement attached to that daily email — see
bulbank_mt940.py (requires `pip install mt-940`). If the email fetch/parse fails for
any reason, the script falls back to the previous run's committed balances.json
values first, and only to BULBANK_STATIC if there is no prior data at all — so a
transient Gmail hiccup never blanks out the Bulbank card.

After a successful run:
    balances.json            Written to cwd — commit this to the repo
    new_refresh_token.txt    Written ONLY if Revolut returned a rotated refresh token.
                             The GitHub Action reads this and calls `gh secret set` to update
                             REVOLUT_REFRESH_TOKEN before the old one expires.
"""

import imaplib, email, json, os, sys, time, uuid
from datetime import datetime, timezone, timedelta

# ── Optional: Plaid (Chase) ────────────────────────────────────────────────────
PLAID_CLIENT_ID    = os.environ.get("PLAID_CLIENT_ID", "")
PLAID_SECRET       = os.environ.get("PLAID_SECRET", "")
PLAID_ACCESS_TOKEN = os.environ.get("PLAID_CHASE_ACCESS_TOKEN", "")

try:
    import requests
    import jwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.backends import default_backend
except ImportError:
    print("[ERROR] Missing deps. Run: pip install requests PyJWT cryptography")
    sys.exit(1)

try:
    from bulbank_mt940 import parse_bulbank_statement
except ImportError:
    parse_bulbank_statement = None  # handled at call time — pip install mt-940

# ── Constants ──────────────────────────────────────────────────────────────────
MERCURY_BASE  = "https://api.mercury.com/api/v1"
REV_BASE      = "https://b2b.revolut.com/api/1.0"
REV_TOKEN_URL = REV_BASE + "/auth/token"

FX_EUR        = 1.1696   # EUR → USD  (update as needed)
FX_BGN        = 0.5979   # BGN → USD

# Bulbank — last-resort fallback only, used if no email AND no prior balances.json exist
BULBANK_STATIC = int(os.environ.get("BULBANK_STATIC", 54663))

BULBANK_SENDER      = "pb@unicreditgroup.bg"
BULBANK_IMAP_HOST   = "imap.gmail.com"

# ── Credentials from environment ───────────────────────────────────────────────
MERCURY_KEY       = os.environ.get("MERCURY_API_KEY", "")
REV_CLIENT_ID     = os.environ.get("REVOLUT_CLIENT_ID", "")
REV_DOMAIN        = os.environ.get("REVOLUT_DOMAIN", "")
REV_PRIVATE_KEY   = os.environ.get("REVOLUT_PRIVATE_KEY", "")   # PEM content (not path)
REV_REFRESH_TOKEN = os.environ.get("REVOLUT_REFRESH_TOKEN", "")
BULBANK_GMAIL_USER     = os.environ.get("BULBANK_GMAIL_USER", "")
BULBANK_GMAIL_PASSWORD = os.environ.get("BULBANK_GMAIL_APP_PASSWORD", "")


def _check_env():
    missing = [k for k, v in {
        "MERCURY_API_KEY":       MERCURY_KEY,
        "REVOLUT_CLIENT_ID":     REV_CLIENT_ID,
        "REVOLUT_DOMAIN":        REV_DOMAIN,
        "REVOLUT_PRIVATE_KEY":   REV_PRIVATE_KEY,
        "REVOLUT_REFRESH_TOKEN": REV_REFRESH_TOKEN,
    }.items() if not v]
    if missing:
        print("[ERROR] Missing environment variables: " + ", ".join(missing))
        sys.exit(1)


# ── Revolut JWT / token helpers ────────────────────────────────────────────────
def _privkey():
    key_bytes = REV_PRIVATE_KEY.encode()
    # GitHub Secrets collapse newlines to literal \n — fix if needed
    if b"\\n" in key_bytes:
        key_bytes = key_bytes.replace(b"\\n", b"\n")
    return serialization.load_pem_private_key(key_bytes, password=None, backend=default_backend())


def _make_jwt():
    now = int(time.time())
    return jwt.encode(
        {
            "iss": REV_DOMAIN,
            "sub": REV_CLIENT_ID,
            "aud": "https://revolut.com",
            "exp": now + 2400,
            "iat": now,
            "jti": str(uuid.uuid4()),
        },
        _privkey(),
        algorithm="RS256",
    )


def _refresh_access_token(refresh_token):
    """Exchange refresh_token for a new access_token (and possibly a new refresh_token)."""
    r = requests.post(
        REV_TOKEN_URL,
        data={
            "grant_type":            "refresh_token",
            "refresh_token":         refresh_token,
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion":      _make_jwt(),
        },
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


# ── Chase via Plaid ───────────────────────────────────────────────────────────
def fetch_chase_plaid():
    """Fetch JPMorgan Chase balance via Plaid (Production)."""
    try:
        import plaid
        from plaid.api import plaid_api
        from plaid.api_client import ApiClient
        from plaid.configuration import Configuration
        from plaid.model.accounts_balance_get_request import AccountsBalanceGetRequest
    except ImportError:
        raise RuntimeError("plaid-python not installed")

    if not all([PLAID_CLIENT_ID, PLAID_SECRET, PLAID_ACCESS_TOKEN]):
        raise RuntimeError("Missing PLAID_CLIENT_ID / PLAID_SECRET / PLAID_CHASE_ACCESS_TOKEN")

    configuration = Configuration(
        host=plaid.Environment.Production,
        api_key={"clientId": PLAID_CLIENT_ID, "secret": PLAID_SECRET},
    )
    with ApiClient(configuration) as api_client:
        client = plaid_api.PlaidApi(api_client)
        resp = client.accounts_balance_get(
            AccountsBalanceGetRequest(access_token=PLAID_ACCESS_TOKEN)
        )

    accounts = []
    total = 0.0
    for acct in resp["accounts"]:
        bal = acct["balances"]["available"]
        if bal is None:
            bal = acct["balances"]["current"] or 0
        accounts.append({
            "name":      acct["name"],
            "mask":      acct["mask"],
            "type":      str(acct["type"]),
            "subtype":   str(acct["subtype"]),
            "available": acct["balances"]["available"],
            "current":   acct["balances"]["current"],
        })
        if str(acct["type"]) == "depository":
            total += bal

    print("[Chase/Plaid] OK — " + str(len(accounts)) + " accounts | total $" + str(round(total)))
    return {"status": "ok", "total": round(total, 2), "accounts": accounts}


# ── Mercury ────────────────────────────────────────────────────────────────────
def fetch_mercury():
    hdrs = {
        "Authorization": "Bearer " + MERCURY_KEY,
        "Content-Type":  "application/json",
    }
    raw = requests.get(MERCURY_BASE + "/accounts", headers=hdrs, timeout=10)
    raw.raise_for_status()

    accounts = []
    for acct in raw.json().get("accounts", []):
        aid = acct["id"]
        txr = requests.get(
            MERCURY_BASE + "/account/" + aid + "/transactions",
            headers=hdrs,
            params={"limit": 20, "status": "sent"},
            timeout=10,
        )
        txr.raise_for_status()
        txs = [
            {
                "date":        t.get("postedAt") or t.get("createdAt", ""),
                "description": t.get("bankDescription") or t.get("externalMemo", ""),
                "amount":      t.get("amount", 0),
                "kind":        t.get("kind", ""),
            }
            for t in txr.json().get("transactions", [])
        ]
        accounts.append(
            {
                "id":               aid,
                "name":             acct.get("name", ""),
                "accountNumber":    acct.get("accountNumber", ""),
                "currentBalance":   acct.get("currentBalance", 0),
                "availableBalance": acct.get("availableBalance", 0),
                "currency":         acct.get("currencyCode", "USD"),
                "type":             acct.get("type", ""),
                "status":           acct.get("status", ""),
                "mtdInflows":       round(sum(t["amount"] for t in txs if t["amount"] > 0), 2),
                "mtdOutflows":      round(sum(t["amount"] for t in txs if t["amount"] < 0), 2),
                "recentTransactions": txs,
            }
        )
    print("[Mercury] OK — " + str(len(accounts)) + " accounts")
    return {"status": "ok", "accounts": accounts}


# ── Revolut ────────────────────────────────────────────────────────────────────
def fetch_revolut():
    """Returns (revolut_data_dict, new_refresh_token_or_None)."""
    token_data = _refresh_access_token(REV_REFRESH_TOKEN)
    access_token  = token_data["access_token"]
    new_rt        = token_data.get("refresh_token")   # may or may not be present

    if new_rt and new_rt != REV_REFRESH_TOKEN:
        print("[Revolut] Refresh token rotated — will write new_refresh_token.txt")
    else:
        new_rt = None  # not rotated, no action needed

    print("[Revolut] Token refreshed")

    hdrs = {"Authorization": "Bearer " + access_token}
    resp = requests.get(REV_BASE + "/accounts", headers=hdrs, timeout=10)
    resp.raise_for_status()

    accounts = []
    totals   = {"usd": 0.0, "eur": 0.0, "bgn": 0.0}

    for acct in resp.json():
        currency = acct.get("currency", "").upper()
        balance  = acct.get("balance", 0)
        accounts.append(
            {
                "id":       acct.get("id", ""),
                "name":     acct.get("name", ""),
                "currency": currency,
                "balance":  balance,
                "available": balance,
                "state":    acct.get("state", ""),
            }
        )
        if currency == "USD":
            totals["usd"] += balance
        elif currency == "EUR":
            totals["eur"] += balance
        elif currency == "BGN":
            totals["bgn"] += balance

    total_usd = round(
        totals["usd"]
        + totals["eur"] * FX_EUR
        + totals["bgn"] * FX_BGN,
        2,
    )

    print(
        "[Revolut] OK — "
        + str(len(accounts))
        + " accounts | USD "
        + str(round(totals["usd"]))
        + " EUR "
        + str(round(totals["eur"]))
        + " BGN "
        + str(round(totals["bgn"]))
        + " → total $"
        + str(round(total_usd))
    )

    data = {
        "status":   "ok",
        "total":    total_usd,
        "usd":      round(totals["usd"], 2),
        "eur":      round(totals["eur"], 2),
        "bgn":      round(totals["bgn"], 2),
        "accounts": accounts,
    }
    return data, new_rt


# ── Bulbank (via daily email) ───────────────────────────────────────────────────
BULBANK_LABELS = {
    # IBAN → friendly short label shown on the dashboard.
    # Extend this if/when Bulbank adds or closes an account.
    "BG86UNCR70001526136673": "BG86 — Operating",
    "BG88UNCR70001526079989": "BG88",
    "BG16UNCR70001526136672": "BG16 — Reserve",
}


def _fetch_latest_bulbank_attachment():
    """
    Logs into Gmail via IMAP (App Password auth) and returns the raw text of the
    MT940 attachment from the most recent "Bulbank Online Report" email.
    Returns (raw_text, message_date_iso). Raises on any failure.
    """
    if not BULBANK_GMAIL_USER or not BULBANK_GMAIL_PASSWORD:
        raise RuntimeError("Missing BULBANK_GMAIL_USER / BULBANK_GMAIL_APP_PASSWORD")

    imap = imaplib.IMAP4_SSL(BULBANK_IMAP_HOST)
    try:
        imap.login(BULBANK_GMAIL_USER, BULBANK_GMAIL_PASSWORD)
        imap.select("INBOX")

        # Only look a few days back — we just need the most recent report.
        since = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%d-%b-%Y")
        status, msg_ids = imap.search(None, f'(FROM "{BULBANK_SENDER}" SINCE {since})')
        if status != "OK" or not msg_ids or not msg_ids[0]:
            raise RuntimeError(f"No email from {BULBANK_SENDER} in the last 5 days")

        # IMAP SEARCH returns ids in ascending order — take the newest.
        latest_id = msg_ids[0].split()[-1]
        status, msg_data = imap.fetch(latest_id, "(RFC822)")
        if status != "OK" or not msg_data or not msg_data[0]:
            raise RuntimeError("Failed to fetch latest Bulbank email")

        msg = email.message_from_bytes(msg_data[0][1])
        msg_date = msg.get("Date", "")

        attachment_text = None
        for part in msg.walk():
            filename = part.get_filename()
            if not filename:
                continue  # skip the inline body — only take real attachments
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            try:
                attachment_text = payload.decode("utf-8")
            except UnicodeDecodeError:
                attachment_text = payload.decode("cp1251", errors="replace")
            break

        if not attachment_text:
            raise RuntimeError("Bulbank email had no readable attachment")

        return attachment_text, msg_date
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def fetch_bulbank_email():
    """
    Fetches + parses today's (or most recent) Bulbank MT940 email into the same
    shape used elsewhere in this script. Currency is EUR for every account.
    """
    if parse_bulbank_statement is None:
        raise RuntimeError("mt-940 not installed — run: pip install mt-940")

    raw_text, msg_date = _fetch_latest_bulbank_attachment()
    accounts = parse_bulbank_statement(raw_text)
    if not accounts:
        raise RuntimeError("MT940 attachment parsed but contained no accounts")

    total_eur = round(sum(a["closing_balance"] for a in accounts if a["currency"] == "EUR"), 2)
    other_currency_accounts = [a for a in accounts if a["currency"] != "EUR"]
    if other_currency_accounts:
        # Shouldn't normally happen post euro-adoption, but don't silently drop money.
        print("[WARN] Bulbank accounts in non-EUR currency: " + str(other_currency_accounts))

    total_usd = round(total_eur * FX_EUR, 2)
    statement_date = max((a["value_date"] for a in accounts if a["value_date"]), default=None)

    out_accounts = []
    for a in accounts:
        out_accounts.append({
            "iban":            a["iban"],
            "label":           BULBANK_LABELS.get(a["iban"], a["iban"][-4:]),
            "currency":        a["currency"],
            "closing_balance": a["closing_balance"],
            "value_date":      a["value_date"],
        })

    print(
        "[Bulbank] OK — " + str(len(accounts)) + " accounts | total €"
        + str(round(total_eur)) + " → $" + str(round(total_usd))
        + " | statement date " + str(statement_date)
        + " | email date " + msg_date
    )

    return {
        "status":         "ok",
        "source":         "email-mt940",
        "statement_date": statement_date,
        "total_eur":      total_eur,
        "total_usd":      total_usd,
        "accounts":       out_accounts,
    }


# ── Main ───────────────────────────────────────────────────────────────────────
def _load_previous_bulbank():
    """Reads the bulbank block from the balances.json committed by the PREVIOUS run,
    so a transient email/parse failure degrades to yesterday's real balance instead
    of a hardcoded placeholder."""
    prev_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "balances.json")
    try:
        with open(prev_path) as f:
            prev = json.load(f)
        prev_bulbank = prev.get("bulbank")
        if prev_bulbank and prev_bulbank.get("status") == "ok":
            return prev_bulbank
    except Exception:
        pass
    return None


def main():
    _check_env()

    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mercury":   None,
        "revolut":   None,
        "chase":     None,
        "bulbank":   None,
        "fx":        {"eur_usd": FX_EUR, "bgn_usd": FX_BGN},
        "errors":    [],
    }

    # Bulbank (daily email — MT940 attachment)
    try:
        result["bulbank"] = fetch_bulbank_email()
    except Exception as e:
        msg = "Bulbank: " + str(e)
        result["errors"].append(msg)
        print("[ERROR] " + msg)

        prev_bulbank = _load_previous_bulbank()
        if prev_bulbank:
            result["bulbank"] = dict(prev_bulbank, status="stale", note=(
                "Live email fetch failed this run — showing last known balance "
                f"({prev_bulbank.get('statement_date', '?')})"
            ))
            print("[Bulbank] Falling back to previous run's balance")
        else:
            result["bulbank"] = {
                "status": "fallback", "source": "static",
                "total_usd": BULBANK_STATIC,
                "total_eur": round(BULBANK_STATIC / FX_EUR, 2),
                "accounts": [],
                "note": "No email reachable and no prior balances.json — using BULBANK_STATIC placeholder",
            }
            print("[Bulbank] No prior data either — using BULBANK_STATIC placeholder")

    # Mercury
    try:
        result["mercury"] = fetch_mercury()
    except Exception as e:
        msg = "Mercury: " + str(e)
        result["errors"].append(msg)
        result["mercury"] = {"status": "error", "message": msg}
        print("[ERROR] " + msg)

    # Revolut
    new_refresh_token = None
    try:
        result["revolut"], new_refresh_token = fetch_revolut()
    except Exception as e:
        msg = "Revolut: " + str(e)
        result["errors"].append(msg)
        result["revolut"] = {"status": "error", "message": msg}
        print("[ERROR] " + msg)

    # Chase via Plaid (optional — only runs if secrets are set)
    if PLAID_CLIENT_ID and PLAID_SECRET and PLAID_ACCESS_TOKEN:
        try:
            result["chase"] = fetch_chase_plaid()
        except Exception as e:
            msg = "Chase/Plaid: " + str(e)
            result["errors"].append(msg)
            result["chase"] = {"status": "error", "message": msg}
            print("[ERROR] " + msg)
    else:
        print("[Chase/Plaid] Skipped — PLAID_* secrets not set")
        result["chase"] = {"status": "pending", "message": "Plaid secrets not configured yet"}

    # Write balances.json
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "balances.json")
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)
    print("[OK] balances.json written")

    # Write new refresh token for the GitHub Action to pick up
    if new_refresh_token:
        tok_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "new_refresh_token.txt")
        with open(tok_path, "w") as f:
            f.write(new_refresh_token)
        print("[OK] new_refresh_token.txt written (GitHub Action will update secret)")

    # Exit non-zero if any errors so the Action shows a failure
    if result["errors"]:
        print("[WARN] Completed with errors: " + str(result["errors"]))
        sys.exit(1)

    print("[OK] All done. Grand total: $"
          + str(round(
              (result["mercury"]["accounts"][0]["currentBalance"]
               if result["mercury"] and result["mercury"].get("accounts") else 0)
              + (result["revolut"]["total"] if result["revolut"] else 0)
              + (result["bulbank"]["total_usd"] if result["bulbank"] else 0)
          )))


if __name__ == "__main__":
    main()
