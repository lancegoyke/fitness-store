release: cd app && python manage.py migrate
web: gunicorn --chdir app config.wsgi:application
