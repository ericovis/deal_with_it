FROM ericovis/python3-10-dlib:latest as dev
COPY pyproject.toml poetry.lock ./
RUN poetry install

FROM dev as heroku
COPY . .
RUN poetry install --no-dev --remove-untracked
CMD uvicorn src.app:app --host 0.0.0.0 --port $PORT --proxy-headers



