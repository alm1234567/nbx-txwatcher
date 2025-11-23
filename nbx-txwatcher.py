#!/usr/bin/env python3
"""
nbx-txwatcher.py

Bitcoin transaction watcher for RaspiBlitz using NBXplorer.

- Registers single-sig wallet derivations (xpub) with NBX
- Uses existing derivations (e.g. BTCPay-managed multisig)
- Streams NBX events and sends plain-text email notifications
- Optional PGP encryption (inline ASCII armored text)

Config: /mnt/hdd/app-data/nbx-txwatcher/nbx-txwatcher.conf
Run as: btcpay user
"""

import configparser
import time
import urllib.parse
import requests
import smtplib
import subprocess
from datetime import datetime, timedelta, timezone

# Keep track of (derivationStrategy, txid) we've already notified on
seen_txs = set()
# Keep track of derivation strategies (wallets) that have already had at least one tx
wallets_seen_once = set()

CONFIG_PATH = "/mnt/hdd/app-data/nbx-txwatcher/nbx-txwatcher.conf"

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def load_config():
    config = configparser.ConfigParser()
    config.read(CONFIG_PATH)
    return config

def get_global(config, key, fallback=None):
    return config.get("global", key, fallback=fallback)

def iter_wallets(config):
    for section in config.sections():
        if section.startswith("wallet "):
            yield section, config[section]

# ---------------------------------------------------------------------------
# NBX helpers
# ---------------------------------------------------------------------------

def register_derivation(nbx_url, nbx_user, nbx_pass, derivation):
    encoded = urllib.parse.quote(derivation, safe='')
    url = f"{nbx_url}/v1/cryptos/BTC/derivations/{encoded}"
    try:
        resp = requests.post(url, auth=(nbx_user, nbx_pass), timeout=10)
        if resp.status_code in (200, 201):
            return True, resp.json() if resp.content else {}
        elif resp.status_code == 409:
            return True, {"status": "already_registered"}
        else:
            return False, {"status": resp.status_code, "body": resp.text}
    except Exception as e:
        return False, {"error": str(e)}

def stream_events(nbx_url, nbx_user, nbx_pass, last_event_id=0):
    url = f"{nbx_url}/v1/cryptos/BTC/events"
    params = {"lastEventId": last_event_id, "longPolling": 20}
    while True:
        try:
            resp = requests.get(
                url,
                auth=(nbx_user, nbx_pass),
                params=params,
                timeout=30,
            )
            resp.raise_for_status()
            events = resp.json()
            if not events:
                continue

            for ev in events:
                yield ev
                if "eventId" in ev:
                    params["lastEventId"] = ev["eventId"]
        except Exception as e:
            print(f"[events] Error: {e}. Sleeping 10s...")
            time.sleep(10)

def get_wallet_balance_sats(nbx_url, nbx_user, nbx_pass, derivation):
    """
    Query NBXplorer for the wallet's confirmed balance in sats.

    Tries /summary first. If 404, falls back to /balance.
    """
    encoded = urllib.parse.quote(derivation, safe='')

    # First try /summary
    url_summary = f"{nbx_url}/v1/cryptos/BTC/derivations/{encoded}/summary"
    try:
        resp = requests.get(url_summary, auth=(nbx_user, nbx_pass), timeout=10)
        if resp.status_code == 404:
            # Fall back to /balance
            url_balance = f"{nbx_url}/v1/cryptos/BTC/derivations/{encoded}/balance"
            resp2 = requests.get(url_balance, auth=(nbx_user, nbx_pass), timeout=10)
            resp2.raise_for_status()
            data2 = resp2.json()
            # Old-style /balance response typically has "confirmed" or "unconfirmed"
            if "confirmedBalance" in data2:
                return int(data2.get("confirmedBalance", 0))
            elif "confirmed" in data2:
                return int(data2.get("confirmed", 0))
            else:
                return 0
        else:
            resp.raise_for_status()
            data = resp.json()
            return int(data.get("confirmedBalance", 0))
    except Exception as e:
        print(f"[balance] Error fetching balance for derivation: {e}")
        return 0

def is_first_seen_unconfirmed_tx(ev):
    """
    Return True only for the initial broadcast (0 confirmations).
    NBX puts confirmations under data["transactionData"]["confirmations"].
    """
    data = ev.get("data", {}) or {}
    txd = data.get("transactionData", {}) or {}
    confs = txd.get("confirmations")
    # If field missing, treat as 0 (mempool)
    if confs is None:
        return True
    return int(confs) == 0

# ---------------------------------------------------------------------------
# Amount / direction helpers (sats -> BTC)
# ---------------------------------------------------------------------------

def sats_to_btc(sats):
    return sats / 100_000_000

def format_btc(amount_btc):
    return f"{amount_btc:.8f}"


def infer_direction_and_amount_sats(ev):
    """
    Compute the net change for *this wallet* from NBX event.

    - inputs: amounts leaving the wallet
    - outputs: amounts entering the wallet (change, pay-to-self, etc.)

    net_delta_sats = sum(outputs) - sum(inputs)

    If net_delta_sats < 0:
        Direction: Outbound (wallet lost coins)
        Amount: abs(net_delta_sats)
    If net_delta_sats > 0:
        Direction: Inbound (wallet gained coins)
        Amount: net_delta_sats
    If net_delta_sats == 0:
        Direction: Internal/Unknown, Amount: 0
    """
    data = ev.get("data", {}) or {}
    inputs = data.get("inputs", []) or []
    outputs = data.get("outputs", []) or []

    sum_inputs = sum(int(i.get("value", 0)) for i in inputs)
    sum_outputs = sum(int(o.get("value", 0)) for o in outputs)

    net_delta = sum_outputs - sum_inputs  # positive if wallet gains, negative if loses

    if net_delta > 0:
        direction = "Inbound"
        amount_sats = net_delta
    elif net_delta < 0:
        direction = "Outbound"
        amount_sats = -net_delta
    else:
        direction = "Internal"
        amount_sats = 0

    return direction, amount_sats

# ---------------------------------------------------------------------------
# Time stamp Helper
# ---------------------------------------------------------------------------

def parse_nbx_timestamp(ts_str):
    """
    Parse NBX timestamp (ISO 8601 / RFC3339-like) into a timezone-aware UTC datetime.
    NBX typically uses e.g. "2025-11-21T17:59:30.123Z" or "2025-11-21T17:59:30Z".
    """
    if not ts_str:
        return None
    try:
        # Strip trailing Z and parse
        if ts_str.endswith("Z"):
            ts_str = ts_str[:-1]
        # Try with microseconds
        try:
            dt = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%S.%f")
        except ValueError:
            dt = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%S")
        return dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def get_event_utc_datetime(event_data):
    """
    Try to get a UTC datetime for when NBX first saw the tx.
    Checks a few likely fields in event_data.
    """
    ts_str = (
        event_data.get("timestamp")
        or event_data.get("seenAt")
        or event_data.get("firstSeen")
    )
    dt_utc = parse_nbx_timestamp(ts_str)
    if dt_utc is not None:
        return dt_utc

    # Fallback: no timestamp from NBX; use current UTC
    return datetime.now(timezone.utc)


def format_dates_for_email(config, dt_utc):
    """
    Returns two strings formatted as:
    - UTC:   22/Nov/25 23:45:15
    - Local: 22/Nov/25 20:45:15  (label from config)
    """
    offset_hours = config.getfloat("global", "timezone_offset_hours", fallback=0.0)
    tz_label = config.get("global", "timezone_label", fallback="Local")

    offset = timedelta(hours=offset_hours)
    dt_local = dt_utc + offset

    fmt = "%d/%b/%y %H:%M:%S"  # 22/Nov/25 23:45:15

    utc_str = dt_utc.strftime(fmt)
    local_str = dt_local.strftime(fmt)

    return utc_str, local_str, tz_label

# ---------------------------------------------------------------------------
# PGP + Email
# ---------------------------------------------------------------------------

import smtplib
import subprocess

def pgp_encrypt_if_enabled(config, plaintext):
    """
    If [global] pgp_enabled = true, encrypts plaintext with pgp_recipient
    and returns (encrypted_ascii_armor, True).
    Otherwise returns (plaintext, False).
    """
    enabled = config.getboolean("global", "pgp_enabled", fallback=False)
    if not enabled:
        return plaintext, False

    pgp_recipient = config.get("global", "pgp_recipient", fallback=None)
    if not pgp_recipient:
        print("[pgp] pgp_enabled=true but pgp_recipient is empty; sending unencrypted.")
        return plaintext, False

    try:
        proc = subprocess.run(
            ["gpg", "--batch", "--yes", "--encrypt", "--armor", "-r", pgp_recipient],
            input=plaintext.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        encrypted = proc.stdout.decode("utf-8", errors="ignore")
        return encrypted, True
    except subprocess.CalledProcessError as e:
        print(f"[pgp] Encryption failed: {e.stderr.decode('utf-8', errors='ignore')}")
        return plaintext, False
    except FileNotFoundError:
        print("[pgp] gpg binary not found; sending unencrypted.")
        return plaintext, False


def send_email(config, subject, body_text):
    """
    Sends a plain-text email using the exact raw message format that worked with ProtonMail PGP.
    If PGP is enabled, body_text is replaced by ASCII-armored GPG output.
    """
    smtp_server = config.get("global", "smtp_server", fallback=None)
    smtp_port   = config.getint("global", "smtp_port", fallback=587)
    smtp_user   = config.get("global", "smtp_user", fallback=None)
    smtp_pass   = config.get("global", "smtp_pass", fallback=None)
    mail_from   = config.get("global", "mail_from", fallback=None)
    mail_to     = config.get("global", "mail_to", fallback=None)

    if not (smtp_server and smtp_user and smtp_pass and mail_from and mail_to):
        print("[email] Missing SMTP or mail_from/mail_to configuration; cannot send email.")
        return

    final_body, is_encrypted = pgp_encrypt_if_enabled(config, body_text)

    raw_msg = f"""From: {mail_from}
To: {mail_to}
Subject: {subject}
MIME-Version: 1.0
Content-Type: text/plain; charset=utf-8
Content-Transfer-Encoding: 7bit

{final_body}
"""

    try:
        with smtplib.SMTP(smtp_server, smtp_port, timeout=20) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(mail_from, [mail_to], raw_msg.encode("utf-8"))
        print(f"[email] Sent to {mail_to}: {subject} (encrypted={is_encrypted})")
    except Exception as e:
        print(f"[email] Error sending email: {e}")

# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------

def format_tx_message(wallet_name, direction, amount_sats,
                      local_explorer, public_explorer, txid,
                      ending_balance_sats, utc_str, local_str, tz_label,
                      note=None):
    """
    Subject:
      [Wallet Name] Transaction in Monitored Wallet

    Body (monospace-friendly):

    ----------------------------------
    Wallet:       Wallet Name
    Direction:    Inbound
    Date (UTC):   22/Nov/25 23:45:15
    Date (GMT-3): 22/Nov/25 20:45:15
    ----------------------------------
    Original:     0.00024551 BTC
    Transaction: +0.00010000 BTC
    Balance:      0.00034551 BTC
    ----------------------------------
    https://10.10.1.10:4081/tx/<txid>
    https://mempool.space/tx/<txid>
    """

    amount_btc = sats_to_btc(amount_sats)
    ending_btc = sats_to_btc(ending_balance_sats)

    # Infer original balance from ending balance +/- tx amount
    if direction == "Inbound":
        orig_btc = ending_btc - amount_btc
        sign = "+"
    elif direction == "Outbound":
        orig_btc = ending_btc + amount_btc
        sign = "-"
    else:
        orig_btc = ending_btc
        sign = " "

    # Header block
    lines = []
    lines.append("----------------------------------")
    lines.append(f"Wallet:       {wallet_name}")
    lines.append(f"Direction:    {direction}")
    lines.append(f"Date (UTC):   {utc_str}")
    lines.append(f"Date ({tz_label}): {local_str}")
    lines.append("----------------------------------")

    # Amounts block
    lines.append(f"Original:     {format_btc(orig_btc)} BTC")
    lines.append(f"Transaction: {sign}{format_btc(amount_btc)} BTC")
    lines.append(f"Balance:      {format_btc(ending_btc)} BTC")
    lines.append("----------------------------------")

    # URLs
    if local_explorer:
        base = local_explorer.rstrip("/")
        lines.append(f"{base}/tx/{txid}")
    if public_explorer:
        base = public_explorer.rstrip("/")
        lines.append(f"{base}/tx/{txid}")

    # Note to warn about potential incorrect balance if first ever seen transaction
    if note:
        lines.append("")
        lines.append(note)

    return "\n".join(lines)


    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main watcher
# ---------------------------------------------------------------------------

def main():
    global seen_txs
    print("=== nbx-txwatcher ===")

    print("1. Loading config...")
    config = load_config()
    print(f"   Config loaded from {CONFIG_PATH}")

    nbx_url  = get_global(config, "nbx_url")
    cookiefile = get_global(config, "nbx_cookiefile", fallback="")
    if cookiefile:
        try:
            with open(cookiefile, "r", encoding="utf-8") as f:
                line = f.read().strip()
            # e.g. "__cookie__:67611..."
            if ":" in line:
                nbx_user, nbx_pass = line.split(":", 1)
            else:
                nbx_user = "__cookie__"
                nbx_pass = line
        except Exception as e:
            raise RuntimeError(f"Failed to read NBX cookiefile {cookiefile}: {e}")
    else:
        nbx_user = get_global(config, "nbx_user")
        nbx_pass = get_global(config, "nbx_pass")

    local_explorer  = get_global(config, "local_explorer_url", fallback="")
    public_explorer = get_global(config, "explorer_url", fallback="")

    # 2. Register derivations (single-sig only)
    print("2. Registering derivations...")
    for section, wcfg in iter_wallets(config):
        name = wcfg.get("name", section)
        xpub = wcfg.get("xpub", "").strip()
        derivation_fixed = wcfg.get("derivation", "").strip()

        if derivation_fixed:
            print(f"   {section} ({name}): using existing derivation (BTCPay-managed), not registering.")
            continue

        if xpub:
            derivation_string = xpub
            print(f"   {section} ({name}): registering single-sig xpub {derivation_string[:80]}...")
        else:
            print(f"   {section} ({name}): Missing 'xpub' or 'derivation', skipping.")
            continue

        ok, info = register_derivation(nbx_url, nbx_user, nbx_pass, derivation_string)
        if ok:
            print("      ✓ registered (or already registered)")
        else:
            print(f"      ✗ failed: {info}")

    # 3. Build derivation -> wallet name map
    deriv_to_wallet = {}
    for section, wcfg in iter_wallets(config):
        name = wcfg.get("name", section)
        xpub = wcfg.get("xpub", "").strip()
        derivation_fixed = wcfg.get("derivation", "").strip()

        if derivation_fixed:
            deriv_to_wallet[derivation_fixed] = name
        elif xpub:
            deriv_to_wallet[xpub] = name

    print("\n3. Streaming events from NBX... (Ctrl+C to stop)\n")

    try:
        for ev in stream_events(nbx_url, nbx_user, nbx_pass):
            etype = ev.get("type")


            if etype == "newtransaction":
                # Only handle the initial broadcast (0 confirmations)
                if not is_first_seen_unconfirmed_tx(ev):
                    # Uncomment if you want to see when confirmations are ignored:
                    print(f"[tx] id={ev.get('eventId')} (confirmed update, ignored)")
                    continue

                data = ev.get("data", {})
                deriv = data.get("derivationStrategy", "")
                wallet_name = deriv_to_wallet.get(deriv, "UNKNOWN WALLET")
                txid = data.get("transactionData", {}).get("transactionHash", "")

                # Dedupe: skip if we've already notified this wallet+txid
                key = (deriv, txid)
                if key in seen_txs:
                    print(f"[tx] id={ev.get('eventId')} wallet={wallet_name} txid={txid} (duplicate, skipped)")
                    continue
                seen_txs.add(key)

                direction, amount_sats = infer_direction_and_amount_sats(ev)

                # Real wallet ending balance
                ending_balance_sats = get_wallet_balance_sats(nbx_url, nbx_user, nbx_pass, deriv)

                # Timestamp handling
                dt_utc = get_event_utc_datetime(data)
                utc_str, local_str, tz_label = format_dates_for_email(config, dt_utc)


                # Determine whether this is the first transaction we observe for this wallet
                note = None
                if deriv not in wallets_seen_once:
                    wallets_seen_once.add(deriv)
                    note = (
                        "Note: This is the first transaction observed for this wallet by nbx-txwatcher; "
                        "earlier history may not be fully reflected in the Original/Balance values."
                    )


                body = format_tx_message(
                    wallet_name,
                    direction,
                    amount_sats,
                    local_explorer,
                    public_explorer,
                    txid,
                    ending_balance_sats,
                    utc_str,
                    local_str,
                    tz_label,
                    note,
                )

                subject = f"[{wallet_name}] Transaction in Monitored Wallet"

                print(f"[tx] id={ev.get('eventId')} wallet={wallet_name} dir={direction} amount={amount_sats} sats")
                print(f"     txid={txid}")

                send_email(config, subject, body)

            elif etype == "newblock":
                data = ev.get("data", {})
                height = data.get("height")
                blockhash = data.get("hash", "")[:16]
                print(f"[block] height={height} hash={blockhash}...")
            else:
                print(f"[event] id={ev.get('eventId')} type={etype}")
    except KeyboardInterrupt:
        print("\nStopping nbx-txwatcher on user request (Ctrl+C).")

if __name__ == "__main__":
    main()
