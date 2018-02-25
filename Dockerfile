FROM amazonlinux:latest
WORKDIR /app
RUN yum install -y \
    gcc \
    gcc-c++ \
    cmake \
    git \
    ImageMagick \
    python36 \
    python36-devel \
    python36-pip
RUN mkdir -p dlib && \
    git clone -b 'v19.9' --single-branch https://github.com/davisking/dlib.git dlib/ && \
    cd  dlib/ && \
    python3 setup.py install --yes USE_AVX_INSTRUCTIONS

COPY requirements.txt .
RUN pip-3.6 install -r requirements.txt
COPY . .
