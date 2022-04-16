def test_request_should_have_either_url_or_image_field(client):
    data = {"message": "bad request"}
    resp = client.post('/api', json=data)
    assert resp.status_code == 422
    assert resp.json()['detail'][0]['msg'] == 'An url or a base64 string must be passed.'

def test_request_cant_have_both_fields(client, base64_image, image_url):
    data = {"base64": base64_image, "url": image_url}
    resp = client.post('/api', json=data)
    assert resp.status_code == 422
    assert resp.json()['detail'][0]['msg'] == 'An url OR a base64 string must be passed, not both.'

def test_api_request_with_b64_image_as_param(client, base64_image):
    data = { "base64": base64_image }
    resp = client.post('/api', json=data)
    assert resp.status_code == 200

def test_api_request_with_url_as_param(client):
    data = { "url": "https://emagalha.es/images/me.jpg" }
    resp = client.post('/api', json=data)
    assert resp.status_code == 200
