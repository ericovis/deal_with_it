"""Content-hashed static URLs, and what they are worth to a cache."""

import os

from src import assets
from src.assets import IMMUTABLE, REVALIDATE, asset, version


class TestVersioning:
    def test_a_static_url_carries_the_content_hash_of_its_file(self):
        stamped = asset('/static/css/style.css')
        assert stamped.startswith('/static/css/style.css?v=')
        assert stamped == asset('/static/css/style.css'), 'the same bytes, the same URL'

    def test_two_files_do_not_share_a_version(self):
        assert version('/static/css/style.css') != version('/static/img/glasses.svg')

    def test_editing_a_file_moves_its_version(self, tmp_path, monkeypatch):
        """The point of the whole exercise: a deploy invalidates what it
        changed. The stat is what notices, so a reloading dev server does
        not have to restart to serve a new stylesheet."""
        monkeypatch.setattr(assets, 'STATIC_DIR', tmp_path)
        edited = tmp_path / 'x.css'
        edited.write_text('a {}')
        before = version('/static/x.css')
        edited.write_text('b {}')
        # The kernel stamps inodes from a coarse clock, so two writes this
        # close together can share an mtime. A real edit is seconds apart;
        # setting one keeps the test about content rather than about how
        # fast pytest runs.
        os.utime(edited, ns=(0, edited.stat().st_mtime_ns + 10_000_000))
        assert version('/static/x.css') != before

    def test_a_path_that_names_no_file_is_left_alone(self):
        """A missing icon should 404 on its own, not take the page down."""
        assert asset('/static/img/nope.png') == '/static/img/nope.png'
        assert version('/static/img/nope.png') is None

    def test_a_url_that_is_not_ours_is_left_alone(self):
        url = 'https://fonts.googleapis.com/css2?family=Manrope'
        assert asset(url) == url

    def test_a_directory_is_not_a_file(self):
        assert version('/static/img') is None


class TestCacheHeaders:
    def test_a_versioned_url_may_be_kept_forever(self, client):
        """Any ``v`` counts: the URL is the promise, and the only URLs we
        hand out are the ones ``asset()`` built."""
        response = client.get('/static/css/style.css?v=0123456789')
        assert response.status_code == 200
        assert response.headers['cache-control'] == IMMUTABLE

    def test_a_bare_url_is_revalidated_instead(self, client):
        response = client.get('/static/css/style.css')
        assert response.headers['cache-control'] == REVALIDATE

    def test_a_miss_is_not_cached_as_one(self, client):
        assert 'cache-control' not in client.get('/static/img/nope.png?v=1').headers

    def test_the_page_asks_for_the_versioned_stylesheet(self, client):
        assert 'href="/static/css/style.css?v=' in client.get('/').text
