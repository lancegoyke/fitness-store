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

...or in the Django admin at `http://localhost:8000/backside/users/user/`

## Local Development

### Environment Variables

Be sure to include [Stripe test mode publishable and secret keys](https://stripe.com/docs/test-mode) in `.env.dev`.
AWS credentials are optional when running locally because `settings/local.py` now sets
`AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` to dummy values if they are not
provided.
Set `GOOGLE_API_KEY` to enable Gemini-powered challenge summaries.

### Docker Compose

To build the database and cache containers:

```
cd fitness-store
docker-compose up -d --build
```

### Python & Django

To run the Django development server:

```
cd app
python manage.py runserver
```

To update the database schema:

```
cd app
python manage.py migrate
```

To see running container logs:

```
docker-compose logs -f
```

To test if a purchase gives a user the permission to view a purchased product, you'll need to [forward those events from Stripe to the local server](https://stripe.com/docs/webhooks/test) instead of the live server in production. Make sure to list the appropriate webhook URL for handling the triggered events.

```
stripe login
stripe listen --forward-to localhost:8000/payments/webhook/
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
