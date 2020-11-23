# Fitness Store

This will be an e-commerce platform for delivering my fitness products and services.

## Features

Current features:

- Layout powered by [Every Layout](https://every-layout.dev/) and basic CSS
- `payments` Django app powered by Stripe with syncing from Django Admin dashboard
- `pages` Django app for making new markdown-powered pages
- `product` Django app with extendable Product abstract base class
- `users` Django app for authentication and authorization
- `feed` Django app for RSS feed
- `robots.txt` for search engines
- `sitemap.xml` for search engines; tracks `pages` and `products`

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

## Static files

Static files must be copied in a similar fashion if updated:

```
heroku run python manage.py collectstatic
```

## Media files

Currently no solution for user-uploaded media, but the app does not accept user-uploaded media.

## Users

To create a superuser:

```
heroku run python manage.py createsuperuser
```

This should ask for username (hidden), email, and password. You must head to Django Admin and then add the superuser's full name.

## Run Locally

To build containers and detach the console:

```
cd fitness-store
docker-compose up -d --build
```

To migrate database:

```
docker-compose exec web python manage.py migrate
```

To see running container logs:

```
docker-compose logs -f
```

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
