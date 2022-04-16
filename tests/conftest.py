import pytest
import os

from fastapi.testclient import TestClient
from src.app import app


HERE = os.path.dirname(os.path.abspath(__file__))


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)

@pytest.fixture
def base64_image() -> str:
    with open(os.path.join(HERE, 'fixtures/img.base64.txt'), 'r') as file:
        return file.read()

@pytest.fixture
def image_url() -> str:
    return 'https://emagalha.es/images/me.jpg'
