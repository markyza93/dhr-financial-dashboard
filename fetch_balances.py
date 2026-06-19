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

Optional:
    BULBANK_STATIC           BGN balance override (default 201356 = Jun 1 2026 statement)

After a successful run:
    balances.json            Written to cwd — commit this to the repo
    new_refresh_token.txt    Written ONLY if Revolut returned a rotated refresh token.
                             The GitHub Action reads this and calls `gh secret set` to update
                             REVOLUT_REFRESH_TOKEN before the old one expires.
"""

import json, os, sys, time, uuid
from datetime import datetime, timezone

try:
    import requests
    import jwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.backends import default_backend
except ImportError:
    print("[ERROR] Missing deps. Run: pip install requests PyJWT cryptography")
    sys.exit(1)

# ── Constants ──────────────────────────────────────────────────────────────────
MERCURY_BASE  = "https://api.mercury.com/api/v1"
REV_BASE      = "https://b2b.revolut.com/api/1.0"
REV_TOKEN_URL = REV_BASE + "/auth/token"

FX_EUR        = 1.1696   # EUR → USD  (update as needed)
FX_BGN        = 0.5979   # BGN → USD

# Bulbank (manual — update when you receive your MT940 statement)
BULBANK_STATIC = int(os.environ.get("BULBANK_STATIC", 201356))

# ── Credentials from environment ───────────────────────────────────────────────
MERCURY_KEY       = os.environ.get("MERCURY_API_KEY", "")
REV_CLIENT_ID     = os.environ.get("REVOLUT_CLIENT_ID", "")
REV_DOMAIN        = os.environ.get("REVOLUT_DOMAIN", "")
REV_PRIVATE_KEY   = os.environ.get("REVOLUT_PRIVATE_KEY", "")   # PEM content (not path)
REV_REFRESH_TOKEN = os.environ.get("REVOLUT_REFRESH_TOKEN", "")


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


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    _check_env()

    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mercury":   None,
        "revolut":   None,
        "bulbank":   {"status": "ok", "balance_bgn": BULBANK_STATIC,
                      "balance_usd": round(BULBANK_STATIC * FX_BGN, 2),
                      "note": "Manual — from MT940 statement"},
        "fx":        {"eur_usd": FX_EUR, "bgn_usd": FX_BGN},
        "errors":    [],
    }

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
              + BULBANK_STATIC * FX_BGN
          )))


if __name__ == "__main__":
    main()
