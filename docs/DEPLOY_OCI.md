# Deploying on Oracle Cloud (OCI) at receipts.abalakrishnan.in

Written from memory of OCI's console and Ubuntu images; menu names and free-tier limits change, so check Oracle's current docs where something differs. Nothing here has been run against a real OCI account.

You will end up with: the app in Docker, Caddy in front getting a free HTTPS certificate, your phone using it like an app, email forwarding, nightly backups.

## 1. The server

1. In the OCI console pick an India region (Mumbai `ap-mumbai-1` or Hyderabad `ap-hyderabad-1`) to keep receipts in India. A region cannot be changed later.
2. Create a compute instance: Ubuntu 24.04 (or 22.04). A small Always Free shape is enough. `VM.Standard.A1.Flex` (Arm, 1 OCPU and 4-6 GB) is roomier, but free Arm capacity is often "out of capacity"; `VM.Standard.E2.1.Micro` (1 GB) also works for one user, add a 2 GB swap file if it struggles. All Python dependencies publish Arm wheels.
3. Give it a **reserved public IP** (Networking, Reserved public IPs) so the address never changes, and upload your SSH public key.
4. In the instance's VCN **security list** (or network security group) allow inbound TCP **80 and 443** from anywhere, and **22 only from your own IP**.
5. Ubuntu images on OCI also ship host firewall rules that block ports. On the server:
   ```bash
   sudo iptables -L INPUT -n --line-numbers        # find the line number of the REJECT rule
   sudo iptables -I INPUT <that number> -p tcp --dport 80  -j ACCEPT
   sudo iptables -I INPUT <that number> -p tcp --dport 443 -j ACCEPT
   sudo netfilter-persistent save
   ```

## 2. DNS

At wherever `abalakrishnan.in` DNS is managed, add one record and touch nothing else (especially any existing MX or mail records):

| Type | Name | Value |
|---|---|---|
| A | `receipts` | the reserved public IP |

## 3. Install and configure

```bash
curl -fsSL https://get.docker.com | sudo sh && sudo usermod -aG docker $USER   # log out and back in afterwards
```

The GitHub repo is private, so give the server read-only access with a deploy key:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/rt_deploy -N ""
cat ~/.ssh/rt_deploy.pub        # GitHub repo, Settings, Deploy keys, Add (leave "write access" off)
GIT_SSH_COMMAND="ssh -i ~/.ssh/rt_deploy -o IdentitiesOnly=yes" git clone git@github.com:asishbalakrishnan/receipt-tracker.git
cd receipt-tracker && git config core.sshCommand "ssh -i ~/.ssh/rt_deploy -o IdentitiesOnly=yes"
cp .env.example .env && chmod 600 .env
```

Edit `.env` (`nano .env`):

- `RT_DOMAIN=receipts.abalakrishnan.in` and `RT_PUBLIC_URL=https://receipts.abalakrishnan.in`
- `RT_KEY`: run `docker compose run --rm --no-deps app python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` and paste the result. **Save a copy in your password manager now.** Without it, stored receipts and backups are unreadable.
- `RT_PASSWORD_HASH`: run `docker compose run --rm --no-deps app python -m app.auth`, choose a long password, paste the line it prints (keep the single quotes).
- `RT_LLM_API_KEY` (and `RT_LLM_BASE_URL` / `RT_MODEL` if not using OpenAI).
- `RT_TOKEN`: `openssl rand -hex 32` (only needed for the iOS Shortcut below).

Start it:

```bash
docker compose up -d --build
docker compose logs -f          # Caddy should report it obtained a certificate
```

Open https://receipts.abalakrishnan.in and sign in. If the certificate fails, the cause is nearly always DNS not yet pointing at the IP, or ports 80/443 blocked in the security list or the iptables step.

## 4. Use it on your phone

- **Android (Chrome):** open the site, menu, **Install app**. Then "Snap a receipt" opens the camera, and **Receipts** appears in the Share menu of Gallery, Files and Gmail.
- **iPhone (Safari only):** open the site, Share, **Add to Home Screen**. Web apps cannot appear in the iOS share sheet, so use the Shortcut below or email.
- Multi-page bills: take several photos, then "Send as one receipt". With no signal, photos wait on the phone and go out when you are back online.

### iPhone share-sheet Shortcut

In the Shortcuts app create a shortcut named "Send receipt": turn on **Show in Share Sheet** (accept Images and PDFs), then add **Get Contents of URL**: URL `https://receipts.abalakrishnan.in/api/receipts`, Method **POST**, Headers `Authorization` = `Bearer <your RT_TOKEN>`, Request Body **Form** with one field: name `files`, type **File**, value **Shortcut Input**. Add a **Show Notification** after it. (Menu names may differ by iOS version.) The token is a full-access key to your receipts, so do not share the shortcut.

## 5. Email intake

1. Make a mailbox that receives nothing else. For Gmail: turn on 2-step verification, create an **App password**, and enable IMAP in Gmail settings.
2. In `.env` set `RT_IMAP_HOST=imap.gmail.com`, `RT_IMAP_USER`, `RT_IMAP_PASSWORD` (the app password), `RT_MAIL_ALLOWED_SENDERS=you@yourmail.com`, `RT_MAIL_ADDRESS`. Then `docker compose up -d`.
3. Forward a receipt or e-invoice from an allowed address to that mailbox. Within a couple of minutes it appears in the app and moves to a `Processed` folder.
4. Mail from anyone else, or that the provider could not verify (`dmarc=pass`), goes to `Rejected` and is never shown to the AI. If your own forwards land in `Rejected`, check that the header exists; only if your provider never adds it set `RT_MAIL_REQUIRE_AUTH=0`, knowing that a forged sender then passes the check.

## 6. Notifications (optional)

Install the ntfy app, subscribe to a long random topic, and set `RT_NTFY_URL=https://ntfy.sh/<that-topic>`. Messages say only "2 filed, 1 need review" unless `RT_NTFY_DETAILS=1`; anyone who knows the topic name can read it, so keep details off on the public server.

## 7. Backups

```bash
chmod +x deploy/backup.sh
crontab -e     # add:  30 2 * * *  /home/ubuntu/receipt-tracker/deploy/backup.sh
```

This writes an encrypted database snapshot (last 14 kept) and a mirror of the encrypted receipt files to `~/receipt-backups`. To also copy it off the server, create an OCI Object Storage bucket, configure an `rclone` remote for its S3-compatible endpoint, and set `RT_BACKUP_REMOTE=remote:bucket` in the cron environment. Deleting a receipt in the app removes its file from the mirror on the next run.

Restore on a fresh server: same `.env` (especially `RT_KEY`), then
`docker compose run --rm -v ~/receipt-backups:/backup app python -m app.backup restore /backup`, then `docker compose up -d`.
**Test a restore once** before you rely on it.

## 8. Updating and housekeeping

```bash
git pull && docker compose up -d --build
sudo apt install unattended-upgrades       # automatic security updates
```

Keep SSH key-only and limited to your IP. Keep `.env` at `chmod 600`.

## What this does and does not protect

- Receipt files and backups are encrypted with `RT_KEY`, so a stolen backup or disk snapshot is unreadable without it.
- The key sits in `.env` on the same server, so a compromised server can read everything. The database is not encrypted on the server itself (OCI encrypts boot volumes by default, worth confirming).
- Receipt images and text are sent to your model provider to be read. Check its data-retention terms, or use a local model.
- Login is a single password with rate limiting. Use a long passphrase.
