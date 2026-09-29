# Receipt Tracker (personal, phase 1)

Upload a photo, PDF or forwarded email (.eml) of a receipt. A vision model reads it, categorises it, checks the numbers, and anything doubtful goes to a Review queue. Originals are stored encrypted.

## What it does

- **Phone:** installable web app with a "Snap a receipt" camera button, multi-page receipts, an offline queue, and Share-to-app on Android.
- **Email:** forward a receipt to a dedicated mailbox; the app checks it over IMAP. Only allowed, verified senders are read.
- **Background reading:** uploads return at once; a worker reads each receipt with the model, so slow networks or model errors never lose a file.
- **Review queue:** anything doubtful is flagged and waits for you; your corrections teach later classifications.

## Run locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # set RT_LLM_API_KEY (and RT_MODEL if not using OpenAI)
export $(grep -v '^#' .env | xargs)
uvicorn app.main:app_factory --factory --port 8000
```

Open http://localhost:8000. With no password or token set it is open, which is fine on your own machine only. Without a model key (or a local `RT_LLM_BASE_URL`) every upload is stored and sent to Review for manual entry. Camera and install need HTTPS, so for phones deploy it: **see [docs/DEPLOY_OCI.md](docs/DEPLOY_OCI.md)** (Docker, Caddy, backups, email, iPhone Shortcut).

Tests: `python3 -m pytest -q`

## Settings (env vars)

Everything is documented in `.env.example`. The main ones:

| Variable | Purpose |
|---|---|
| `RT_PASSWORD_HASH` | Browser login (`python -m app.auth` creates it) |
| `RT_TOKEN` | Long-lived API token for scripts and the iOS Shortcut (`Authorization: Bearer`) |
| `RT_LLM_API_KEY`, `RT_LLM_BASE_URL`, `RT_MODEL` | Model provider (any OpenAI-compatible endpoint) |
| `RT_PDF_MODE`, `RT_JSON_MODE` | Provider quirks for PDFs and JSON replies |
| `RT_KEY` | Encryption key for receipts and backups (otherwise generated in `data/secret.key`) |
| `RT_DATA_DIR` | Database, encrypted files and key location |
| `RT_IMAP_*`, `RT_MAIL_ALLOWED_SENDERS`, `RT_MAIL_REQUIRE_AUTH` | Email intake |
| `RT_NTFY_URL` | Phone notifications |
| `RT_CONFIDENCE_THRESHOLD` | Below this a field is flagged for review (default 0.8) |

## Switching model provider

The app calls the standard OpenAI Chat Completions API over plain HTTP, with no provider SDK. To switch, change only the base URL, key and model name:

| Provider | `RT_LLM_BASE_URL` |
|---|---|
| OpenAI | (default) |
| OpenRouter | `https://openrouter.ai/api/v1` |
| Gemini | `https://generativelanguage.googleapis.com/v1beta/openai` |
| Anthropic | `https://api.anthropic.com/v1` (images only, so set `RT_PDF_MODE=images`) |
| Ollama / vLLM / LM Studio | `http://localhost:11434/v1` etc. (use a vision model) |

Receipts never need to leave your machine if you use a local model, though small local models read receipts less accurately. The provider endpoints above are from memory and are not tested here; confirm each provider's current compatibility notes.

## How it works

- Extraction returns merchant, date, total, tax, GSTIN, line items, category and per-field confidence.
- Your merchant rules run before the AI. Your corrections are logged, and the latest 20 are fed back into the prompt.
- Deterministic checks: GSTIN format and checksum, future or missing dates, line items vs total, non-INR currency, duplicates (same file, or same merchant/date/amount).
- Money is stored as integer paise.
- Extraction failures never lose the file; it is kept and flagged.

## Back up and privacy

- **Back up `data/secret.key`** (or your `RT_KEY`). Without it the stored receipts cannot be decrypted.
- Only the original files are encrypted. The SQLite database (`data/app.db`) is not, so use full-disk encryption on the machine.
- Receipt images go to the Anthropic API for reading. Check your account's data-retention terms.
- Settings has full export (CSV, JSON) and a wipe-everything button.
- Personal use is largely outside India's DPDP Act. Before opening this to other people you need the notice, consent, erasure, breach-reporting and processor steps in the Feature Set doc.

## Not built yet

Multiple users and sync, native mobile app, WhatsApp intake, budgets and alerts, Tally export, multi-user privacy flows.

## Known gaps

- **Extraction accuracy is untested.** The request format is tested against a mock server, never a live model or provider. Run about 100 of your real receipts and check the auto-accept rate and error rate before trusting auto-accepted records.
- The GSTIN checksum was verified against one known sample number only.
- The phone app was tested in a phone-sized headless Chromium (login, camera input, multi-page, offline queue), not on real Android or iOS devices. Install, camera and Share-to-app behaviour on real phones is unverified.
- Email intake is tested against a scripted stand-in for an IMAP server, not a real mailbox. Gmail's folder and expunge behaviour is from memory.
- The Docker and OCI files have not been built or run: no Docker daemon was available where this was written. The compose file validates, and the app runs with the same server flags outside Docker.
- Backups are tested for round-trip restore locally, not against OCI Object Storage.
