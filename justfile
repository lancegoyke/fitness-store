alias django := dev

# Development commands (unique port to avoid conflicts)
dev:
    uv run python app/manage.py runserver 8034

# Docker development
docker-dev:
    docker compose up -d --build

# Django management commands
manage *args:
    uv run python app/manage.py {{ args }}

# Database operations
migrate:
    uv run python app/manage.py migrate

makemigrations:
    uv run python app/manage.py makemigrations

setup-test-data:
    uv run python app/manage.py setup_test_data

createsuperuser:
    uv run python app/manage.py createsuperuser

# Testing and code quality
test:
    uv run pytest

test-coverage:
    uv run coverage run -m pytest
    uv run coverage report -m

lint:
    uv run ruff check

format:
    uv run ruff format

format-unsafe:
    uv run ruff check --fix --unsafe-fixes

pre-commit:
    pre-commit run --all-files

check: pre-commit lint test

# Heroku deployment
deploy:
    git push origin main

# Utility commands
shell:
    uv run python app/manage.py shell

# Run shell command with -c option
shell-c command:
    uv run python app/manage.py shell -c "{{ command }}"

collectstatic:
    uv run python app/manage.py collectstatic

# Start services (PostgreSQL on 5434, Redis on 6334)
services:
    docker compose up -d

stop-services:
    docker compose down

# Database shell (connects to local PostgreSQL)
db-shell:
    docker exec -it fitness_store_postgres psql -U postgres
