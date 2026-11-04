# Holoween Sequential Account Manager Bot

A single-file (`main.py`) Python 3.10+ Telegram bot built with `aiogram 3` and `SQLite` (WAL mode) designed for managing manual sequential X (Twitter) account signups (`Holoween00111`, `Holoween00112`, ...) and verifying profile existence via official X API v2 or tokenless public syndication fallback.

---

## 🔒 Security & Verification Architecture

1. **Single-Admin Enforcement:** Protected by `SingleAdminMiddleware` which intercepts all messages and inline callbacks, rejecting any user ID that does not match `ADMIN_TELEGRAM_ID`.
2. **Zero Credential Storage Policy:** The bot never requests, processes, or stores X passwords, OTPs, session cookies, or authentication tokens.
3. **Public Existence vs. Confirmed Ownership:**
   - `/verify USERNAME` queries X to check if a profile is publicly discoverable.
   - Public discoverability confirms existence on X, not account ownership.
   - Tapping **"Confirm My Signup & Record"** officially commits the account to `confirmed_accounts`, closes any active session, and atomically advances `sequence_meta.next_seq`.

---

## 📂 Project Structure

```text
/opt/holoween_bot/
├── main.py          # Complete bot application (single file)
├── requirements.txt # Python dependencies
├── .env             # Secure configuration & credentials
└── README.md        # Installation & deployment guide
