# Fitness Store

This will be an e-commerce platform for delivering my fitness products and services.

## Features

Current features:

- Layout powered by [Every Layout](https://every-layout.dev/) and basic CSS
- Payments Django app powered by Stripe with syncing from Django Admin dashboard
- Pages Django app for making new markdown-powered pages
- Product Django app with extendable Product abstract base class
- Users Django app for authentication and authorization
- Feed Django app for RSS feed

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

To deploy:

- Get managed relational database
- Marketing
- Marketing
- Sales
- Marketing

My intention is to deploy on Heroku with a container.

## Run Locally

Navigate to the base folder and run `docker-compose up -d --build` to build containers and detach the console.

`docker-compose logs -f` to see running logs from containers.

## Tests

Short tests have been made for most of the project.

To run tests:

`docker-compose exec web pytest`

To see coverage report:

```
docker-compose exec web coverage run -m pytest
docker-compose exec web coverage report -m
```
