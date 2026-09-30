# Deploying on OCI behind Cloudflare Tunnel and Access (receipts.abalakrishnan.in)

Same pattern as your portfolio: a `cloudflared` sidecar makes an outbound-only connection to Cloudflare, so **no inbound ports are opened** on the server and its IP stays hidden, and Cloudflare Access does the sign-in. On top of that the app checks Access's signed token itself, so a request that somehow skipped Access is still refused.

Written from memory of the Cloudflare and OCI dashboards; menu names change, so trust the dashboard where it differs. Nothing here has been run against your accounts.

You will end up with: the app in Docker, reachable only through Cloudflare, installable on your phone, email forwarding, nightly backups.

It must live on its **own subdomain** (`receipts.abalakrishnan.in`), not a path under your portfolio, because the phone app's scope and service worker are the site root.

## 1. The server (OCI)

1. Pick an India region (Mumbai `ap-mumbai-1` or Hyderabad `ap-hyderabad-1`) so receipts stay in India. It cannot be changed later. If you already run your portfolio on an OCI instance, you can reuse it (see section 8) and skip to section 2.
2. Create a compute instance: Ubuntu 24.04 (or 22.04). A small Always Free shape is enough: `VM.Standard.A1.Flex` (Arm, 1 OCPU, 4-6 GB) if capacity is available, else `VM.Standard.E2.1.Micro` (1 GB, add a 2 GB swap file). Upload your SSH public key.
3. In the VCN security list, allow inbound **only SSH (22) from your own IP**. You do not need 80 or 443, and you do not need to touch `iptables`.

## 2. Cloudflare: tunnel and Access

1. **Tunnel.** Zero Trust dashboard, Networks, Tunnels, Create a tunnel (Cloudflared), name it `receipts`. On the connector step copy the token (the string after `--token`). This is `CLOUDFLARE_TUNNEL_TOKEN`.
2. **Public hostname** on that tunnel: subdomain `receipts`, domain `abalakrishnan.in`, path blank, service type **HTTP**, URL `receipt-tracker:8000`. Saving creates the DNS record for you.
3. **Sign in with Google, owner only (same as MarketBuddy).** Reuse the Google login method you already created for MarketBuddy: Zero Trust, Integrations, Identity providers (older dashboards: Settings, Authentication, Login methods). If you need a new one, register a Google OAuth client with the redirect URI `https://<your-team-domain>/cdn-cgi/access/callback` and paste its Client ID and Secret there.
4. **Access application.** Access, Applications, Add, Self-hosted: name `Receipts`, domain `receipts.abalakrishnan.in`, **session duration 1 month** (so your phone stays signed in). Under identity providers select **only Google** and turn off one-time PIN, so nothing else can be used to sign in. Add a policy named `Owner only`, action **Allow**, include **Emails** = your own Google address, and add no other policy for people. Do not use "Everyone" here; that is right for a public game, wrong for your receipts.
5. Copy two values into `app.env` later: the **AUD tag** (Access application, Overview) as `RT_ACCESS_AUD`, and your **team domain** (Settings, Custom pages, e.g. `yourteam.cloudflareaccess.com`) as `RT_ACCESS_TEAM_DOMAIN`. This is the same setup as hinty-word-pal.
6. **Service token for the iPhone Shortcut** (skip if you have no iPhone): Access, Service Auth, Create service token. Copy the Client ID and Secret now, the secret is shown once. On the `Receipts` application add a second policy with action **Service Auth** that includes this token.

## 3. Install and configure the server

```bash
curl -fsSL https://get.docker.com | sudo sh && sudo usermod -aG docker $USER   # log out and back in afterwards
```

The GitHub repo is private, so give the server read-only access with a deploy key:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/rt_deploy -N ""
cat ~/.ssh/rt_deploy.pub        # GitHub repo, Settings, Deploy keys, Add (leave "write access" off)
GIT_SSH_COMMAND="ssh -i ~/.ssh/rt_deploy -o IdentitiesOnly=yes" git clone git@github.com:asishbalakrishnan/receipt-tracker.git
cd receipt-tracker && git config core.sshCommand "ssh -i ~/.ssh/rt_deploy -o IdentitiesOnly=yes"
cp .env.example .env && cp app.env.example app.env && chmod 600 .env app.env
```

`.env` holds only the tunnel token (and never reaches the app). Paste `CLOUDFLARE_TUNNEL_TOKEN` there. In `app.env`:

- `RT_ACCESS_TEAM_DOMAIN` and `RT_ACCESS_AUD` from step 2.5. Also set `RT_ACCESS_EMAILS=<your Google address>`: the app then double-checks the email in Access's token, so even a mistake in the Access policy (say, someone adds "Everyone") cannot let anyone else in.
- `RT_PUBLIC_URL=https://receipts.abalakrishnan.in`
- `RT_KEY`: run `docker compose run --rm --no-deps app python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` and paste the result. **Save a copy in your password manager now.** Without it, stored receipts and backups are unreadable.
- `RT_LLM_API_KEY` (and `RT_LLM_BASE_URL` / `RT_MODEL` if not using OpenAI).
- Leave `RT_PASSWORD_HASH` and `RT_TOKEN` empty. The app refuses to start with no sign-in configured at all.

Start it:

```bash
docker compose up -d --build
docker compose logs -f          # cloudflared should report it registered a connection
```

Open https://receipts.abalakrishnan.in. Cloudflare Access should ask you to sign in, then the app loads. To confirm the app really checks the token: from the server, `docker compose exec app python -c "import urllib.request as u; print(u.urlopen('http://127.0.0.1:8000/api/session').read())"` should show `authenticated: false`, meaning a request that skips Access is not trusted.

If the page shows a Cloudflare 502, the hostname's service URL does not match the container name and port (`receipt-tracker:8000`), or the containers are not on the same Docker network (this compose file puts them together).

## 4. Use it on your phone

- **Android (Chrome):** open the site, sign in, menu, **Install app**. "Snap a receipt" opens the camera, and **Receipts** appears in the Share menu of Gallery, Files and Gmail.
- **iPhone (Safari only):** open the site, sign in, Share, **Add to Home Screen**. Web apps cannot appear in the iOS share sheet, so use the Shortcut below or email.
- Multi-page bills: take several photos, then "Send as one receipt". With no signal, photos wait on the phone and go out when you are back online.
- When the Access session expires (after a month), the app saves the photo on the phone, sends you through the Cloudflare sign-in, and sends it afterwards. Nothing is lost.

### iPhone share-sheet Shortcut

In the Shortcuts app create a shortcut named "Send receipt": turn on **Show in Share Sheet** (accept Images and PDFs), then add **Get Contents of URL**: URL `https://receipts.abalakrishnan.in/api/receipts`, Method **POST**, Headers `CF-Access-Client-Id` = your service token's Client ID and `CF-Access-Client-Secret` = its secret, Request Body **Form** with one field: name `files`, type **File**, value **Shortcut Input**. Add a **Show Notification** after it. (Menu names may differ by iOS version.) The service token is a full-access key to your receipts, so do not share the shortcut, and revoke the token in Cloudflare if the phone is lost.

## 5. Email intake

Email is fetched by the server making an outbound IMAP connection, so Cloudflare Access does not affect it.

1. Make a mailbox that receives nothing else. For Gmail: turn on 2-step verification, create an **App password**, and enable IMAP in Gmail settings.
2. In `app.env` set `RT_IMAP_HOST=imap.gmail.com`, `RT_IMAP_USER`, `RT_IMAP_PASSWORD` (the app password), `RT_MAIL_ALLOWED_SENDERS=you@yourmail.com`, `RT_MAIL_ADDRESS`. Then `docker compose up -d`.
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

Restore on a fresh server: same `app.env` (especially `RT_KEY`), then
`docker compose run --rm --no-deps -v ~/receipt-backups:/backup app python -m app.backup restore /backup`, then `docker compose up -d`.
**Test a restore once** before you rely on it.

## 8. Alternative: add it to your existing portfolio tunnel

If your portfolio already runs a tunnel on this server, you can skip a second tunnel. In `portfolio-infra/docker-compose.yml` add a service next to the others (build from the sibling directory, same as your other apps):

```yaml
  receipt-tracker:
    build:
      context: ../receipt-tracker
    container_name: receipt-tracker
    restart: unless-stopped
    env_file: ../receipt-tracker/app.env
    environment:
      RT_REQUIRE_AUTH: "1"
    volumes:
      - receipts_data:/data
    expose:
      - "8000"
    networks:
      - internal          # use whatever network name your other services use
# and under the top-level volumes: add   receipts_data:
```

Then in the Cloudflare dashboard add a **public hostname** to that existing tunnel: `receipts.abalakrishnan.in` to `http://receipt-tracker:8000`, and create the Access application from section 2. Skip this repo's own `docker-compose.yml`. `deploy/backup.sh` assumes this repo's compose file, so if you go this route run the backup with `docker exec receipt-tracker python -m app.backup /data/backup` and copy that folder off the volume yourself.

## 9. Updating and housekeeping

```bash
git pull && docker compose up -d --build
sudo apt install unattended-upgrades       # automatic security updates
```

Keep SSH key-only and limited to your IP. Keep `.env` and `app.env` at `chmod 600`.

## What this does and does not protect

- No open ports and no public server IP: the only way in is through Cloudflare, where Access asks you to sign in first. The app then checks Access's signed token itself.
- Receipt files and backups are encrypted with `RT_KEY`, so a stolen backup or disk snapshot is unreadable without it. The key sits in `app.env` on the same server, so a compromised server can read everything. The database is not encrypted on the server itself (OCI encrypts boot volumes by default, worth confirming).
- Cloudflare ends the HTTPS connection at its edge, so it can technically see receipt images in transit, just as it does for your other sites. Include it in your list of processors if this ever becomes a product for other people.
- Receipt images and text are sent to your model provider to be read. Check its data-retention terms, or use a local model.
- A service token or the Access session cookie is a key to your receipts. Revoke them in Cloudflare if a device is lost.
