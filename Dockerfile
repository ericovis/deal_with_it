FROM ericovis/python3-10-dlib:latest

COPY pyproject.toml poetry.lock ./

RUN poetry config virtualenvs.create false && poetry install

COPY . . 
