# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a Django-based e-commerce platform for fitness products and services called "Mastering Fitness". The application uses a modular Django app structure with custom user authentication, Stripe payments, exercise database, challenges system, and content management.

## Development Commands

### Environment Setup
```bash
# Development with Docker
cd fitness-store
docker-compose up -d --build

# Development server (after Docker setup)
cd app
python manage.py runserver
```

### Database Operations
```bash
cd app
python manage.py migrate
python manage.py setup_test_data  # Setup test data
python manage.py createsuperuser
```

### Testing and Code Quality
```bash
# Run tests
pytest

# Test coverage
coverage run -m pytest
coverage report -m

# Code formatting and linting
ruff check
ruff format
```

### Production Deployment
```bash
# Deploy to Heroku
git push origin main

# Manual migrations on Heroku
heroku run python manage.py migrate
heroku run python manage.py collectstatic
```

## Project Architecture

### Directory Structure
- `app/` - Main Django application directory
- `app/config/` - Django settings and main URL configuration
- `app/store_project/` - Main project package containing all Django apps
- `app/manage.py` - Django management script

### Key Django Apps
- `users` - Custom user model with UUID primary keys, points system, and Stripe integration
- `products` - Abstract Product base class for extensible e-commerce (Programs, Books)
- `exercises` - Exercise database with videos and alternatives
- `challenges` - Fitness challenges system with records tracking
- `payments` - Stripe Checkout integration
- `pages` - Markdown-powered CMS pages
- `cardio` - Self-generated cardio workout system
- `notifications` - Email notification system
- `analytics` - Google Analytics integration

### Custom User Model
Located at `app/store_project/users/models.py:10`. Uses UUID primary keys, includes birthday, points system, sex field, and Stripe customer ID integration.

### Product System
Abstract base class at `app/store_project/products/models.py:39` with status fields (Public/Private/Draft), Stripe integration, and extensible for different product types (Programs, Books).

### Settings Configuration
- Base settings: `app/config/settings/base.py`
- Local development: `app/config/settings/local.py`
- Production: `app/config/settings/production.py`
- Test: `app/config/settings/test.py`

### Admin Interface
Django admin is accessible at `/backside/` (not `/admin/`) for security. The `/admin/` URL is a honeypot trap.

### Authentication
Uses django-allauth with email-only login, social authentication (Facebook, Google), and custom user display functions.

## Key Technologies
- Django 5.2+ with PostgreSQL database
- Redis for caching and sessions
- Stripe for payments
- AWS S3 for media storage
- WhiteNoise for static files
- docker-compose for development
- Heroku for production deployment
- pytest for testing
- ruff for code formatting and linting

## Environment Variables
Key variables needed in `.env.dev` for local development:
- `STRIPE_PUBLISHABLE_KEY` and `STRIPE_SECRET_KEY` (test mode)
- Database credentials for PostgreSQL
- Social auth keys for Facebook and Google
- AWS credentials for S3 storage

## Testing Strategy
- Test files located in `tests/` subdirectories within each app
- Uses pytest with Django integration
- Factory Boy for test data generation
- Coverage reporting available via `coverage` package
