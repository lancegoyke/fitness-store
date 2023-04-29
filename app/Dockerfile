# pull official base image
FROM python:3.11.3

# set work directory
WORKDIR /usr/src/app

# set environment variables
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# install dependencies
RUN apt-get update && apt-get install -y \
    swig
RUN pip install --upgrade pip
COPY . .
RUN pip install -r ./config/requirements.txt

# collect static files
RUN python3 manage.py collectstatic --noinput
