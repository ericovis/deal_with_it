import base64
import socket

import numpy as np
import pytest
import requests
import responses
from PIL import Image

from src import images
from src.images import ImageSourceError
from tests.conftest import make_png


def data_uri(payload: bytes, mime: str = 'image/png') -> str:
    return f'data:{mime};base64,' + base64.b64encode(payload).decode()


class TestDecodeDataUri:
    def test_decodes_a_valid_data_uri(self, png_bytes):
        assert images.decode_data_uri(data_uri(png_bytes)) == png_bytes

    def test_tolerates_wrapped_base64(self, png_bytes):
        wrapped = data_uri(png_bytes).replace(';base64,', ';base64,\n', 1)
        assert images.decode_data_uri(wrapped) == png_bytes

    def test_rejects_a_missing_header(self, png_bytes):
        payload = base64.b64encode(png_bytes).decode()
        with pytest.raises(ImageSourceError, match='data URI header'):
            images.decode_data_uri(payload)

    def test_rejects_a_non_image_header(self, png_bytes):
        with pytest.raises(ImageSourceError, match='data URI header'):
            images.decode_data_uri(data_uri(png_bytes, mime='text/plain'))

    def test_rejects_undecodable_base64(self):
        with pytest.raises(ImageSourceError, match='could not be decoded'):
            images.decode_data_uri('data:image/png;base64,not!valid!base64!')


class TestToRgbArray:
    def test_decodes_to_an_rgb_uint8_array(self, png_bytes, settings):
        array = images.to_rgb_array(png_bytes, settings)
        assert array.shape == (32, 32, 3)
        assert array.dtype == np.uint8
        assert tuple(array[0][0]) == (255, 0, 0)

    def test_honours_exif_orientation(self, settings, tmp_path):
        """A 6-tagged image must come back rotated, the way image_to_numpy did."""
        source = Image.new('RGB', (40, 20), 'blue')
        # Paint a marker in the top-left so orientation is observable.
        source.putpixel((0, 0), (255, 255, 0))
        exif = source.getexif()
        exif[0x0112] = 6  # rotate 90 degrees clockwise
        path = tmp_path / 'rotated.jpg'
        source.save(path, exif=exif)

        array = images.to_rgb_array(path.read_bytes(), settings)
        assert array.shape[:2] == (40, 20), 'expected width and height to be swapped'

    def test_rejects_bytes_over_the_size_limit(self, settings):
        with pytest.raises(ImageSourceError, match='byte limit'):
            images.to_rgb_array(b'x' * (settings.max_image_bytes + 1), settings)

    def test_rejects_a_decompression_bomb_before_decoding(self, settings):
        # 1000x1000 is 1e6 pixels against a 1e5 limit, and is rejected from
        # the header alone -- the pixels are never allocated.
        with pytest.raises(ImageSourceError, match='pixel limit'):
            images.to_rgb_array(make_png(size=(1000, 1000)), settings)

    def test_rejects_garbage(self, settings):
        with pytest.raises(ImageSourceError, match='could not be decoded'):
            images.to_rgb_array(b'definitely not an image', settings)


class TestFetchUrl:
    @responses.activate
    def test_downloads_an_image(self, png_bytes, settings):
        responses.add(
            responses.GET, 'https://example.test/a.png',
            body=png_bytes, content_type='image/png',
        )
        assert images.fetch_url('https://example.test/a.png', settings) == png_bytes

    @responses.activate
    def test_rejects_a_non_image_content_type(self, settings):
        responses.add(
            responses.GET, 'https://example.test/page',
            body='<html/>', content_type='text/html',
        )
        with pytest.raises(ImageSourceError, match='not an image'):
            images.fetch_url('https://example.test/page', settings)

    @responses.activate
    def test_rejects_a_non_200(self, settings):
        responses.add(responses.GET, 'https://example.test/gone', status=404)
        with pytest.raises(ImageSourceError, match='HTTP status: 404'):
            images.fetch_url('https://example.test/gone', settings)

    @responses.activate
    def test_rejects_a_declared_size_over_the_limit(self, settings):
        responses.add(
            responses.GET, 'https://example.test/big.png',
            body=b'x' * 10, content_type='image/png',
            headers={'content-length': str(settings.max_image_bytes + 1)},
        )
        with pytest.raises(ImageSourceError, match='byte limit'):
            images.fetch_url('https://example.test/big.png', settings)

    @responses.activate
    def test_caps_an_undeclared_body_while_streaming(self, settings):
        """Content-Length is only a hint, so the read itself must stop short."""
        responses.add(
            responses.GET, 'https://example.test/big.png',
            body=b'x' * (settings.max_image_bytes + 1024), content_type='image/png',
        )
        with pytest.raises(ImageSourceError, match='byte limit'):
            images.fetch_url('https://example.test/big.png', settings)

    @responses.activate
    def test_follows_a_redirect_and_revalidates_it(self, png_bytes, settings):
        responses.add(
            responses.GET, 'https://example.test/go',
            status=302, headers={'location': 'https://elsewhere.test/a.png'},
        )
        responses.add(
            responses.GET, 'https://elsewhere.test/a.png',
            body=png_bytes, content_type='image/png',
        )
        assert images.fetch_url('https://example.test/go', settings) == png_bytes

    @responses.activate
    def test_refuses_a_redirect_into_a_private_address(self, settings):
        """The classic SSRF bypass: a public URL that bounces to metadata."""
        responses.add(
            responses.GET, 'https://example.test/go',
            status=302, headers={'location': 'http://metadata.test/latest/meta-data/'},
        )
        with pytest.raises(ImageSourceError, match='non-public address'):
            images.fetch_url('https://example.test/go', settings)

    @responses.activate
    def test_refuses_an_endless_redirect_chain(self, settings):
        responses.add(
            responses.GET, 'https://example.test/loop',
            status=302, headers={'location': 'https://example.test/loop'},
        )
        with pytest.raises(ImageSourceError, match='redirected too many times'):
            images.fetch_url('https://example.test/loop', settings)

    @responses.activate
    def test_reports_a_connection_failure_without_leaking_internals(self, settings):
        responses.add(
            responses.GET, 'https://example.test/a.png',
            body=requests.exceptions.ConnectionError('kaboom'),
        )
        with pytest.raises(ImageSourceError, match='could not be fetched'):
            images.fetch_url('https://example.test/a.png', settings)


class TestSsrfGuard:
    @pytest.mark.parametrize('url', [
        'http://localhost:6379/',
        'http://metadata.test/latest/meta-data/',
        'https://internal.test/secret.png',
    ])
    def test_refuses_non_public_hosts(self, url, settings):
        with pytest.raises(ImageSourceError, match='non-public address'):
            images.fetch_url(url, settings)

    @pytest.mark.parametrize('url', [
        'file:///etc/passwd',
        'gopher://example.test/',
        'redis://localhost:6379/',
    ])
    def test_refuses_non_http_schemes(self, url, settings):
        with pytest.raises(ImageSourceError, match='Only http and https'):
            images.fetch_url(url, settings)

    def test_reports_an_unresolvable_host(self, settings):
        with pytest.raises(ImageSourceError, match='could not be resolved'):
            images.fetch_url('https://unresolvable.test/a.png', settings)

    @responses.activate
    def test_private_addresses_are_reachable_when_explicitly_allowed(self, png_bytes, settings):
        """Opt-in escape hatch, for pointing the app at a local fixture server."""
        permissive = settings.model_copy(update={'allow_private_addresses': True})
        responses.add(
            responses.GET, 'http://localhost/a.png',
            body=png_bytes, content_type='image/png',
        )
        assert images.fetch_url('http://localhost/a.png', permissive) == png_bytes


class TestLoad:
    def test_loads_from_base64(self, png_bytes, settings):
        array = images.load(base64_data=data_uri(png_bytes), settings=settings)
        assert array.shape == (32, 32, 3)

    @responses.activate
    def test_loads_from_url(self, png_bytes, settings):
        responses.add(
            responses.GET, 'https://example.test/a.png',
            body=png_bytes, content_type='image/png',
        )
        array = images.load(url='https://example.test/a.png', settings=settings)
        assert array.shape == (32, 32, 3)

    def test_requires_a_source(self, settings):
        with pytest.raises(ImageSourceError, match='must be passed'):
            images.load(settings=settings)


class TestShapeError:
    """The shape-only check behind the live hint under the URL field."""

    @pytest.mark.parametrize('url,expected', [
        ('https://example.test/a.png', None),
        ('http://example.test/a.png', None),
        ('https://example.test:8443/a.png', None),
        ('ftp://example.test/a.png', images.SCHEME_ERROR),
        ('not a url', images.SCHEME_ERROR),
        ('', images.SCHEME_ERROR),
        ('http:///a.png', images.NO_HOST_ERROR),
        ('http://[oops/a.png', images.NO_HOST_ERROR),
    ])
    def test_judges_what_it_can_see(self, url, expected):
        assert images.shape_error(url) == expected

    @pytest.mark.parametrize('host', [
        'localhost', '0.0.0.0', '127.0.0.1', '10.1.2.3', '192.168.0.1',
        '169.254.169.254', '172.16.0.1', '172.31.255.255', 'printer.local',
    ])
    def test_turns_away_the_obviously_private(self, host):
        assert images.shape_error(f'http://{host}/a.png') == images.non_public_error(host)

    @pytest.mark.parametrize('host', ['172.15.0.1', '172.32.0.1', '11.0.0.1', '10.example.test'])
    def test_does_not_over_reach(self, host):
        """These only look private. Judging them here would reject real hosts."""
        assert images.shape_error(f'http://{host}/a.png') is None

    def test_it_never_resolves_anything(self, monkeypatch):
        """The point of the exercise: a web handler must not wait on DNS."""
        monkeypatch.setattr(socket, 'getaddrinfo', _refuse_to_resolve)
        assert images.shape_error('https://example.test/a.png') is None

    def test_it_is_not_the_real_guard(self, settings, monkeypatch):
        """A name that resolves privately passes the shape check and is still
        refused by the worker, which is the one that asks."""
        assert images.shape_error('http://internal.test/a.png') is None
        with pytest.raises(ImageSourceError, match='non-public'):
            images.fetch_url('http://internal.test/a.png', settings)


def _refuse_to_resolve(*args, **kwargs):
    raise AssertionError('shape_error must not resolve a hostname')
