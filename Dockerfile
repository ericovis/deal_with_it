FROM python:3.8
WORKDIR /app

RUN apt-get update \
    && apt-get install -y build-essential gcc g++ imagemagick cmake\
    && pip install pipenv

COPY Pipfile Pipfile.lock ./

RUN pipenv install --system

COPY . .
