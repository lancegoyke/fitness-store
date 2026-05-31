# Deploying Mastering Fitness on Hetzner

This app runs on a shared Hetzner box managed by the [`deploy`](https://github.com/lancegoyke/deploy)
CLI. The box already hosts other apps behind a single shared **Caddy** reverse
proxy (auto Let's Encrypt TLS) on the external `proxy` Docker network. This app
ships its **own** Postgres 17 + Redis 7 containers; media stays on **AWS S3** and
email on **AWS SES** (unchanged from Heroku).

## Architecture

```
Internet ──443──▶ shared Caddy (caddy:2.10.2, /opt/caddy)
                     │ reverse_proxy fitness-store-web:8000  (proxy network)
                     ▼
        ┌─────────── /srv/fitness-store (docker compose: fitness-store) ──────────┐
        │  web (gunicorn, WhiteNoise static)  ── postgres:17-alpine (volume)       │
        │                                     ── redis:7-alpine (AOF volume)        │
        └──────────────────────────────────────────────────────────────────────────┘
                     │ media → AWS S3 (bucket "masterfit")
                     │ email → AWS SES
```

Repo files that drive this:

| File | Purpose |
|------|---------|
| `Dockerfile` | builds the web image (uv → gunicorn, non-root) |
| `docker-entrypoint.sh` | runs `migrate` + `collectstatic` before gunicorn |
| `docker-compose.production.yml` | web + postgres + redis; joins `proxy` net as `fitness-store-web` |
| `caddy/fitness-store.caddy` | Caddy site (placeholder `yourdomain.com` → real domain) |
| `.deployment.env` | deploy-tool overrides (COMPOSE_FILE, APP_USER, backup config) |
| `.env.example` | env contract; `deploy repo create` fills SECRET_KEY/DB_*/ALLOWED_HOSTS |

## One-time migration from Heroku (app `mastering-fitness`)

Domain stays `mastering.fitness`, so **no third-party reconfiguration is needed**
(Stripe webhook, Google/Facebook OAuth, SES, reCAPTCHA, the Sites row, and
`DOMAIN_URL` are all keyed on the domain, not the host). Only the DNS records move.

### 0. Prerequisites

- `deploy` CLI on PATH locally (it is); `ssh hetzner` works.
- `heroku login` done locally (so we can pull config + the DB).
- The box is provisioned (Caddy up at `/opt/caddy`, `/opt/deploy` present, `gh`
  authed on the box).
- A few minutes of write-freeze on Heroku during the final dump.

### 1. Provision the app on the box

```bash
deploy repo create --repo lancegoyke/fitness-store --domain mastering.fitness
```

This adds a GitHub deploy key, clones to `/srv/fitness-store`, creates the
`fitness_store` system user, scaffolds `/srv/fitness-store/.env` (auto-generated
`SECRET_KEY`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `ALLOWED_HOSTS=mastering.fitness`),
and installs + reloads the Caddy site. (DNS still points at Heroku, so no cert yet.)

### 2. Populate the server `.env` from Heroku config

The app raises at import if AWS/Stripe/OAuth keys are missing, so fill these
**before** the first deploy. Copy every app secret from Heroku **except** the
infra vars that the compose file owns.

Copy these (values from `heroku config:get <VAR> -a mastering-fitness`):

```
SECRET_KEY                 # overwrite the generated one to keep signed tokens valid
AWS_ACCESS_KEY_ID  AWS_SECRET_ACCESS_KEY
AWS_SES_ACCESS_KEY_ID  AWS_SES_SECRET_ACCESS_KEY
AWS_SES_REGION_NAME  AWS_SES_REGION_ENDPOINT  AWS_SES_CONFIGURATION_SET
STRIPE_PUBLISHABLE_KEY  STRIPE_SECRET_KEY  STRIPE_ENDPOINT_SECRET
FB_APP_ID  FB_SECRET_KEY
GOOGLE_CLIENT_ID  GOOGLE_CLIENT_SECRET  GOOGLE_API_KEY
GOOGLE_ANALYTICS_GTAG_PROPERTY_ID
G_RECAPTCHA_SITE_KEY  G_RECAPTCHA_SECRET_KEY  G_RECAPTCHA_ENDPOINT
CORS_ALLOWED_ORIGINS  DOMAIN_URL  SENTRY_DSN
```

Do **not** copy: `DATABASE_URL`, `REDIS_URL` (compose provides them),
`DJANGO_ALLOWED_HOSTS`, `DJANGO_SETTINGS_MODULE`, `DEBUG`, `ENVIRONMENT`
(set in `docker-compose.production.yml`), `SCOUT_KEY`/`SCOUT_MONITOR`/`SCOUT_LOG_LEVEL`
(Scout is off unless re-enabled), `CRYPTOGRAPHY_DONT_BUILD_RUST` (unused).
Keep the generated `DB_NAME`/`DB_USER`/`DB_PASSWORD`.

Set values with `deploy config set -a fitness-store KEY=VALUE` (or edit
`/srv/fitness-store/.env` directly via sudo). Confirm with `deploy config list -a fitness-store`.

### 3. Migrate the Postgres data (≈14 MB, Postgres 17)

```bash
# On the box, bring up just the data stores so the DB exists.
ssh hetzner 'cd /srv/fitness-store && \
  docker compose -f docker-compose.production.yml up -d postgres redis'

# Locally: capture + download a final dump (freeze writes first to be safe).
heroku maintenance:on -a mastering-fitness
heroku pg:backups:capture -a mastering-fitness
heroku pg:backups:download -a mastering-fitness   # -> latest.dump (custom format)

# Ship it to the box and restore into the app DB (drop+recreate objects,
# ignore Heroku-only roles/ACLs).
scp latest.dump hetzner:/tmp/heroku.dump
ssh hetzner 'cd /srv/fitness-store && \
  DB=$(grep ^DB_NAME= .env | cut -d= -f2) && \
  U=$(grep ^DB_USER= .env | cut -d= -f2) && \
  docker compose -f docker-compose.production.yml exec -T postgres \
    pg_restore --no-owner --no-acl --clean --if-exists -U "$U" -d "$DB" < /tmp/heroku.dump ; \
  rm -f /tmp/heroku.dump'
```

A few `does not exist, skipping` notices during `--clean` of a fresh DB are
expected. Spot-check row counts afterward (e.g. `deploy shell -a fitness-store web
"python manage.py shell -c 'from django.contrib.auth import get_user_model as g; print(g().objects.count())'"`).
The `django_site` row already holds `mastering.fitness` (it came from the dump),
so allauth/order emails build correct links with no extra step.

### 4. First deploy

```bash
deploy deploy -a fitness-store        # git pull (no-op) -> build -> migrate -> up -d
deploy status -a fitness-store        # web should be (healthy)
```

The web container's healthcheck performs an internal HTTPS-style GET, so a
`healthy` status confirms the app serves 200 **before** any public traffic.

### 5. DNS cutover (the only outward-facing change)

1. Lower the TTL on `mastering.fitness` and `www.mastering.fitness` (ideally a
   day ahead) so the flip propagates fast.
2. Get the box IPv4: `ssh hetzner 'curl -s ifconfig.me'`.
3. At the DNS provider, replace the Heroku targets with the box IP:
   ```
   mastering.fitness.       A   <box-ipv4>
   www.mastering.fitness.   A   <box-ipv4>
   ```
   (Remove the old `herokudns.com` ALIAS/CNAME. DNS-only — no Cloudflare proxy.)
4. Watch Caddy issue the cert on first request:
   ```bash
   ssh hetzner 'sudo docker compose -f /opt/deploy/infra/caddy/docker-compose.yml \
     logs -f caddy | grep -i "obtain\|certificate"'
   ```

### 6. Verify, then stand down Heroku

- `curl -fsSI https://mastering.fitness/ | grep -i strict-transport-security`
- Log in (email + Google + Facebook), load an exercise/program page, submit the
  contact form (reCAPTCHA), and run a Stripe **test**-mode checkout to confirm the
  webhook reaches `https://mastering.fitness/payments/webhook/` and the
  confirmation email sends via SES.
- Leave Heroku in maintenance for a stability window, then scale down / delete the
  app and its add-ons once you're confident. Keep `heroku maintenance:on` until then.

## Day-2 operations

- **Deploy:** `just deploy` (push to GitHub, then `deploy deploy -a fitness-store`),
  or push to `main` and let the **Deploy** GitHub Action run after CI passes.
- **Logs:** `just prod-logs` (or `deploy logs -a fitness-store web -f`).
- **Backups:** `just prod-backup` on demand. Schedule a nightly cron on the box:
  ```cron
  0 3 * * * PROJECT_DIR=/srv/fitness-store bash /opt/deploy/infra/scripts/project-backup.sh >> /var/log/backups/fitness-store.log 2>&1
  ```
  For off-site copies, configure an rclone remote on the box and set
  `RCLONE_REMOTE=` in `.deployment.env`. Restore with
  `deploy backup restore -a fitness-store <dump>`.

## GitHub Actions auto-deploy

`.github/workflows/deploy.yml` triggers after the **Django CI** workflow succeeds
on `main` and SSHes in to run the on-box deploy. Add repo secrets:

- `DEPLOY_HOST` — box public IP / hostname (not the `hetzner` SSH alias)
- `DEPLOY_USER` — a **docker-capable** box user (e.g. `lance`); the deploy builds
  images on the box, which the scoped static-only `deploy` CI user cannot do
- `DEPLOY_SSH_KEY` — private half of a **dedicated** CI keypair whose public half
  is in that user's `~/.ssh/authorized_keys` (rotate independently of your personal key)

## Rollback

The deploy is a rebuild from `main`; to roll back, revert the commit (or
`git -C /srv/fitness-store checkout <good-sha>`) and `deploy deploy -a fitness-store`
again. Data is safe in the `postgres_data` volume; restore a dump with
`deploy backup restore` if needed.
