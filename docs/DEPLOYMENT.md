# Deploying NyaySutra AI online

This puts NyaySutra on a server with its own web address, HTTPS and an uptime alert. The database (Supabase,
Mumbai) stays where it is; only the app moves off your laptop.

> **Status:** the Docker files below are written and reviewed but have **not yet been built or run** (Docker is not
> installed on the development machine). Expect to fix small issues the first time you build.

## 1. What you need

| Item | Recommendation | Cost |
|---|---|---|
| A server | Ubuntu 22.04/24.04, **2 vCPU, 4 GB RAM or more** (the app uses about 1.5 GB), 20 GB disk | Oracle Cloud Always Free VM (free; reportedly 2 cores / 12 GB since June 2026; may be "out of capacity"), or a small paid VM in Mumbai/Bangalore (~₹1,000/month) |
| A web address | Your own domain, or a free subdomain from DuckDNS (`yourname.duckdns.org`) | free – ₹1,000/year |
| An uptime monitor | UptimeRobot or Better Stack free plan | free |
| Your `.env` | The same file you use now, with the changes in step 3 | — |

You create these accounts yourself; Claude cannot create accounts or enter passwords.

## 2. Prepare the server

1. Create the VM. Open ports **22, 80 and 443** in its firewall / security list (Oracle: VCN security list *and*
   the VM's own firewall: `sudo iptables -I INPUT -p tcp -m multiport --dports 80,443 -j ACCEPT`).
2. Point your domain's **A record** at the server's public IP (DuckDNS: enter the IP on duckdns.org).
3. Install Docker:
   ```bash
   curl -fsSL https://get.docker.com | sudo sh
   sudo usermod -aG docker $USER   # then log out and back in
   ```

## 3. Copy the project and set the configuration

Copy the whole project folder **from this machine** (it contains the 101 India Code acts imported at run time, which
are not in Git):

```bash
rsync -av --exclude .git --exclude __pycache__ --exclude data/nyaysutra.db* --exclude data/uploads \
  ./ user@SERVER_IP:~/nyaysutra/
```

(Alternative: `git clone` the repository on the server and re-import the acts there with
`python scripts/import_central_acts.py` — slower.)

On the server, edit `~/nyaysutra/.env` (copy your current one; keep all Supabase and Ollama keys) and **add or
change**:

```ini
PUBLIC_URL=https://nyaysutra.example.com
ALLOWED_HOSTS=nyaysutra.example.com
```

Create `~/nyaysutra/deploy/.env`:

```ini
SITE_ADDRESS=nyaysutra.example.com
```

In the **Supabase dashboard → Authentication → URL Configuration**, set *Site URL* to `https://nyaysutra.example.com`
and add `https://nyaysutra.example.com/**` to *Redirect URLs*. Without this, sign-up confirmation and password-reset
e-mails would link back to `127.0.0.1`.

## 4. Start it

```bash
cd ~/nyaysutra/deploy
docker compose up -d --build
docker compose logs -f app      # first start downloads the search index from Supabase (~1–2 minutes)
```

Check: open `https://nyaysutra.example.com/api/health` — it should show `{"ok": true, "database": true,
"index_loaded": true}`. Then open the site and sign in.

What runs: the **app** container (not reachable directly) and **Caddy**, which obtains and renews the HTTPS
certificate automatically and forwards traffic to the app. The app trusts Caddy's forwarded headers
(`TRUSTED_PROXY_IPS=*`) only because its port is not published.

## 5. Uptime monitoring

In UptimeRobot (or Better Stack) create an **HTTP(s) monitor** for `https://nyaysutra.example.com/api/health`,
every 5 minutes, with e-mail alerts. `/api/health` needs no sign-in, returns no data, does not call the AI model, and
returns HTTP 503 if the database is unreachable. These regular checks also count as database activity, which keeps
the Supabase free project from being paused after 7 quiet days.

## 6. Updating

```bash
# from this machine
rsync -av --exclude .git --exclude __pycache__ --exclude data/nyaysutra.db* --exclude data/uploads ./ user@SERVER_IP:~/nyaysutra/
# on the server
cd ~/nyaysutra/deploy && docker compose up -d --build
```

Database migrations (`supabase/migrations/*`) are applied to Supabase separately, before deploying code that needs
them; the app refuses to start against an older schema.

## 7. Other hosts

* **Google Cloud Run (Mumbai, asia-south1):** build the same image, deploy with **2 GiB memory**, 1 vCPU, port 8000,
  and the `.env` values as environment variables / secrets; `TRUSTED_PROXY_IPS=*` (Cloud Run is the proxy). With
  minimum instances 0 it is free for light use but the first request after idle takes about a minute (models and
  index load); minimum instances 1 avoids that and costs roughly ₹2,500+/month. Full judgment texts fetched on
  demand are lost when an instance stops (they are fetched again when needed).
* **Hugging Face Spaces:** Docker Spaces now appear to need a paid plan; not recommended.

## Settings reference (production)

| Setting | Value | Why |
|---|---|---|
| `PUBLIC_URL` | `https://your-domain` | links in e-mails; marks session cookies Secure |
| `ALLOWED_HOSTS` | `your-domain` | blocks requests for other host names |
| `APP_HOST` | `0.0.0.0` (set by compose) | listen inside the container |
| `TRUSTED_PROXY_IPS` | `*` (set by compose) | real client IPs for rate limits and the audit log |
| `RETENTION_ENABLED` | `true` (default) | daily data-retention clean-up (docs/DATA_RETENTION.md) |
| `WEB_SEARCH_ENABLED` | `true` (default) | note: the Ollama web-search allowance is limited; heavy use returns HTTP 429 until it resets or the plan is upgraded |
