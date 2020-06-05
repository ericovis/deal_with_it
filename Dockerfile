FROM python:3.8

WORKDIR /app

RUN apt-get update \
    && apt-get install -y build-essential gcc g++ imagemagick cmake

RUN pip install pipenv dlib==19.19.0

COPY Pipfile Pipfile.lock ./

RUN pipenv install --system

COPY . .
