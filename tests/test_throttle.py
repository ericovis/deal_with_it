"""Admission control: the rate limit, the queue cap, and how each surface
reports them."""

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from src import jobs, throttle
from src.config import Settings, get_settings
from tests import support
from tests.test_ui import submit

URL = 'https://example.test/a.png'


@pytest.fixture
def limits(monkeypatch):
    """Settings with a rate limit and queue cap small enough to hit."""

    def install(**overrides):
        settings = Settings(**{'rate_limit': 3, 'rate_window': 60, 'max_queue_depth': 2,
                               **overrides})
        monkeypatch.setattr(throttle, 'get_settings', lambda: settings)
        monkeypatch.setattr(jobs, 'get_settings', lambda: settings)
        get_settings.cache_clear()
        return settings

    return install


class TestRateLimit:
    def test_allows_up_to_the_limit(self, sync_queue, limits):
        limits()
        for _ in range(3):
            throttle.check_rate('1.2.3.4')

    def test_refuses_the_next_one_with_a_retry_after(self, sync_queue, limits):
        limits()
        for _ in range(3):
            throttle.check_rate('1.2.3.4')
        with pytest.raises(throttle.TooManyRequests) as info:
            throttle.check_rate('1.2.3.4')
        assert 1 <= info.value.retry_after <= 60
        assert info.value.status_code == 429

    def test_counts_per_client(self, sync_queue, limits):
        limits()
        for _ in range(3):
            throttle.check_rate('1.2.3.4')
        throttle.check_rate('5.6.7.8')

    def test_the_counter_expires_with_the_window(self, sync_queue, limits):
        settings = limits()
        throttle.check_rate('1.2.3.4')
        key = next(iter(sync_queue.connection.keys('dwi:rate:*')))
        assert 0 < sync_queue.connection.ttl(key) <= settings.rate_window

    def test_zero_disables_it(self, sync_queue, limits):
        limits(rate_limit=0)
        for _ in range(50):
            throttle.check_rate('1.2.3.4')

    def test_a_broken_broker_does_not_refuse_anyone(self, sync_queue, limits, monkeypatch):
        """The enqueue right after this fails loudly on its own."""
        limits()

        def boom(*args, **kwargs):
            raise RedisConnectionError('down')

        monkeypatch.setattr(sync_queue.connection, 'pipeline', boom)
        throttle.check_rate('1.2.3.4')


class TestQueueCap:
    def test_refuses_when_the_backlog_is_deep_enough(self, async_queue, limits, stub_task):
        limits()
        stub_task(support.SUCCESS)
        throttle.check_queue_depth()
        jobs.enqueue({'url': URL, 'base64': None})
        jobs.enqueue({'url': URL, 'base64': None})
        with pytest.raises(throttle.QueueFull) as info:
            throttle.check_queue_depth()
        assert info.value.status_code == 503

    def test_running_jobs_do_not_count(self, async_queue, limits, stub_task, drain):
        limits()
        stub_task(support.SUCCESS)
        jobs.enqueue({'url': URL, 'base64': None})
        jobs.enqueue({'url': URL, 'base64': None})
        drain()
        throttle.check_queue_depth()

    def test_zero_disables_it(self, async_queue, limits, stub_task):
        limits(max_queue_depth=0)
        stub_task(support.SUCCESS)
        for _ in range(5):
            jobs.enqueue({'url': URL, 'base64': None})
        throttle.check_queue_depth()


class TestTheApi:
    def test_a_flood_gets_a_429_with_retry_after(self, client, limits):
        limits()
        for _ in range(3):
            assert client.post('/api/jobs', json={'url': URL}).status_code == 202
        response = client.post('/api/jobs', json={'url': URL})
        assert response.status_code == 429
        assert response.headers['retry-after'].isdigit()
        assert 'Too many submissions' in response.json()['detail']

    def test_a_full_queue_is_a_503(self, client, async_queue, limits, stub_task):
        limits(rate_limit=0)
        stub_task(support.SUCCESS)
        client.post('/api/jobs', json={'url': URL})
        client.post('/api/jobs', json={'url': URL})
        response = client.post('/api/jobs', json={'url': URL})
        assert response.status_code == 503
        assert response.headers['retry-after'].isdigit()

    def test_polling_is_never_throttled(self, client, limits):
        limits()
        job_id = client.post('/api/jobs', json={'url': URL}).json()['job_id']
        for _ in range(20):
            assert client.get(f'/api/jobs/{job_id}').status_code == 200


class TestThePage:
    def test_a_refused_url_says_so_in_the_hint(self, client, hx, limits):
        limits()
        for _ in range(3):
            submit(client, hx, url=URL)
        body = submit(client, hx, url=URL).text
        assert 'id="url-hint"' in body
        assert 'Too many submissions' in body
        assert '<article' not in body

    def test_a_refused_sample_is_a_failed_card(self, client, hx, limits):
        limits()
        for _ in range(3):
            submit(client, hx, sample='me')
        body = submit(client, hx, sample='me').text
        assert 'Too many submissions' in body
        assert 'class="chip failed"' in body
        assert 'Retry' not in body, 'there is no job behind it'

    def test_a_refused_retry_keeps_the_card(self, client, hx, limits, stub_task):
        limits()
        stub_task(support.REJECTS)
        body = submit(client, hx, url=URL).text
        job_id = body.split('id="job-')[1].split('"')[0]
        for _ in range(2):
            submit(client, hx, url=URL)
        body = client.post(f'/jobs/{job_id}/retry', headers=hx).text
        assert 'Too many submissions' in body
        assert f'id="job-{job_id}"' in body
