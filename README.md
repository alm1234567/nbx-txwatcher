# nbx-txwatcher

A lightweight Bitcoin transaction watcher for **RaspiBlitz** using **NBXplorer** and **BTCPay Server**.

It monitors one or more wallets (single‑sig or multisig registered in BTCPay), listens to NBXplorer’s event stream, and sends an email notification whenever a **new transaction is first broadcast** (0 confirmations).

Optionally, it can:

- Include links to a local mempool explorer and/or public explorer.
- Encrypt notification emails to a PGP public key.

---

## Overview

### Concept

`nbx-txwatcher` is a standalone Python script that:

1. Connects to NBXplorer’s HTTP API using the same cookie credentials as BTCPay.
2. Identifies and subscribes to the wallets you want to monitor.
3. Listens to NBXplorer’s `newtransaction` and `newblock` events via its event stream.
4. For each **newly broadcast** transaction (0 confirmations) on a monitored wallet:
   - Computes the **net wallet movement**, including fees.
   - Fetches the wallet’s **current balance** from NBXplorer.
   - Derives the **previous balance** and formats:
     - Original balance  
     - Transaction amount  
     - New balance
   - Sends a single email notification per transaction per wallet.
5. Optionally encrypts the email body with the recipient’s PGP public key.

The watcher is designed to:

- Run as the `btcpay` user on RaspiBlitz.
- Use a simple INI configuration file (`nbx-txwatcher.conf`).
- Be supervised by `systemd` so it restarts automatically.

---

## How it is built

- **Language**: Python 3 (standard library only).
- **Backend**:
  - [NBXplorer](https://github.com/dgarage/NBXplorer) via its HTTP API and event stream.
  - Wallet registration for multisig through **BTCPay Server** (BTCPay exposes derivation strategies for NBX).
- **Daemonization**: `systemd` service:
  - Runs as `btcpay`.
  - Restarts on failure.
- **Email**:
  - SMTP with TLS (STARTTLS).
  - Optional PGP encryption using `gpg` on the machine where it runs.

---

## Limitations

1. **Initial wallet balance accuracy**

   NBXplorer only knows about transactions **from the point it is synchronized** for that wallet.

   For the **first transaction observed** by `nbx-txwatcher` on a given wallet:

   - The “Original” balance is inferred as:

     > Original = EndingBalance + |NetTxAmount| (for outbound)  
     > Original = EndingBalance − NetTxAmount (for inbound)

   - If there was wallet history before NBX was synced, that earlier history may not be fully reflected.

   For cosmetic clarity, the first transaction email per wallet includes a note:

   > This is the first transaction observed for this wallet by nbx-txwatcher; earlier history may not be fully reflected in the Original/Balance values.

2. **Multisig wallets must be registered in BTCPay Server**

   NBXplorer itself needs a **derivation strategy** to track multisig wallets.

   The easiest, RaspiBlitz‑friendly way is to:

   1. Create or import the multisig wallet in **BTCPay Server**.
   2. Let BTCPay register it with NBXplorer.
   3. Copy the resulting `derivationStrategy` from NBX events and put it in `nbx-txwatcher.conf`.

   Without this, NBX will not emit events for your multisig wallet and `nbx-txwatcher` cannot monitor it.

3. **“New transaction” means mempool broadcast**

   - The watcher only notifies on the **initial 0‑confirmation broadcast event**.
   - Confirmation updates (1, 2, … confirmations) are **ignored** by design.
   - This is a monitoring tool for *new activity*, not for tracking confirmation depth.

4. **One process per NBX instance**

   - The script assumes one NBX URL and one set of credentials.
   - Running multiple instances against the same NBX instance is not recommended (you’ll just duplicate notifications).

---

## Requirements

### Environment

- A **RaspiBlitz** node with:
  - **Bitcoin Core** fully synced. This was developed and tested on a x86-64 virtual machine.
  - **BTCPay Server** installed/enabled.
  - **NBXplorer** running (automatically handled by RaspiBlitz when BTCPay is enabled).

- User:
  - The service should run as the **`btcpay`** user so it can:
    - Access the NBX cookie file.
    - Use the same network namespace and permissions as BTCPay.

- Python:
  - Python 3 installed (typically already present on RaspiBlitz).

### NBXplorer credentials on RaspiBlitz

On RaspiBlitz with BTCPay integration, NBXplorer uses a cookie file for authentication.

For the `btcpay` user, the cookie is typically located at:

```text
/home/btcpay/.nbxplorer/Main/.cookie
```

The file contents look like:

```text
__cookie__:67611...
```

Where:

- `__cookie__` is the HTTP username.
- `67611...` (the rest of the line) is the HTTP password.

`nbx-txwatcher` can either:

1. Read the cookie file directly (recommended), or  
2. Use explicit `nbx_user` / `nbx_pass` in the config.

---

## Installation

> All commands below assume a RaspiBlitz with BTCPay + NBXplorer enabled and that you are logged in as `admin` via SSH.

### 1. Clone the repository

```bash
cd /home/admin
git clone https://github.com/YOUR_GITHUB_USERNAME/nbx-txwatcher.git
cd nbx-txwatcher
```

### 2. Install the script and config

Copy the main script and example config to `/home/admin`:

```bash
sudo cp nbx-txwatcher.py /home/admin/nbx-txwatcher.py
sudo cp nbx-txwatcher.conf.example /home/admin/nbx-txwatcher.conf
sudo chown btcpay:btcpay /home/admin/nbx-txwatcher.py /home/admin/nbx-txwatcher.conf
chmod 640 /home/admin/nbx-txwatcher.conf
```

### 3. Configure `nbx-txwatcher.conf`

Edit:

```bash
sudo nano /home/admin/nbx-txwatcher.conf
```

Set at least:

```ini
[global]
nbx_url        = http://127.0.0.1:24444
nbx_cookiefile = /home/btcpay/.nbxplorer/Main/.cookie

smtp_host = smtp.example.com
smtp_port = 587
smtp_user = your_smtp_login
smtp_pass = your_smtp_password
smtp_from = "Your Node <node@example.com>"
smtp_to   = your_destination_email@example.com

# Optional explorers
local_explorer_url = https://10.0.0.2:4081
explorer_url       = https://mempool.space

# Optional PGP
pgp_enable    = false
pgp_recipient = your-pgp-key-id-or-email
pgp_homedir   = /home/btcpay/.gnupg

# Optional timezone label for emails
local_tz_label = GMT-3
```

Then define your wallets (see “Wallet configuration” below).

### 4. Install systemd service

Copy the service unit:

```bash
sudo cp nbx-txwatcher.service /etc/systemd/system/nbx-txwatcher.service
sudo systemctl daemon-reload
sudo systemctl enable nbx-txwatcher.service
sudo systemctl start nbx-txwatcher.service
```

Check logs:

```bash
journalctl -u nbx-txwatcher.service -n 50 -f
```

You should see messages like:

```text
=== nbx-txwatcher ===
1. Loading config...
2. Registering derivations...
3. Streaming events from NBX... (Ctrl+C to stop)
[block] height=...
```

---

## Configuration

### Global section

```ini
[global]
nbx_url        = http://127.0.0.1:24444

# Preferred: use the NBX cookie file (RaspiBlitz + BTCPay default)
nbx_cookiefile = /home/btcpay/.nbxplorer/Main/.cookie

# OR explicitly set credentials (discouraged; avoid committing secrets)
# nbx_user = __cookie__
# nbx_pass = 67611...

# SMTP settings
smtp_host = smtp.example.com
smtp_port = 587
smtp_user = your_smtp_login
smtp_pass = your_smtp_password
smtp_from = "Your Node <node@example.com>"
smtp_to   = your_destination_email@example.com

# Optional explorers
local_explorer_url = https://10.0.0.2:4081
explorer_url       = https://mempool.space

# Optional PGP settings
pgp_enable    = false
pgp_recipient = you@example.com
pgp_homedir   = /home/btcpay/.gnupg

# Optional timezone label for the local time shown in emails
local_tz_label = GMT-3
```

### Wallet configuration

Each wallet gets its own section. For example:

```ini
[wallet_coldcard_nox]
name = Coldcard (NOX)
xpub = xpub6XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX

[wallet_coldcard_caju_temp]
name = Coldcard Caju (Temp)
xpub = xpub6XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX

[wallet_multisig_backup]
name       = 2-of-3 Multisig Backup
derivation = 2-of-3([f23a.../48h/0h/0h/2h]xpub...,[...])
```

Two modes:

1. **Single‑sig via xpub**

   - Set `xpub` to your wallet’s public key (converted as needed to NBX‑compatible xpub).
   - `nbx-txwatcher` will attempt to register it directly with NBXplorer.

2. **Multisig or BTCPay‑managed wallets**

   - Set `derivation` to the **exact** `derivationStrategy` NBX uses (obtained via BTCPay and NBX events — see below).
   - In this case, the script does **not** attempt registration; it trusts NBX/BTCPay.

---

## Optional: mempool explorer links

If you have:

- A local mempool instance (e.g. RaspiBlitz mempool), and/or
- A preferred public explorer (e.g. mempool.space),

you can configure:

```ini
[global]
local_explorer_url = https://10.0.0.2:4081
explorer_url       = https://mempool.space
```

Emails will then include links like:

```text
https://10.0.0.2:4081/tx/<txid>
https://mempool.space/tx/<txid>
```

If you leave these empty, the links are simply omitted.

---

## Optional: PGP‑encrypted notifications

To enable PGP‑encrypted email:

1. Ensure `gpg` is installed on your node.

2. As the `btcpay` user, create or import your GPG keyring:

   ```bash
   sudo -u btcpay -s
   gpg --homedir /home/btcpay/.gnupg --import /path/to/your-public-key.asc
   gpg --homedir /home/btcpay/.gnupg --list-keys
   exit
   ```

3. Set in `nbx-txwatcher.conf`:

   ```ini
   [global]
   pgp_enable    = true
   pgp_recipient = you@example.com     ; or key ID / fingerprint
   pgp_homedir   = /home/btcpay/.gnupg
   ```

When enabled, the email body is encrypted to `pgp_recipient` before being sent via SMTP.

> Note: PGP behavior may differ between providers (e.g. ProtonMail vs Gmail). Always send a test transaction to verify your mailbox correctly decrypts and displays the content.

---

## Getting the correct output descriptor / derivation for multisig wallets

Multisig wallets are best handled through **BTCPay Server**, which manages NBX registration.

### Steps

1. **Create or import the multisig wallet in BTCPay**

   - In BTCPay Server, create a new Store (if needed).
   - Under `Wallet` → `Setup` → choose **Import**.
   - Paste your descriptor (or xpubs + script type) according to BTCPay’s instructions.
   - BTCPay will register the wallet with NBXplorer.

2. **Trigger a test transaction**

   - Send a small transaction to or from this multisig wallet.
   - This forces NBXplorer to emit `newtransaction` events for that derivation.

3. **Capture the `derivationStrategy` from NBX events**

   - Temporarily run `nbx-txwatcher.py` in the foreground to inspect events:

     ```bash
     sudo -u btcpay python3 /home/admin/nbx-txwatcher.py
     ```

   - Or call NBX directly (e.g. `curl` to `/v1/cryptos/BTC/events`).
   - Look at the `data.derivationStrategy` field in the JSON for your multisig tx. It will look like a descriptor‑style string, for example:

     ```text
     2-of-3([f23a.../48h/0h/0h/2h]xpub...,[...])
     ```

4. **Configure `nbx-txwatcher.conf`**

   - For that wallet section, set:

     ```ini
     [wallet_multisig_backup]
     name       = 2-of-3 Multisig Backup
     derivation = 2-of-3([f23a.../48h/0h/0h/2h]xpub...,[...])
     ```

   - Do **not** set an `xpub` for multisig; use the full `derivation` string.

The watcher will then:

- Map `derivationStrategy` values in events to your `name`.
- Correctly compute inbound/outbound deltas and balances for that multisig wallet.

---

## Behavior summary

For each new transaction (0 confirmations) on a monitored wallet:

1. `nbx-txwatcher` computes:

   ```text
   net_delta_sats = sum(outputs_to_wallet) - sum(inputs_from_wallet)
   ```

   - Positive → **Inbound** (wallet gained coins).
   - Negative → **Outbound** (wallet lost coins, including fees).

2. It reads wallet ending balance from NBX.

3. It infers original balance:

   - Inbound: `Original = Ending - Amount`.
   - Outbound: `Original = Ending + Amount`.

4. It sends one email with a body like:

   ```text
   ----------------------------------
   Wallet:       <Wallet Name>
   Direction:    Inbound/Outbound
   Date (UTC):   22/Nov/25 14:03:45
   Date (GMT-3): 22/Nov/25 11:03:45
   ----------------------------------
   Original:     X.XXXXXXXX BTC
   Transaction: -0.00000001 BTC
   Balance:      X.XXXXXXXX BTC
   ----------------------------------
   https://your-local-mempool/tx/<txid>
   https://mempool.space/tx/<txid>

   Note: This is the first transaction observed for this wallet by nbx-txwatcher; earlier history may not be fully reflected in the Original/Balance values.
   ```

   The note at the bottom only appears on the **first transaction observed** for each wallet.

---

## Troubleshooting

### No emails received

- Check service logs:

  ```bash
  journalctl -u nbx-txwatcher.service -n 50
  ```

- Verify SMTP host, port, user, and password.
- Test SMTP separately with another tool (e.g. a minimal Python script).
- Check spam/junk folder and mail provider filters.

### No events / nothing logged

- Confirm NBX is running and reachable at `nbx_url`.
- Ensure BTCPay is enabled and NBX is not in error state.
- Verify your wallet’s `xpub` / `derivation` is correct in `nbx-txwatcher.conf`.

### Multisig wallet not triggering

- Confirm the wallet is imported in BTCPay and displays transactions there.
- Check NBX events for `derivationStrategy` and verify it matches `derivation` in your config.

### Duplicate notifications

- Ensure only **one** watcher process is running:

  ```bash
  ps aux | grep nbx-txwatcher.py | grep -v grep
  ```

- If you see multiple:

  - Stop manual foreground runs.
  - Let only the systemd service run the watcher.

```
