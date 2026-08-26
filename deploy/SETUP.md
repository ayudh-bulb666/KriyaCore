# GYMPro — VPS deployment runbook (India-billed, self-hosted)

Follow this top to bottom on a fresh Ubuntu 22.04 VPS. Commands are meant to
be copy-pasted over SSH. Total cost target: ~₹300–600/month for the VPS,
₹0 beyond that (Let's Encrypt SSL is free).

## 0. Pick a VPS and buy a domain

- **VPS**: Hostinger VPS KVM 1 (1 vCPU / 4GB RAM / 50GB NVMe, INR billing,
  no forex markup) or E2E Networks (Indian company, own India datacenters).
  Either is plenty for 2 gyms' worth of traffic.
- **Domain**: any registrar, ~₹800–1000/year for a `.com`. Point an **A
  record** at your VPS's IP address once you have it (this can take a few
  minutes to a few hours to propagate).

You'll SSH in as `root` initially — the provider emails you the IP and a
root password (or lets you upload an SSH key at signup, which is safer:
prefer that if offered).

## 1. Initial server setup (as root)

```bash
ssh root@YOUR_VPS_IP

apt update && apt upgrade -y

# A non-root user to run everything under — never run the app as root
adduser deploy
usermod -aG sudo deploy

# Copy your SSH key over so you can log in as 'deploy' without a password
rsync --archive --chown=deploy:deploy ~/.ssh /home/deploy

# Firewall: only SSH, HTTP, HTTPS
ufw allow OpenSSH
ufw allow 80
ufw allow 443
ufw --force enable

# Harden SSH: no root login, no password auth (key-only)
sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
systemctl restart ssh
```

From here on, log back in as `deploy`, not `root`:
```bash
ssh deploy@YOUR_VPS_IP
```

## 2. Install everything the app needs

```bash
sudo apt install -y python3 python3-venv python3-pip \
    postgresql postgresql-contrib \
    nginx certbot python3-certbot-nginx \
    git
```

## 3. Set up Postgres

```bash
sudo -u postgres psql
```
Inside the `psql` prompt:
```sql
CREATE DATABASE gympro;
CREATE USER gympro WITH PASSWORD 'pick-a-strong-password-here';
GRANT ALL PRIVILEGES ON DATABASE gympro TO gympro;
ALTER DATABASE gympro OWNER TO gympro;
\q
```

## 4. Deploy the code

```bash
cd /home/deploy
git clone https://github.com/YOUR_USERNAME/GYMPro.git gympro
cd gympro

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp deploy/.env.example .env
nano .env   # fill in SECRET_KEY, DATABASE_URL (with the password from step 3), etc.
chmod 600 .env
```

Generate a real `SECRET_KEY`:
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```
Paste that into `.env`.

Test it runs before wiring up systemd:
```bash
set -a; source .env; set +a
gunicorn --bind 127.0.0.1:8000 run:app
# Ctrl+C once you see it start cleanly with no traceback
```

## 5. systemd service (keeps it running forever)

```bash
sudo cp deploy/gympro.service /etc/systemd/system/gympro.service
sudo systemctl daemon-reload
sudo systemctl enable --now gympro
sudo systemctl status gympro   # should say "active (running)"
```

## 6. Nginx + free SSL

```bash
sudo cp deploy/nginx.conf /etc/nginx/sites-available/gympro
sudo nano /etc/nginx/sites-available/gympro   # replace "your-domain.com" with your real domain
sudo ln -s /etc/nginx/sites-available/gympro /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

sudo certbot --nginx -d your-domain.com
```
Certbot edits the Nginx config automatically to add HTTPS + a redirect from
HTTP, and sets up auto-renewal. Your site is now live at
`https://your-domain.com`.

## 7. Nightly backups (Railway gives you this for free — this replaces it)

```bash
chmod +x deploy/backup_db.sh
crontab -e
```
Add this line:
```
30 2 * * * /home/deploy/gympro/deploy/backup_db.sh >> /home/deploy/backup.log 2>&1
```

## 7b. Automatic WhatsApp expiry reminders (optional)

Only matters if you've set `CRON_SECRET` in `.env` and turned on auto-reminders
for a gym under WhatsApp → Settings. Add a second crontab line — this one
hits the app itself over HTTP, so it works the same whether you're on this
VPS or on Railway (swap the URL for your Railway domain and point Railway's
own Cron trigger at the same curl command instead of using crontab there):

```
0 9 * * * curl -s -X POST https://your-domain.com/internal/cron/whatsapp-reminders -H "X-Cron-Secret: $(grep CRON_SECRET /home/deploy/gympro/.env | cut -d= -f2)" >> /home/deploy/whatsapp-cron.log 2>&1
```

Runs daily at 9 AM. Test it manually first:
```bash
curl -X POST https://your-domain.com/internal/cron/whatsapp-reminders \
  -H "X-Cron-Secret: <same value as CRON_SECRET in .env>"
```
A working response looks like `{"ok": true, "gyms_processed": N, "results": [...]}`.

## 8. First login

The app seeds itself on first boot (platform admin + PowerFit Mumbai demo
data) — same as local dev. Log in at `https://your-domain.com/login` with
`platform@gympro.com` / `platform123` and **change that password
immediately** via the account menu.

## 9. WhatsApp — testing with your own Meta Business Account

This works the same locally or on a deployed instance — set the same three
env vars either way. Everything below is free for testing; no business
verification or payment needed until you outgrow the test number.

**a. Create the Meta app**
1. Go to [developers.facebook.com](https://developers.facebook.com) → **My Apps → Create App** → choose **Business** as the app type
2. Once created, on the app dashboard, find **WhatsApp** in the product list and click **Set up**
3. Meta automatically provisions a **free test phone number** — no need to register your own

**b. Get your three credentials**
On the WhatsApp → API Setup page in the Meta dashboard, you'll see:
- A **temporary access token** (valid 24 hours — fine for testing; generate a permanent one later via a System User under Business Settings when you're ready to go live) → this is `WHATSAPP_API_KEY`
- A **Phone Number ID** (a numeric ID, not the phone number itself) → this is `WHATSAPP_PHONE_NUMBER_ID`

**c. Add yourself as a verified test recipient**
The free test number can only message phone numbers you've explicitly added. On the same API Setup page, under **To**, click **Manage phone number list** and add your own number (up to 5 numbers, no cost). You'll get an OTP to confirm it.

**d. Create the message templates**
Go to **WhatsApp Manager → Message Templates → Create Template**. GYMPro expects the template **name** to exactly match its internal message type — create these two (category: **Utility**, since both are transactional, not promotional):

| Template name | Body text (use Meta's `{{1}}` `{{2}}` numbered placeholders) |
|---|---|
| `expiry_reminder` | `Hi {{1}}, your {{2}} membership at {{3}} expires on {{4}} ({{5}} days left). Renew soon to keep your access!` |
| `renewal_confirmation` | `Hi {{1}}, your {{2}} membership at {{3}} has been renewed. New expiry: {{4}}. Thank you!` |

The order matters — it must match `TEMPLATES[...]['vars']` in `app/whatsapp_provider.py`
(`member_name, gym_name, plan_name, expiry_date, days_left` for the reminder;
`member_name, gym_name, plan_name, new_expiry_date` for the confirmation).
Submission is usually approved within a few minutes for straightforward utility text, but Meta can take longer or reject wording that looks promotional.

**e. Set the env vars and test**

Locally:
```bash
export WHATSAPP_PROVIDER=meta
export WHATSAPP_API_KEY=<your temporary access token>
export WHATSAPP_PHONE_NUMBER_ID=<your Phone Number ID>
python run.py
```
On the VPS, add the same three lines to `.env` instead (see `deploy/.env.example`) and `sudo systemctl restart gympro`.

Then in GYMPro: make sure the test member's phone number is one you added as a verified recipient in step (c), opt them in on their member page, and send a reminder. If Meta's approval hasn't landed yet, or the recipient isn't verified, the message log will show exactly why — Meta's own error text comes straight through, e.g. "template not found" or "recipient phone number not in allowed list."

## Future deploys

Push to GitHub, then on the VPS:
```bash
/home/deploy/gympro/deploy/deploy.sh
```
That's the whole workflow — pulls, installs, migrates, restarts, tails logs.

## What you're now responsible for that Railway used to handle

- **OS security updates** — run `sudo apt update && sudo apt upgrade` every
  so often (a monthly cadence is plenty at this scale).
- **Disk space** — backups accumulate in `/home/deploy/backups`; the backup
  script auto-deletes anything older than 14 days, but keep an eye on it.
- **SSL renewal** — certbot sets up auto-renewal via a systemd timer, but
  it's worth confirming it fires: `sudo certbot renew --dry-run`.
- **Postgres itself** — no managed dashboard anymore. `sudo -u postgres psql`
  is your admin console now.
