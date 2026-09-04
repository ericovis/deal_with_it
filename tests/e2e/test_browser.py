"""The interface, driven by a browser, against the whole real stack."""

import re

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


#: Drops a <time data-expires> on the page with a chosen number of seconds
#: left, so the units can be checked without waiting an hour for a real one.
SPAWN_EXPIRY = """(seconds) => {
    const t = document.createElement('time');
    t.id = 'probe';
    t.setAttribute('data-expires', '');
    t.setAttribute('datetime', new Date(Date.now() + seconds * 1000).toISOString());
    document.body.appendChild(t);
}"""


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
        names = page.locator('#queue .card-id b').all_text_contents()
        # Newest on top, the same rule every other submission follows.
        assert names == ['three.png', 'two.png', 'one.png']

    def test_each_file_is_posted_on_its_own(self, page: Page, tmp_path):
        """One request per picture, so a card lands as each upload finishes
        rather than all of them after the last byte of the batch."""
        posted = []
        page.on('request', lambda request: posted.append(request.url)
                if request.method == 'POST' and request.url.endswith('/submit') else None)
        paths = []
        for name in ('one.png', 'two.png', 'three.png'):
            path = tmp_path / name
            path.write_bytes(make_png())
            paths.append(path)
        page.goto('/')
        page.locator('#files').set_input_files(paths)
        expect(page.locator('#queue article')).to_have_count(3, timeout=TIMEOUT)
        assert len(posted) == 3

    def test_the_dropzone_is_emptied_so_the_same_file_can_be_sent_twice(
            self, page: Page, tmp_path):
        picture = tmp_path / 'again.png'
        picture.write_bytes(make_png())
        page.goto('/')
        page.locator('#files').set_input_files(picture)
        expect(page.locator('#queue article')).to_have_count(1, timeout=TIMEOUT)
        expect(page.locator('#files')).to_have_js_property('value', '')
        page.locator('#files').set_input_files(picture)
        expect(page.locator('#queue article')).to_have_count(2, timeout=TIMEOUT)

    def test_clicking_anywhere_on_the_dropzone_opens_the_picker(self, page: Page):
        """The card is no longer the label itself -- it holds the camera
        button too -- so the label's hit area is stretched back over it. A
        click on the padding has to reach the file input all the same."""
        page.goto('/')
        with page.expect_file_chooser() as chooser:
            page.locator('.dropzone').click(position={'x': 6, 'y': 6})
        assert chooser.value.is_multiple(), 'the library input, not the camera'

    def test_a_typed_url_survives_an_upload(self, page: Page, tmp_path):
        """The form is no longer swapped out from under a file post, so
        whatever is in the URL field stays there."""
        picture = tmp_path / 'holiday.png'
        picture.write_bytes(make_png())
        page.goto('/')
        page.locator('#url').fill('https://example.test/later.png')
        page.locator('#files').set_input_files(picture)
        expect(page.locator('#queue article')).to_have_count(1, timeout=TIMEOUT)
        expect(page.locator('#url')).to_have_value('https://example.test/later.png')


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


class TestOnAPhone:
    """The whole phone layout, at 390x844.

    None of it has a route or a line of script: which state the page is in is
    `:has(#queue article)`, and the two panels over the bar are radios.
    """

    @pytest.fixture(autouse=True)
    def phone(self, page: Page):
        page.set_viewport_size({'width': 390, 'height': 844})

    def test_the_input_is_the_whole_page_until_there_is_a_result(self, page: Page):
        page.goto('/')
        expect(page.locator('.lede')).to_be_visible()
        expect(page.locator('.dropzone')).to_be_visible()
        expect(page.locator('.addbar')).to_be_hidden()

    def test_a_result_takes_the_screen_and_the_input_moves_to_the_bar(self, page: Page):
        run(page, 'me')
        expect(page.locator('.lede')).to_be_hidden()
        expect(page.locator('.dropzone')).to_be_hidden()
        expect(page.locator('.addbar')).to_be_visible()

    def test_clearing_the_list_gives_the_input_back(self, page: Page):
        run(page, 'me')
        page.locator('.clear').click()
        expect(page.locator('.addbar')).to_be_hidden()
        expect(page.locator('.lede')).to_be_visible()

    def test_the_url_tab_brings_the_field_back_and_a_second_tap_closes_it(self, page: Page):
        run(page, 'me')
        expect(page.locator('#url')).to_be_hidden()
        page.locator('.addbar-item.show-url .open').click()
        expect(page.locator('#url')).to_be_visible()
        page.locator('.addbar-item.show-url .close').click()
        expect(page.locator('#url')).to_be_hidden()

    def test_the_samples_tab_brings_the_samples_back(self, page: Page):
        run(page, 'me')
        expect(page.locator('.sample-strip')).to_be_hidden()
        page.locator('.addbar-item.show-samples .open').click()
        expect(page.locator('.sample-strip')).to_be_visible()
        expect(page.locator('#url')).to_be_hidden(), 'one panel at a time'

    def test_the_card_opens_the_one_picker(self, page: Page):
        """One input for both devices: a phone's picker already offers the
        camera beside the library."""
        page.goto('/')
        with page.expect_file_chooser() as chooser:
            page.locator('.dropzone-face').click()
        assert chooser.value.is_multiple(), 'several at once is fine'

    def test_the_bar_opens_the_same_picker(self, page: Page):
        run(page, 'me')
        with page.expect_file_chooser() as chooser:
            page.locator('.addbar-item.add').click()
        assert chooser.value.is_multiple()

    def test_a_picture_becomes_a_card_without_disturbing_a_typed_url(
            self, page: Page, tmp_path):
        picture = tmp_path / 'snap.png'
        picture.write_bytes(make_png())
        page.goto('/')
        page.locator('#url').fill('https://example.test/later.png')
        page.locator('#files').set_input_files(picture)
        expect(page.locator('#queue article')).to_have_count(1, timeout=TIMEOUT)
        expect(first_card(page).locator('.card-id b')).to_have_text('snap.png')
        expect(page.locator('#url')).to_have_value('https://example.test/later.png')

    def test_a_card_swipes_left_onto_a_remove_panel(self, page: Page):
        card = run(page, 'me')
        remove = card.locator('.card-remove')
        expect(remove).not_to_be_in_viewport()
        # The card is the scroll container, so it must have stopped growing
        # before it is scrolled: a re-layout puts a snap container back on
        # the panel it was showing, which is the card body.
        card.locator('.frame img.after').evaluate('img => img.decode()')
        card.evaluate('el => el.scrollTo({left: el.scrollWidth, behavior: "instant"})')
        expect(remove).to_be_in_viewport()
        remove.locator('button').click()
        expect(page.locator('#queue article')).to_have_count(0)

    def test_tapping_the_picture_opens_it_full_size(self, page: Page):
        card = run(page, 'me')
        card.locator('.frame-tap').click()
        expect(card.locator('.lightbox-close')).to_be_visible()
        expect(card.locator('.zoom')).to_be_visible()

    def test_the_full_size_view_zooms_and_fits_again(self, page: Page):
        card = run(page, 'me')
        card.locator('.frame-tap').click()
        picture = card.locator('.frame img.after')
        fitted = picture.bounding_box()['width']
        card.locator('.zoom').click()
        expect(card.locator('.zoom .out')).to_have_text('Fit')
        assert picture.bounding_box()['width'] > fitted
        card.locator('.zoom').click()
        assert picture.bounding_box()['width'] == fitted

    def test_the_contents_list_on_the_docs_page_starts_closed(self, page: Page):
        page.goto('/docs')
        expect(page.locator('.toc a').first).to_be_hidden()
        page.locator('.toc summary').click()
        expect(page.locator('.toc a').first).to_be_visible()


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


class TestTheSampleGrid:
    def test_the_tiles_are_square(self, page: Page):
        """They are square files and the CSS crops square; a width/height
        attribute pair once quietly overrode the aspect-ratio and made them
        tall, which no unit test can see."""
        page.goto('/')
        for tile in page.locator('.sample img').all()[:4]:
            box = tile.bounding_box()
            assert abs(box['width'] - box['height']) <= 1, (
                f'{box["width"]}x{box["height"]} is not square'
            )

    def test_a_tile_is_small(self, page: Page):
        """Sixteen full-size JPEGs was 4 MB of landing page."""
        page.goto('/')
        page.wait_for_load_state('networkidle')
        sizes = page.evaluate("""() => performance.getEntriesByType('resource')
            .filter(r => r.name.includes('/static/img/tiles/'))
            .map(r => r.encodedBodySize)""")
        assert sizes, 'no tiles were fetched'
        assert max(sizes) < 40_000, f'largest tile is {max(sizes)} bytes'


class TestTheCountdown:
    """The one script in the interface that is not htmx wiring."""

    def test_it_rewrites_the_server_sentence(self, page: Page):
        """The server renders an absolute time; the script makes it relative."""
        card = run(page, 'me')
        expiry = card.locator('time[data-expires]')
        expect(expiry).to_be_visible()
        expect(expiry).to_contain_text('This image will be deleted in', timeout=5000)

    def test_the_clock_actually_moves(self, page: Page):
        """Injected with seconds left, because at minute granularity a real
        card would take a minute to visibly change."""
        page.goto('/')
        page.evaluate(SPAWN_EXPIRY, 40)
        page.wait_for_timeout(1200)
        first = page.locator('#probe').inner_text()
        page.wait_for_timeout(2500)
        assert page.locator('#probe').inner_text() != first, f'stuck on {first!r}'

    @pytest.mark.parametrize('seconds, pattern', [
        (40, r'deleted in \d+ s$'),
        (150, r'deleted in 2 min$'),
        (7300, r'deleted in \d+ h \d+ min$'),
        (-5, r'has been deleted$'),
    ])
    def test_it_picks_a_unit_worth_reading(self, page: Page, seconds, pattern):
        page.goto('/')
        page.evaluate(SPAWN_EXPIRY, seconds)
        page.wait_for_timeout(1200)
        assert re.search(pattern, page.locator('#probe').inner_text())

    def test_the_page_still_reads_without_it(self, page: Page):
        """The absolute time is the truth; the ticker is decoration. Blocking
        the file with an empty one, not setInterval -- the script ticks once
        directly on load, so stubbing the timer would still let it rewrite the
        sentence. Empty rather than aborted, so the console stays clean."""
        page.route('**/countdown.js*',
                   lambda route: route.fulfill(status=200, content_type='text/javascript',
                                               body=''))
        card = run(page, 'me')
        expect(card.locator('time[data-expires]')).to_contain_text('will be deleted at')


class TestSharing:
    def test_a_finished_card_links_to_a_page_worth_passing_on(self, page: Page):
        card = run(page, 'me')
        card.locator('a.share').click()
        expect(page.locator('h1')).to_contain_text('Someone dealt with it')
        expect(page.locator('.frame img.after')).to_be_visible()
        page.locator('.frame img.after').evaluate('img => img.decode()')


class TestNotFound:
    @pytest.mark.expects_404
    def test_an_unknown_url_gets_the_page(self, page: Page):
        page.goto('/no-such-thing')
        expect(page.locator('h1')).to_contain_text('Nothing here')
        expect(page.locator('.site-footer')).to_be_visible()
