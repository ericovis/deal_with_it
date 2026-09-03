"""The interface, driven by a browser, against the whole real stack."""

import pytest
from playwright.sync_api import Page, expect

from src.ui import EXAMPLE_SOURCE, SAMPLES
from tests.conftest import make_png
from tests.e2e.conftest import TIMEOUT

pytestmark = [pytest.mark.e2e, pytest.mark.faces]

def try_sample(page: Page, name: str) -> None:
    """Click a tile, found by the file name printed on it.

    Not by position: with seventeen of them an index is unreadable, and
    adding a sample used to silently repoint these at the wrong picture.
    """
    page.locator('.sample', has_text=SAMPLES[name].filename).click()


def first_card(page: Page):
    return page.locator('#queue article').first


def run(page: Page, name: str, outcome: str = 'done'):
    """Submit a sample and wait for the worker to be finished with it."""
    page.goto('/')
    try_sample(page, name)
    card = first_card(page)
    expect(card.locator(f'.chip.{outcome}')).to_be_visible(timeout=TIMEOUT)
    return card


class TestSubmitting:
    def test_the_list_starts_empty(self, page: Page):
        page.goto('/')
        expect(page.locator('.empty')).to_be_visible()
        expect(page.locator('#queue article')).to_have_count(0)

    def test_a_picture_goes_all_the_way_to_a_result(self, page: Page):
        """Browser to htmx to FastHTML to RQ to the detector to Pillow and back."""
        card = run(page, 'me')
        expect(card.locator('.frame img.after')).to_be_visible()
        expect(card.locator('.card-id b')).to_have_text('me.jpg')
        expect(card.locator('.card-id span')).to_have_text('Sample picture · 1 face')
        expect(page.locator('.empty')).to_be_hidden()

    def test_a_card_stops_polling_once_it_is_done(self, page: Page):
        card = run(page, 'me')
        expect(card).not_to_have_attribute('hx-get', '.', timeout=1000)

    def test_a_picture_with_no_faces_fails_with_a_reason(self, page: Page):
        card = run(page, 'socks', outcome='failed')
        expect(card.locator('.card-error p')).to_have_text(
            'No faces were found in this image.'
        )

    def test_retry_queues_the_picture_again(self, page: Page):
        card = run(page, 'socks', outcome='failed')
        was = card.get_attribute('id')
        card.locator('.retry').click()
        expect(first_card(page)).not_to_have_attribute('id', was, timeout=TIMEOUT)
        expect(first_card(page).locator('.chip.failed')).to_be_visible(timeout=TIMEOUT)

    def test_an_upload_becomes_a_card(self, page: Page, tmp_path):
        picture = tmp_path / 'holiday.png'
        picture.write_bytes(make_png())
        page.goto('/')
        page.locator('#files').set_input_files(picture)
        expect(first_card(page).locator('.card-id b')).to_have_text(
            'holiday.png', timeout=TIMEOUT
        )

    def test_several_files_become_several_cards(self, page: Page, tmp_path):
        paths = []
        for name in ('one.png', 'two.png', 'three.png'):
            path = tmp_path / name
            path.write_bytes(make_png())
            paths.append(path)
        page.goto('/')
        page.locator('#files').set_input_files(paths)
        expect(page.locator('#queue article')).to_have_count(3, timeout=TIMEOUT)


class TestTheUrlField:
    def test_it_says_what_is_wrong_while_you_type(self, page: Page):
        page.goto('/')
        page.locator('#url').fill('http://localhost/photo.jpg')
        expect(page.locator('#url-hint')).to_have_text(
            "The host 'localhost' resolves to a non-public address, "
            'which is not allowed.'
        )
        expect(page.locator('#url-hint')).to_have_class('error')

    def test_it_says_when_nothing_is_wrong(self, page: Page):
        page.goto('/')
        page.locator('#url').fill('https://example.com/photo.jpg')
        expect(page.locator('#url-hint')).to_have_class('ok')

    def test_submitting_empties_the_field(self, page: Page):
        """The blank form comes back as an out-of-band swap."""
        page.goto('/')
        page.locator('#url').fill('https://example.test/photo.jpg')
        page.locator('#form button[type="submit"]').click()
        expect(page.locator('#queue article')).to_have_count(1, timeout=TIMEOUT)
        expect(page.locator('#url')).to_have_value('')

    def test_submitting_nothing_asks_for_something(self, page: Page):
        page.goto('/')
        page.locator('#form button[type="submit"]').click()
        expect(page.locator('#url-hint')).to_have_text('No image or URL was provided!')
        expect(page.locator('#queue article')).to_have_count(0)


class TestComparingBeforeAndAfter:
    def test_the_toggle_swaps_which_image_is_shown(self, page: Page):
        card = run(page, 'me')
        expect(card.locator('.frame img.after')).to_be_visible()
        expect(card.locator('.frame img.before')).to_be_hidden()

        card.locator('label[data-view="before"]').click()
        expect(card.locator('.frame img.before')).to_be_visible()
        expect(card.locator('.frame img.after')).to_be_hidden()

        card.locator('label[data-view="after"]').click()
        expect(card.locator('.frame img.after')).to_be_visible()


class TestTheFullSizeView:
    def test_it_opens_over_the_page_and_closes_again(self, page: Page):
        card = run(page, 'me')
        expect(card.locator('.lightbox-close')).to_be_hidden()

        card.locator('.open-full').click()
        expect(card.locator('.lightbox-close')).to_be_visible()
        expect(card.locator('.frame img.after')).to_be_visible()

        card.locator('.lightbox-close').click()
        expect(card.locator('.lightbox-close')).to_be_hidden()

    def test_it_fills_the_window(self, page: Page):
        card = run(page, 'me')
        card.locator('.open-full').click()
        frame = card.locator('.frame').bounding_box()
        viewport = page.viewport_size
        assert frame['width'] == viewport['width']
        assert frame['height'] == viewport['height']

    def test_clicking_beside_the_image_closes_it(self, page: Page):
        card = run(page, 'me')
        card.locator('.open-full').click()
        page.mouse.click(30, 450)
        expect(card.locator('.lightbox-close')).to_be_hidden()

    def test_before_and_after_work_inside_it_too(self, page: Page):
        """The overlay reuses the card's own radios rather than a second set."""
        card = run(page, 'me')
        card.locator('.open-full').click()
        card.locator('label[data-view="before"]').click()
        expect(card.locator('.frame img.before')).to_be_visible()
        expect(card.locator('.frame img.after')).to_be_hidden()

        card.locator('.lightbox-close').click()
        expect(card.locator('.frame img.before')).to_be_visible(), 'and the card agrees'


class TestClearingUp:
    def test_a_card_can_be_removed(self, page: Page):
        card = run(page, 'me')
        card.locator('.remove').click()
        expect(page.locator('#queue article')).to_have_count(0)
        expect(page.locator('.empty')).to_be_visible()

    def test_clear_empties_the_whole_list(self, page: Page):
        page.goto('/')
        try_sample(page, 'me')
        try_sample(page, 'socks')
        expect(page.locator('#queue article')).to_have_count(2)

        page.locator('.clear').click()
        expect(page.locator('#queue article')).to_have_count(0)
        expect(page.locator('.empty')).to_be_visible()

    def test_clear_is_hidden_until_there_is_something_to_clear(self, page: Page):
        page.goto('/')
        expect(page.locator('.clear')).to_be_hidden()
        try_sample(page, 'me')
        expect(page.locator('.clear')).to_be_visible()


class TestTheTheme:
    #: --bg in each palette, as a browser reports it.
    LIGHT = 'rgb(249, 248, 246)'
    DARK = 'rgb(23, 24, 20)'

    def background(self, page: Page) -> str:
        return page.evaluate('getComputedStyle(document.body).backgroundColor')

    def test_the_switch_repaints_the_page_and_is_remembered(self, page: Page):
        page.goto('/')
        # Start from a known place rather than whatever the OS prefers.
        page.context.add_cookies([{'name': 'dwi_theme', 'value': 'light',
                                   'url': page.url}])
        page.reload()
        assert self.background(page) == self.LIGHT

        page.locator('label.switch').click()
        expect(page.locator('#theme-state')).to_have_attribute('data-theme', 'dark')
        assert self.background(page) == self.DARK

        page.reload()
        assert self.background(page) == self.DARK, 'the cookie outlives the page'

        page.locator('label.switch').click()
        expect(page.locator('#theme-state')).to_have_attribute('data-theme', 'light')
        assert self.background(page) == self.LIGHT, 'and it goes back'

    def test_the_docs_page_follows_the_same_choice(self, page: Page):
        page.goto('/')
        page.context.add_cookies([{'name': 'dwi_theme', 'value': 'dark', 'url': page.url}])
        page.goto('/docs')
        assert self.background(page) == self.DARK


class TestTheRestOfTheFurniture:
    def test_the_status_pill_reports_a_reachable_broker(self, page: Page):
        page.goto('/')
        expect(page.locator('#health')).to_have_text('workers ready')

    def test_the_docs_page_is_reachable_from_the_app(self, page: Page):
        page.goto('/')
        page.locator('.nav a', has_text='Docs').click()
        expect(page.locator('.doc h1')).to_have_text('How it works')

    def test_every_snippet_can_be_copied(self, page: Page):
        page.context.grant_permissions(['clipboard-read', 'clipboard-write'])
        page.goto('/docs')
        page.locator('.snippet .copy').first.click()
        copied = page.evaluate('navigator.clipboard.readText()')
        assert f'"url": "{EXAMPLE_SOURCE}"' in copied
