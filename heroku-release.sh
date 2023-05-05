#!/bin/sh

python manage.py check --deploy --database default && pip freeze && python manage.py migrate
