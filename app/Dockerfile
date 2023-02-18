# pull official base image
FROM python:3.10.10

# set work directory
WORKDIR /usr/src/app

# set environment variables
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# install dependencies
RUN pip install --upgrade pip
COPY ./requirements/base.txt .
COPY ./requirements/local.txt .
RUN pip install -r local.txt

# copy project
COPY . .

# collect static files
RUN python3 manage.py collectstatic --noinput
