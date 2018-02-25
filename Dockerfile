FROM lambci/lambda:build-python3.6
WORKDIR /app
COPY requirements.txt .
RUN pip3 install -r requirements.txt
