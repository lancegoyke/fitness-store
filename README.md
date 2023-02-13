# Fitness Store

This is an e-commerce platform for delivering fitness products and services.

🌍 [Mastering Fitness](https://mastering.fitness/)

## Features

Current features:

- Layout powered by [Every Layout](https://every-layout.dev/) and basic CSS
- `payments` – powered by Stripe Checkout with information synced from Django Admin dashboard
- `pages` – markdown-powered pages (ex: [About page](https://mastering.fitness/about/))
- `product` – foundation of the shop with extendable Product abstract base class (ex: [30 Days of Fitness](https://mastering.fitness/programs/30-days-fitness-challenge/))
- `exercises` – exercise database with instruction videos and alternative suggestions (ex: [Exercises](https://mastering.fitness/exercises/))
- `cardio` – self-generated cardio workouts (ex: [Cardio](https://mastering.fitness/cardio/))
- `users` – user authentication and authorization
- `feed` – RSS feed (ex: [Product Feed](https://mastering.fitness/feed/products/))
- `robots.txt` – for search engines
- `sitemap.xml` – for search engines (ex: [Sitemap](https://mastering.fitness/sitemap.xml))

## Tech

Current features:

- Docker
- Docker Compose
- Nginx
- PostgreSQL
- pytest
- custom User model

This is emulated in docker-compose.prod.yml.

## Deployment

This app runs in a container on Heroku with a heroku-postgresql database addon.

To deploy:

```
git push heroku master
```

## Database Migrations

If a new feature requires changes to the database schema, it may be taken care of in `heroku.yml` Release phase. This is untested.

If the release command does not work, run it manually:

```
heroku run python manage.py migrate
```

## Django Admin

The address of Django's admin backend has been changed from `/admin/` to `/backside/`.

## Static files

Static files must be copied in a similar fashion if updated:

```
heroku run python manage.py collectstatic
```

## Users

To create a superuser:

```
heroku run python manage.py createsuperuser
```

This should ask for username, email, and password.

You can edit the user's full name in the Django shell...

```
docker-compose exec web python manage.py shell
>>> from django.contrib.auth import get_user_model
>>> User = get_user_model()
>>> me = User.objects.get(username="<the username you typed")
>>> me.full_name = "<Desired Name>"
>>> me.save()
```

...or in the Django admin at `http://localhost:8000/backside/users/user/`

## Local Development

Be sure to include test publishable and secret keys from Stripe in `.env.dev`.

To build containers and detach the console:

```
cd fitness-store
docker-compose up -d --build
```

To update the database schema:

```
docker-compose exec web python manage.py migrate
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

To setup test data:

```
docker-compose exec web python manage.py setup_test_data
```

Short tests have been made for most of the project.

To run tests:

```
docker-compose exec web pytest
```

To see coverage report:

```
docker-compose exec web coverage run -m pytest
docker-compose exec web coverage report -m
```
