FROM ericovis/python3-10-dlib:latest

COPY Pipfile Pipfile.lock ./

RUN pipenv install --system

COPY . . 
