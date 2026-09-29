# Receipt Tracker (personal, phase 1)

Upload a photo, PDF or forwarded email (.eml) of a receipt. A vision model reads it, categorises it, checks the numbers, and anything doubtful goes to a Review queue. Originals are stored encrypted.

## Run

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # then set RT_LLM_API_KEY (and RT_MODEL if not using OpenAI)
export $(grep -v '^#' .env | xargs)
uvicorn app.main:app_factory --factory --port 8000
```

Open http://localhost:8000. Without a key (or a local `RT_LLM_BASE_URL`) the app still works: every upload is stored and sent to Review for manual entry.

Tests: `python3 -m pytest -q`

## Settings (env vars)

| Variable | Purpose |
|---|---|
| `RT_LLM_API_KEY` | Key for the model provider (`OPENAI_API_KEY` also works). Not needed for local servers |
| `RT_LLM_BASE_URL` | Any OpenAI-compatible endpoint (default `https://api.openai.com/v1`) |
| `RT_MODEL` | Vision-capable model name on that provider (default `gpt-4o`) |
| `RT_PDF_MODE` | `auto` (render PDF pages to images with PyMuPDF, else send the PDF natively), `images`, or `file` |
| `RT_JSON_MODE` | `0` if your server rejects `response_format=json_object` |
| `RT_DATA_DIR` | Where the database, encrypted files and key live (default `./data`) |
| `RT_KEY` | Encryption key (otherwise generated in `data/secret.key`) |
| `RT_TOKEN` | If set, every API call needs this token (the UI asks for it). Set it if the app is reachable from anywhere but your own machine |
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

Accounts and sync, mobile app, WhatsApp intake, budgets and alerts, Tally export, multi-user privacy flows.

## Known gaps

- **Extraction accuracy is untested.** The request format is tested against a mock server, never a live model or provider. Run about 100 of your real receipts and check the auto-accept rate and error rate before trusting auto-accepted records.
- The GSTIN checksum was verified against one known sample number only.
- The UI was smoke-tested in headless Chromium, not on a phone.
