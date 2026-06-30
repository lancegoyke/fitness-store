# Fitness Store

This is an e-commerce platform for delivering fitness products and services.

🌍 [Mastering Fitness](https://mastering.fitness/)

## Features

Current features:

- Layout powered by [Every Layout](https://every-layout.dev/) and basic CSS
- `payments` – powered by Stripe Checkout with information synced from Django Admin dashboard
- `pages` – markdown-powered pages (ex: [About page](https://mastering.fitness/about/))
- `products` – foundation of the shop with extendable Product abstract base class (ex: [30 Days of Fitness](https://mastering.fitness/programs/30-days-fitness-challenge/))
- `exercises` – exercise database with instruction videos and alternative suggestions ([Exercises](https://mastering.fitness/exercises/))
- `cardio` – self-generated cardio workouts ([Cardio](https://mastering.fitness/cardio/))
- `timer` - interval timer in JavaScript ([Timer](https://mastering.fitness/timer/))
- `users` – user authentication and authorization
- `feed` – RSS feed (ex: [Product Feed](https://mastering.fitness/feed/products/))
- `robots.txt` – for search engines
- `sitemap.xml` – for search engines (ex: [Sitemap](https://mastering.fitness/sitemap.xml))

## Tech

Current features:

- Docker and Docker Compose
- Redis for caching static pages
- PostgreSQL
- pytest
- custom User model

This is emulated in docker-compose.production.yml.

## Deployment

This app runs as a Docker Compose stack on a shared [Hetzner](https://www.hetzner.com/) box, managed by the [`deploy`](https://github.com/lancegoyke/deploy) CLI and fronted by a shared Caddy reverse proxy (automatic Let's Encrypt TLS). PostgreSQL and Redis run as their own containers; media stays on AWS S3 and email on AWS SES.

To deploy:

```
just deploy
```

This pushes `main` to GitHub, then builds, migrates, and restarts the stack on the box. Pushing to `main` also triggers a GitHub Actions deploy after CI passes. See [`docs/deploy-hetzner.md`](docs/deploy-hetzner.md) for the full runbook (one-time migration, DNS, backups, rollback).

## Database Migrations

Migrations run automatically on every deploy: the container entrypoint (`docker-entrypoint.sh`) runs `migrate` before starting gunicorn. To run one manually against production:

```
deploy shell -a fitness-store web "python manage.py migrate"
```

## Django Admin

The address of Django's admin backend has been changed from `/admin/` to `/backside/`.

## Static files

Static files are collected automatically on each deploy (the entrypoint runs `collectstatic`) and served by WhiteNoise. To collect them manually:

```
deploy shell -a fitness-store web "python manage.py collectstatic"
```

## Users

To create a superuser, open an interactive shell in the production web container and run the command there:

```
deploy shell -a fitness-store web
python manage.py createsuperuser
```

This should ask for username, email, and password.

You can edit the user's full name in the Django shell...

```
python manage.py shell
>>> from django.contrib.auth import get_user_model
>>> User = get_user_model()
>>> me = User.objects.get(username="<the username you typed")
>>> me.full_name = "<Desired Name>"
>>> me.save()
```

...or in the Django admin at `http://localhost:8034/backside/users/user/`

## Local Development

### Environment Variables

Copy `.env.example` to `.env` — it lists every variable with notes and safe dev
defaults, and most can be left blank locally. At minimum, set [Stripe test mode
keys](https://stripe.com/docs/test-mode), and point `SQL_PORT=5434` and
`REDIS_URL=redis://localhost:6334/0` at the dev containers (see Docker Compose below).

AWS credentials are optional when running locally because `settings/local.py` sets
`AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` to dummy values if they are not
provided.
Set `GOOGLE_API_KEY` to enable Gemini-powered challenge summaries. The optional
Meso features (AI agent, web push, Stripe billing) have their own `ANTHROPIC_API_KEY`
and `MESO_*` variables — all documented in `.env.example` and all no-ops when unset.

### Docker Compose

To start the Postgres (`:5434`) and Redis (`:6334`) containers:

```
just services
```

### Python & Django

To run the Django development server (on http://localhost:8034/):

```
just dev
```

To update the database schema:

```
just migrate
```

The `django-q2` cluster that runs scheduled/background tasks (invite sweeps, the
`reconcile_seats` billing sweep, async Meso agent jobs) is a separate process —
start it in its own terminal when you need it (locally you can instead set
`MESO_AGENT_RUN_SYNC=true` to run agent jobs inline):

```
just qcluster
```

To see running container logs:

```
docker compose logs -f
```

To test if a purchase gives a user the permission to view a purchased product, you'll need to [forward those events from Stripe to the local server](https://stripe.com/docs/webhooks/test) instead of the live server in production. Make sure to list the appropriate webhook URL for handling the triggered events.

```
stripe login
# One-time product purchases:
stripe listen --forward-to localhost:8034/payments/webhook/
# Meso subscription billing (separate endpoint + signing secret; run in its own terminal):
stripe listen --forward-to localhost:8034/meso/billing/webhook/
```

Once listening, you must trigger the event by performing the corresponding actions on your site or by using the Stripe CLI, e.g. `stripe trigger checkout.session.completed`.

## Tests

### Database Seeding

To seed the database with sample data from all apps:

```
python manage.py seed_database
```

Additional seeding options:

```
# Delete all existing data and reseed everything
python manage.py seed_database --delete

# Seed only challenges data
python manage.py seed_database --challenges-only

# Seed only products data
python manage.py seed_database --products-only

# Individual app commands
python manage.py seed_challenges
python manage.py seed_products
```

### Legacy Test Data

To setup test data (legacy command):

```
python manage.py setup_test_data
```

### Running Tests

Short tests have been made for most of the project.

To run tests:

```
pytest
```

To see coverage report:

```
coverage run -m pytest
coverage report -m
```
