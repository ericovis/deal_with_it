"""The blob store. Every test gets its own directory, so this stays as
hermetic as the suite was when the pictures lived in fakeredis."""

import os
import subprocess
import sys
import time

import pytest

from src import blobs

JOB = '9f2c4a1b-0000-4000-8000-000000000001'
OTHER = '9f2c4a1b-0000-4000-8000-000000000002'


class TestReferences:
    """A reference is two halves, both of which arrive from outside."""

    def test_a_reference_is_the_job_and_the_name(self):
        assert blobs.ref(JOB, 'view.webp') == f'{JOB}/view.webp'

    def test_a_url_is_the_reference_under_the_served_prefix(self):
        assert blobs.url(f'{JOB}/view.webp') == f'/i/{JOB}/view.webp'

    @pytest.mark.parametrize('job_id', [
        '../../etc', '..', '.', '', 'a/b', 'has.a.dot', 'x' * 65, '-leading-dash',
    ])
    def test_an_unusable_job_id_is_refused(self, job_id):
        with pytest.raises(blobs.UnsafeReference):
            blobs.ref(job_id, 'view.webp')

    @pytest.mark.parametrize('name', [
        '../escape.webp', 'view', '.hidden.webp', 'View.WEBP', 'a/b.webp', '', 'x.toolong',
    ])
    def test_an_unusable_name_is_refused(self, name):
        with pytest.raises(blobs.UnsafeReference):
            blobs.ref(JOB, name)

    def test_a_traversal_never_becomes_a_path(self):
        """The halves are checked before they are joined, so there is no
        moment at which '..' is part of a real path."""
        for reference in ('../../etc/passwd', f'{JOB}/../../x.webp', 'a/b/c.webp'):
            with pytest.raises(blobs.UnsafeReference):
                blobs.path(reference)

    def test_a_rejected_card_id_is_not_a_job(self):
        """`rejected-00001234` cards never have blobs, but the id reaches
        the same helpers."""
        assert blobs.ref('rejected-00001234', 'view.webp')


class TestWriting:
    def test_bytes_come_back_out(self, blob_root):
        reference = blobs.put(JOB, 'source.jpg', b'not really a jpeg')
        assert blobs.path(reference).read_bytes() == b'not really a jpeg'
        assert blobs.exists(reference)

    def test_a_job_gets_its_own_directory(self, blob_root):
        blobs.put(JOB, 'source.jpg', b'x')
        assert (blob_root / JOB / 'source.jpg').is_file()

    def test_an_unfinished_write_never_appears_under_its_real_name(
            self, blob_root, monkeypatch):
        """A reader must see the whole file or nothing. The rename is what
        guarantees it, so this breaks the rename and checks the wreckage."""
        def refuse(*args, **kwargs):
            raise OSError('no rename for you')

        monkeypatch.setattr(os, 'replace', refuse)
        with pytest.raises(OSError):
            blobs.put(JOB, 'view.webp', b'half a picture')

        assert not blobs.exists(f'{JOB}/view.webp')
        assert list((blob_root / JOB).iterdir()) == [], 'and the temp file is cleaned up'

    def test_a_write_in_flight_is_not_servable(self, blob_root):
        """The temp name is dot-prefixed, which is what both servers refuse."""
        with blobs.open_for_write(JOB, 'view.webp') as temporary:
            temporary.write_bytes(b'still going')
            assert temporary.name.startswith('.')
            assert not blobs.exists(f'{JOB}/view.webp')
        assert blobs.exists(f'{JOB}/view.webp')

    def test_writing_again_replaces_what_was_there(self, blob_root):
        blobs.put(JOB, 'view.webp', b'first')
        blobs.put(JOB, 'view.webp', b'second')
        assert blobs.path(f'{JOB}/view.webp').read_bytes() == b'second'


class TestLinking:
    """A retry needs the first attempt's picture without borrowing its
    directory, which the sweep must be free to delete."""

    def test_a_link_shares_the_bytes(self, blob_root):
        original = blobs.put(JOB, 'source.jpg', b'the original upload')
        copy = blobs.link(original, OTHER, 'source.jpg')
        assert blobs.path(copy).read_bytes() == b'the original upload'
        assert blobs.path(original).stat().st_ino == blobs.path(copy).stat().st_ino

    def test_dropping_one_leaves_the_other_readable(self, blob_root):
        original = blobs.put(JOB, 'source.jpg', b'shared')
        copy = blobs.link(original, OTHER, 'source.jpg')
        blobs.forget(JOB)
        assert blobs.path(copy).read_bytes() == b'shared'


class TestDeleting:
    def test_delete_removes_one_blob(self, blob_root):
        reference = blobs.put(JOB, 'source.jpg', b'x')
        blobs.delete(reference)
        assert not blobs.exists(reference)

    def test_deleting_what_is_not_there_is_quiet(self, blob_root):
        blobs.delete(f'{JOB}/source.jpg')
        blobs.delete('nonsense')

    def test_forget_takes_the_whole_job(self, blob_root):
        blobs.put(JOB, 'source.jpg', b'x')
        blobs.put(JOB, 'view.webp', b'y')
        blobs.forget(JOB)
        assert not (blob_root / JOB).exists()


class TestSweeping:
    """Age first, then the ceiling, oldest first."""

    @staticmethod
    def _age(blob_root, job_id: str, seconds: float) -> None:
        when = time.time() - seconds
        os.utime(blob_root / job_id, (when, when))

    def test_nothing_to_do_is_not_an_error(self, blob_root):
        assert blobs.reap(ttl=60, max_bytes=10**9, min_age=0) == []

    def test_what_is_past_its_ttl_goes(self, blob_root):
        blobs.put(JOB, 'view.webp', b'old')
        blobs.put(OTHER, 'view.webp', b'new')
        self._age(blob_root, JOB, 7200)
        assert blobs.reap(ttl=3600, max_bytes=10**9, min_age=0) == [JOB]
        assert not (blob_root / JOB).exists()
        assert (blob_root / OTHER).exists()

    def test_the_ceiling_drops_the_oldest_first(self, blob_root):
        for index, job in enumerate((JOB, OTHER)):
            blobs.put(job, 'view.webp', b'x' * 1000)
            self._age(blob_root, job, 600 - index * 60)
        # Room for one of the two.
        assert blobs.reap(ttl=86400, max_bytes=1500, min_age=0) == [JOB]
        assert (blob_root / OTHER).exists()

    def test_the_ceiling_will_not_eat_a_job_that_may_still_be_running(
            self, blob_root, caplog):
        blobs.put(JOB, 'source.jpg', b'x' * 5000)
        assert blobs.reap(ttl=86400, max_bytes=10, min_age=240) == []
        assert (blob_root / JOB).exists()
        assert 'over its' in caplog.text, 'and it says so rather than going quiet'

    def test_a_directory_that_vanishes_mid_sweep_is_not_an_error(
            self, blob_root, monkeypatch):
        """Two workers may tick at once."""
        blobs.put(JOB, 'view.webp', b'x')
        self._age(blob_root, JOB, 7200)
        real = blobs.usage

        def racy():
            entries = real()
            blobs.forget(JOB)
            return entries

        monkeypatch.setattr(blobs, 'usage', racy)
        assert blobs.reap(ttl=60, max_bytes=10**9, min_age=0) == [JOB]

    def test_usage_reports_a_job_size(self, blob_root):
        blobs.put(JOB, 'view.webp', b'x' * 100)
        blobs.put(JOB, 'thumb.webp', b'y' * 50)
        entry = next(e for e in blobs.usage() if e.job_id == JOB)
        assert entry.size == 150


class TestCacheControl:
    def test_it_tracks_the_stores_own_ttl(self, blob_root, monkeypatch):
        """An immutable URL must never outlive the file behind it."""
        monkeypatch.setenv('DWI_BLOB_TTL', '1800')
        blobs.get_settings.cache_clear()
        assert blobs.cache_control() == 'private, max-age=1800, immutable'

    def test_a_picture_is_never_offered_to_a_shared_cache(self, blob_root):
        assert blobs.cache_control().startswith('private,')


def test_the_store_is_web_safe():
    """This module is imported by the web tier, which must never grow the
    ability to decode an attacker's image. A subprocess, because the test
    session has Pillow loaded for its own reasons."""
    code = ('import src.blobs, sys; '
            'print(sorted({"PIL", "cv2", "numpy", "dlib"} & set(sys.modules)))')
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == '[]', f'src.blobs dragged in {result.stdout.strip()}'
