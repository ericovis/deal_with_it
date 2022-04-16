FROM ericovis/python3-10-dlib:latest as dev

COPY pyproject.toml poetry.lock ./

RUN poetry install

FROM ericovis/python3-10-dlib:latest as heroku

COPY src pyproject.toml poetry.lock ./

RUN poetry install --no-dev

CMD uvicorn src.app:app --host 0.0.0.0 --port $PORT
