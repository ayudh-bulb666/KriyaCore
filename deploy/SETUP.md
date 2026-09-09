# KriyaCore — VPS deployment runbook (India-billed, self-hosted)

Follow this top to bottom on a fresh Ubuntu 22.04 VPS. Commands are meant to
be copy-pasted over SSH. Total cost target: ~₹300–600/month for the VPS,
₹0 beyond that (Let's Encrypt SSL is free).

## 0. VPS and DNS

**Domain:** `kriyacore.in` — already registered.

**VPS:** pick one with an **India region**, so the data stays in the country:

| Provider | Region | Billing | Note |
|---|---|---|---|
| DigitalOcean | Bangalore (BLR1) | USD | Most documentation; 2–5% forex on an Indian card |
| Linode / Akamai | Mumbai | USD | Comparable; same forex caveat |
| E2E Networks | Mumbai / Delhi / Bengaluru | **INR + GST invoice** | Indian company; pick if the rupee invoice matters for books |

1 vCPU / 2GB RAM is ample for two gyms. This is a residency choice, not a
legal one — DPDP uses a blacklist model and no country is currently
restricted — but it also removes ~150ms of latency versus Singapore, and
future-proofs you against RBI payment-data rules if you add Razorpay.

**DNS — do this first, it takes the longest to propagate.** At your
registrar, once you have the VPS's IP:

| Type | Name | Value |
|---|---|---|
| A | `@` | your VPS IP |
| A (or CNAME to `@`) | `www` | your VPS IP |

Both are needed — `deploy/nginx.conf` and the certbot command cover
`kriyacore.in` and `www.kriyacore.in`, and certbot validates by fetching over
HTTP, so a name that doesn't resolve fails the whole command rather than just
that one name.

Check propagation before moving on:
```bash
dig +short kriyacore.in
dig +short www.kriyacore.in
```
Both should print your VPS IP. Minutes to a few hours.

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
CREATE DATABASE kriyacore;
CREATE USER kriyacore WITH PASSWORD 'pick-a-strong-password-here';
GRANT ALL PRIVILEGES ON DATABASE kriyacore TO kriyacore;
ALTER DATABASE kriyacore OWNER TO kriyacore;
\q
```

## 4. Deploy the code

```bash
cd /home/deploy
git clone https://github.com/ayudh-bulb666/KriyaCore.git kriyacore
cd kriyacore

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
sudo cp deploy/kriyacore.service /etc/systemd/system/kriyacore.service
sudo systemctl daemon-reload
sudo systemctl enable --now kriyacore
sudo systemctl status kriyacore   # should say "active (running)"
```

## 6. Nginx + free SSL

```bash
sudo cp deploy/nginx.conf /etc/nginx/sites-available/kriyacore
sudo ln -s /etc/nginx/sites-available/kriyacore /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

sudo certbot --nginx -d kriyacore.in -d www.kriyacore.in
```
The config already has `kriyacore.in` in it — nothing to edit. Both names are
requested so the certificate covers `www` too; make sure both DNS records from
step 0 resolve first, or certbot fails the whole request.
Certbot edits the Nginx config automatically to add HTTPS + a redirect from
HTTP, and sets up auto-renewal. Your site is now live at
`https://kriyacore.in`.

## 7. Nightly backups (Railway gives you this for free — this replaces it)

```bash
chmod +x deploy/backup_db.sh
crontab -e
```
Add this line:
```
30 2 * * * /home/deploy/kriyacore/deploy/backup_db.sh >> /home/deploy/backup.log 2>&1
```

## 7b. Automatic WhatsApp expiry reminders (optional)

Only matters if you've set `CRON_SECRET` in `.env` and turned on auto-reminders
for a gym under WhatsApp → Settings. Add a second crontab line — this one
hits the app itself over HTTP, so it works the same whether you're on this
VPS or on Railway (swap the URL for your Railway domain and point Railway's
own Cron trigger at the same curl command instead of using crontab there):

```
0 9 * * * curl -s -X POST https://kriyacore.in/internal/cron/whatsapp-reminders -H "X-Cron-Secret: $(grep CRON_SECRET /home/deploy/kriyacore/.env | cut -d= -f2)" >> /home/deploy/whatsapp-cron.log 2>&1
```

Runs daily at 9 AM. Test it manually first:
```bash
curl -X POST https://kriyacore.in/internal/cron/whatsapp-reminders \
  -H "X-Cron-Secret: <same value as CRON_SECRET in .env>"
```
A working response looks like `{"ok": true, "gyms_processed": N, "results": [...]}`.

## 8. Creating the database schema, and first login

The app no longer builds its own tables at startup — the schema is owned by
Alembic migrations, so it changes only when you tell it to. On a brand-new
database, run these once:

```bash
cd /home/deploy/kriyacore && source venv/bin/activate
export FLASK_APP=run.py
flask db upgrade      # creates every table
flask create-admin    # prompts for your email and a password
```

`create-admin` makes exactly one account — a platform admin, no gym, no
demo data. Log in at `https://kriyacore.in/login`, then create the real gyms
from the **operator panel**. That's the live path.

**Do not run `flask seed` on a real instance.** It exists for demos and
local work, and it creates the fictional PowerFit Mumbai with ten made-up
members plus the well-known `admin@kriyacore.com` / `admin123` login that is
public in this repo. Fine on a laptop, wrong on a server your clients use.

On an **existing** database that predates migrations and already has its
tables, don't run `upgrade` — tell Alembic the schema is already current,
or it will try to create tables that exist and fail:

```bash
flask db stamp head
```

### Changing the schema afterwards

After editing a model, generate a migration and commit it with the code:

```bash
flask db migrate -m "Add whatever you added"
```

**Read the generated file in `migrations/versions/` before committing it.**
Autogenerate reliably catches new tables and new columns, but it sees a
renamed column as a drop plus an add — which silently discards that
column's data. Renames need editing by hand.

Deploys apply migrations automatically (`deploy/deploy.sh`), after taking a
backup. To roll one back: `flask db downgrade`. To see where a database
stands: `flask db current`.

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
Go to **WhatsApp Manager → Message Templates → Create Template**. KriyaCore expects the template **name** to exactly match its internal message type — create these two (category: **Utility**, since both are transactional, not promotional):

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
On the VPS, add the same three lines to `.env` instead (see `deploy/.env.example`) and `sudo systemctl restart kriyacore`.

Then in KriyaCore: make sure the test member's phone number is one you added as a verified recipient in step (c), opt them in on their member page, and send a reminder. If Meta's approval hasn't landed yet, or the recipient isn't verified, the message log will show exactly why — Meta's own error text comes straight through, e.g. "template not found" or "recipient phone number not in allowed list."

## A. Testing on a public URL before there's a VPS

You can put the app on the internet from this laptop with a Cloudflare
tunnel — real HTTPS, no VPS, no port forwarding, works behind a router. It's
how you test with the Face ID hardware before committing to a server, since
the terminal needs a public HTTPS endpoint to POST scan events to.

**Run the app in public mode first. This matters.**

```bash
./deploy/run_public.sh
```

Never expose `python3 run.py` — that's the Flask dev server with debug on,
which serves Werkzeug's interactive debugger: a Python console on a web page.
On a public URL that is remote code execution on your machine. `run_public.sh`
starts gunicorn with `FLASK_ENV=production`, so the debugger is off, session
cookies are `Secure`, and the seeded credentials stop being printed on the
login page.

It also generates `.secret_key.local` on first run — a persistent signing key,
git-ignored — so logins survive a restart and the repo's placeholder key never
signs a real session.

**Then open the tunnel** (in a second terminal):

```bash
cloudflared tunnel --url http://127.0.0.1:8000
```

It prints a `https://<random-words>.trycloudflare.com` URL. That's live
immediately, no Cloudflare account needed. The URL changes every time you
restart the tunnel — fine for your own testing, worth knowing before you give
it to a hardware vendor to configure.

Two things to expect:

- **Log in through the tunnel URL, not `localhost:8000`.** In production mode
  the session cookie is `Secure`, so a browser will refuse to keep it over
  plain `http://`. You'll appear to log in and immediately bounce back.
- **The site is up only while your laptop is on** and both processes are
  running.

### Putting it on kriyacore.in

When you want the real domain instead of a random URL, you need a named
tunnel, which needs a Cloudflare account:

1. Create a free Cloudflare account and add `kriyacore.in` to it.
2. Cloudflare gives you two nameservers — set those at your registrar,
   replacing the current ones. This takes anywhere from minutes to a day for
   a `.in`, so start it early.
3. Then:

```bash
cloudflared tunnel login                      # opens a browser to authorise
cloudflared tunnel create kriyacore
cloudflared tunnel route dns kriyacore kriyacore.in
cloudflared tunnel run --url http://127.0.0.1:8000 kriyacore
```

Note this replaces your registrar's nameservers with Cloudflare's. Once you
move to a VPS you can either keep Cloudflare as your DNS (point an A record
at the VPS and drop the tunnel) or move the nameservers back — both work, but
it's a decision to make deliberately rather than discover.

## B. Deploying to Render + Supabase

The alternative to the VPS above: Render runs the app, Supabase runs Postgres.
Faster to stand up, no server to maintain, and roughly the same money — but
read the four gotchas at the end before you rely on it.

### 1. Supabase — create the database

1. [supabase.com](https://supabase.com) → New project.
2. **Region: Singapore.** Not Mumbai, even though Mumbai is closer to your
   users. Render has no India region, so the app runs in Singapore; putting
   the database in Mumbai means every single SQL query crosses the Bay of
   Bengal. The business dashboard alone runs a dozen queries per page — that
   is half a second of pure latency added to one page load. Co-locating both
   in Singapore is materially faster than splitting them.
3. Save the database password somewhere safe. Supabase shows it once.
4. Settings → Database → **Connection pooling** → copy the **session mode**
   URI (port `5432`). Use the pooler URI, not the direct one: the direct
   connection is IPv6-only on newer projects and Render will not reach it.

### 2. Render — deploy the app

Dashboard → **New → Blueprint** → pick this repo. It reads `render.yaml`,
which already sets the plan, region, build and start commands, the migration
step, and the Python version.

Then set the secrets it deliberately does not contain (`sync: false` means
"ask me"), under the service's Environment tab:

| Variable | Value |
|---|---|
| `DATABASE_URL` | the Supabase pooler URI from step 1 |
| `CRON_SECRET` | `python3 -c "import secrets; print(secrets.token_hex(32))"` |
| `SENTRY_DSN` | optional |
| `WHATSAPP_*` | once Meta approves your templates |
| `MAIL_*` | optional |

`SECRET_KEY` is generated by Render automatically. **Write it down.** Keep the
same value if you ever migrate, or every user is signed out mid-move.

### 3. First boot

`flask db upgrade` runs automatically as the pre-deploy step, so the tables
exist as soon as the first deploy finishes. Create your login from the Render
shell (Dashboard → Shell):

```bash
flask create-admin
```

Then sign in at `https://kriyacore.in/login` and add the real gyms from the
operator panel. **Do not run `flask seed`** — see §8.

### 4. Domain

Render → Settings → Custom Domain → add `kriyacore.in` and `www.kriyacore.in`.
Render issues the TLS certificate itself; you don't need certbot here.

**Set your DNS TTL to 300 seconds before you point it at Render.** If you
leave it at the registrar default, a later move to a VPS has a day-long tail
where some users still reach the old host.

### 5. WhatsApp reminders

Render's cron jobs are a separate paid service. Use a free external scheduler
([cron-job.org](https://cron-job.org), or a GitHub Actions schedule) to POST
daily:

```
POST https://kriyacore.in/internal/cron/whatsapp-reminders
Header: X-Cron-Secret: <your CRON_SECRET>
```

Deliberately a plain HTTP call, so the same schedule works unchanged if you
move to a VPS crontab.

### The four gotchas

**1. Timezone — the one that silently corrupts data.** Attendance timestamps
are server-local by design (see the `Attendance` model), and Render's servers
run UTC. Without `TZ=Asia/Kolkata` a member arriving at 6:06pm is recorded at
12:36pm, and "today's arrivals" rolls over at 5:30am instead of midnight.
`render.yaml` sets it — just don't remove it.

**2. Supabase Free has no backups.** This is member, billing and salary data.
`.github/workflows/backup-database.yml` takes an encrypted nightly dump and
verifies it opens. Set the `DATABASE_URL` and `BACKUP_PASSPHRASE` repository
secrets or it will not run.

**3. Free tiers sleep.** A Render free web service spins down after 15 minutes
idle — a 30–60 second wait for whoever opens the app first each morning. The
blueprint uses `starter` ($7/mo) for that reason. Supabase pauses a free
project after 7 days of no activity; two active gyms will never hit that, but
a long holiday could.

**4. One worker only.** Flask-Limiter keeps rate-limit counters in memory, so
a second instance gets its own counters and the login limit quietly doubles.
Don't scale the service without moving the limiter to Redis first.

### Moving to a VPS later

The app keeps no local state — logos are base64 in the database, invoice PDFs
are generated in memory, nothing touches disk. So the whole migration is:

```bash
pg_dump "$SUPABASE_URL" --no-owner --no-acl > kriyacore.sql
psql "$VPS_URL" < kriyacore.sql
```

then follow §0–§8 above, reuse the same `SECRET_KEY`, and repoint the DNS A
record. Roughly 90 minutes. The nightly backup workflow needs only its
`DATABASE_URL` secret changed.

## Future deploys

Push to GitHub, then on the VPS:
```bash
/home/deploy/kriyacore/deploy/deploy.sh
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
