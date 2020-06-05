import os 
import pytest
import json

here = os.path.dirname(os.path.abspath(__file__))


with open(os.path.join(here, 'image.txt'), 'r') as f:
    IMAGE = f.read()


def test_request_should_have_either_url_or_image_field(client):
    data = {"message": "bad request"}
    resp = client.post('/api', json=data)
    assert resp.status_code == 400


def test_api_request_with_b64_image_as_param(client):
    data = { "image": IMAGE }
    resp = client.post('/api', json=data)
    assert resp.status_code == 200


def test_api_request_with_b64_url_as_param(client):
    data = { "url": "https://emagalha.es/images/me.jpg" }
    resp = client.post('/api', json=data)
    assert resp.status_code == 200

    