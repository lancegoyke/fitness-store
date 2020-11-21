# pull official base image
FROM python:3.8-alpine

# set work directory
WORKDIR /usr/src/app

# set environment variables
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# install psycopg2 dependencies
RUN apk update \
    && apk add postgresql-dev gcc python3-dev musl-dev

# install Pillow dependencies
RUN apk --no-cache add sudo jpeg-dev zlib-dev openjpeg-dev

# install dependencies
RUN pip install --upgrade pip
COPY ./requirements/base.txt .
COPY ./requirements/local.txt .
RUN pip install -r local.txt

# copy entrypoint.sh
COPY ./entrypoint.sh .

# copy project
COPY . .

# collect static files
RUN python3.8 manage.py collectstatic --noinput

# run entrypoint.sh
ENTRYPOINT ["/usr/src/app/entrypoint.sh"]