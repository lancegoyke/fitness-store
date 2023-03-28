# pull official base image
FROM python:3.11.2

# set work directory
WORKDIR /usr/src/app

# install m2crypto dependencies
RUN apt-get update && apt-get install -y \
    swig \
    libssl-dev \
    libffi-dev \
    build-essential \
    gcc \
    python3-dev

# set environment variables
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# install dependencies
RUN pip install --upgrade pip
COPY ./requirements/base.txt .
RUN pip install -r base.txt

# copy project
COPY . .

# collect static files
RUN python3 manage.py collectstatic --noinput
