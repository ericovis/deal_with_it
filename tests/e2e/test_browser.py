"""The interface, driven by a browser, against the whole real stack."""

import re

import pytest
from playwright.sync_api import Page, expect

from src.config import get_settings
from src.ui import EXAMPLE_SOURCE, SAMPLES
from tests.conftest import STATIC_IMG, make_png
from tests.e2e.conftest import TIMEOUT

pytestmark = [pytest.mark.e2e, pytest.mark.faces]

def try_sample(page: Page, name: str) -> None:
    """Click a tile, found by the file name printed on it.

    Not by position: with a dozen of them an index is unreadable, and
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


#: Tells share.js whether this browser can hand a *file* to a share sheet.
#: Headless Chromium on Linux cannot, so the button that the phone footer is
#: sized around has to be asked for; forcing it off is how the row without it
#: gets tested on the same browser.
def pretend_sharing(page: Page, works: bool) -> None:
    value = '() => true' if works else 'undefined'
    page.add_init_script(f"""
        Object.defineProperty(navigator, 'canShare',
            {{value: {value}, configurable: true}});
        Object.defineProperty(navigator, 'share',
            {{value: {value}, configurable: true}});
    """)


def boxes(page: Page, selector: str) -> list[dict]:
    """The laid-out rectangle of everything matching, in DOM order."""
    return page.evaluate(
        """(selector) => [...document.querySelectorAll(selector)]
               .map(el => {
                   const r = el.getBoundingClientRect();
                   return {top: r.top, width: r.width, height: r.height};
               })""",
        selector,
    )


def first_card(page: Page):
    return page.locator('#queue article').first


def run(page: Page, name: str, outcome: str = 'done'):
    """Submit a sample and wait for the worker to be finished with it."""
    page.goto('/')
    try_sample(page, name)
    card = first_card(page)
    expect(card.locator(f'.chip.{outcome}')).to_be_visible(timeout=TIMEOUT)
    return card


#: A committed picture with no face in it. It was a sample once; the samples
#: are all faces now, so the failure path is reached by uploading it.
FACELESS = STATIC_IMG / 'socks_the_cat.jpg'


def run_upload(page: Page, path, outcome: str = 'done'):
    """The same, for a picture the page does not offer."""
    page.goto('/')
    page.locator('#files').set_input_files(path)
    card = first_card(page)
    expect(card.locator(f'.chip.{outcome}')).to_be_visible(timeout=TIMEOUT)
    return card


def test_the_pins_took(live_server):
    """The settings this suite depends on are actually in force.

    Not paranoia: the rate limit silently reverted to thirty a minute for a
    long time, and because this suite submits far more than that from one
    address, the symptom was a card coming back Failed in whichever test
    happened to cross the line -- a different one each run, and only sometimes.
    Cheaper to assert the cause than to keep diagnosing the symptom.
    """
    settings = get_settings()
    assert settings.rate_limit == 0, 'the suite would throttle itself'
    assert settings.http_timeout == 1, 'one hung fetch would block the worker'
    assert settings.blob_dir, 'the store must not land next to the checkout'


class TestSubmitting:
    def test_the_list_starts_empty(self, page: Page):
        page.goto('/')
        expect(page.locator('.empty')).to_be_visible()
        expect(page.locator('#queue article')).to_have_count(0)

    def test_a_picture_goes_all_the_way_to_a_result(self, page: Page):
        """Browser to htmx to FastHTML to RQ to the detector to Pillow and back."""
        card = run(page, 'me')
        expect(card.locator('.frame img.animated')).to_be_visible()
        expect(card.locator('.card-id b')).to_have_text('me.jpg')
        expect(card.locator('.card-id span')).to_have_text('Sample picture · 1 face')
        expect(page.locator('.empty')).to_be_hidden()

    def test_a_card_stops_polling_once_it_is_done(self, page: Page):
        card = run(page, 'me')
        expect(card).not_to_have_attribute('hx-get', '.', timeout=1000)

    def test_a_picture_with_no_faces_fails_with_a_reason(self, page: Page):
        card = run_upload(page, FACELESS, outcome='failed')
        expect(card.locator('.card-error p')).to_have_text(
            'No faces were found in this image.'
        )

    def test_retry_queues_the_picture_again(self, page: Page):
        card = run_upload(page, FACELESS, outcome='failed')
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
        expect(card.locator('.frame img.animated')).to_be_visible()
        expect(card.locator('.frame img.before')).to_be_hidden()

        card.locator('.card-foot label[data-view="before"]').click()
        expect(card.locator('.frame img.before')).to_be_visible()
        expect(card.locator('.frame img.animated')).to_be_hidden()

        card.locator('.card-foot label[data-view="after"]').click()
        expect(card.locator('.frame img.after')).to_be_visible()

    def test_the_card_opens_on_the_gif(self, page: Page):
        """It is the tab a card opens on: the picture has to be decoded
        without anyone clicking anything."""
        card = run(page, 'me')
        gif = card.locator('.frame img.animated')
        expect(gif).to_be_visible()
        expect(card.locator('.frame img.after')).to_be_hidden()
        assert gif.get_attribute('src').endswith('/animation.gif')
        assert gif.get_attribute('loading') != 'lazy', 'first in the queue, never deferred'
        page.wait_for_function('img => img.naturalWidth > 0', arg=gif.element_handle())

    def test_the_still_is_fetched_before_its_tab_is_opened(self, page: Page):
        """It used to wait for the click that needed it. `views.js` promotes
        it behind the GIF instead, so the tab is already decoded when it is
        chosen -- and the hint must not strand it either way."""
        card = run(page, 'me')
        still = card.locator('.frame img.after')
        page.wait_for_function('img => img.naturalWidth > 0', timeout=20000,
                               arg=still.element_handle())

        card.locator('.card-foot label[data-view="after"]').click()
        expect(still).to_be_visible()


class TestTheFullSizeView:
    def test_it_opens_over_the_page_and_closes_again(self, page: Page):
        card = run(page, 'me')
        expect(card.locator('.lightbox-close')).to_be_hidden()

        card.locator('.open-full').click()
        expect(card.locator('.lightbox-close')).to_be_visible()
        expect(card.locator('.frame img.animated')).to_be_visible()

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
        card.locator('.card-foot label[data-view="before"]').click()
        expect(card.locator('.frame img.before')).to_be_visible()
        expect(card.locator('.frame img.after')).to_be_hidden()

        card.locator('.lightbox-close').click()
        expect(card.locator('.frame img.before')).to_be_visible(), 'and the card agrees'


class TestTheDesktopCardIsUnchanged:
    """The phone got a new footer; the desktop keeps the one it had.

    Written as the shape of the row rather than a screenshot: one line, the
    segmented control first and Download last, and neither of the phone's
    pills anywhere on the picture.
    """

    def test_the_footer_is_still_one_flex_row_in_the_old_order(self, page: Page):
        pretend_sharing(page, True)
        card = run(page, 'me')
        foot = card.locator('.card-foot')
        assert foot.evaluate('el => getComputedStyle(el).display') == 'flex'
        order = foot.evaluate(
            """el => [...el.children]
                   .filter(child => getComputedStyle(child).display !== 'none'
                                    && !child.classList.contains('visually-hidden'))
                   .map(child => child.className.split(' ')[0])""")
        assert order == ['segmented', 'spacer', 'open-full', 'share-system', 'download-menu']
        # The row is align-items: center, so one line means one shared centre
        # rather than one shared top -- the controls are different heights.
        centres = {round(box['top'] + box['height'] / 2)
                   for box in boxes(page, '.card-foot > *:not(.visually-hidden)')
                   if box['height']}
        assert len(centres) == 1, f'nothing wrapped, got {centres}'
        assert boxes(page, '.card-foot .download')[0]['height'] == 34, 'the old height'

    def test_the_phone_pills_are_not_on_the_desktop_picture(self, page: Page):
        card = run(page, 'me')
        expect(card.locator('.frame .segmented.view-pill')).to_be_hidden()
        expect(card.locator('.open-full-pill')).to_be_hidden()
        expect(card.locator('.open-full')).to_be_visible()

    def test_the_share_page_offers_one_copy_link_not_two(self, page: Page):
        """The footer's copy is the phone's. `.copy-wrap` sets no display of
        its own for exactly this reason: a rule that late in the file would
        beat `.mobile-only` and leave both on the row."""
        card = run(page, 'me')
        page.goto(card.locator('.card-share a.share').get_attribute('href'))
        expect(page.locator('.card-share .copy-link')).to_be_visible()
        expect(page.locator('.card-foot .copy-link')).to_be_hidden()

    def test_the_link_is_still_beside_the_expiry_line(self, page: Page):
        card = run(page, 'me')
        expect(card.locator('.card-share a.share')).to_be_visible()
        expect(card.locator('.card-foot a.share')).to_be_hidden()


class TestClearingUp:
    def test_a_card_can_be_removed(self, page: Page):
        card = run(page, 'me')
        card.locator('.remove').click()
        expect(page.locator('#queue article')).to_have_count(0)
        expect(page.locator('.empty')).to_be_visible()

    def test_clear_empties_the_whole_list(self, page: Page):
        page.goto('/')
        try_sample(page, 'me')
        try_sample(page, 'apollo')
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
        card.locator('.frame img.animated').evaluate('img => img.decode()')
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
        picture = card.locator('.frame img.animated')
        fitted = picture.bounding_box()['width']
        card.locator('.zoom').click()
        expect(card.locator('.zoom .out')).to_have_text('Fit')
        assert picture.bounding_box()['width'] > fitted
        card.locator('.zoom').click()
        assert picture.bounding_box()['width'] == fitted

    def test_the_footer_is_one_row_of_equal_buttons(self, page: Page):
        """Send, Get a link and Download, one row, nothing wrapped."""
        pretend_sharing(page, True)
        run(page, 'me')
        share, link, download = boxes(page, '.card-foot .share-system, '
                                            '.card-foot a.share, .card-foot .download')
        assert share['top'] == link['top'] == download['top'], 'one row, not two'
        assert share['height'] == link['height'] == download['height'] == 44
        widths = {round(box['width']) for box in (share, link, download)}
        assert len(widths) == 1, f'equal columns, got {widths}'

    def test_the_row_closes_up_when_the_browser_cannot_share(self, page: Page):
        """grid-auto-flow: column, so a hidden Send leaves two equal buttons
        rather than a row sized for three with a hole in it."""
        pretend_sharing(page, False)
        card = run(page, 'me')
        expect(card.locator('.share-system')).to_be_hidden()
        link, download = boxes(page, '.card-foot a.share, .card-foot .download')
        assert link['top'] == download['top']
        assert round(link['width']) == round(download['width'])

    def test_nothing_in_the_footer_wraps_on_the_smallest_phone(self, page: Page):
        """320px is an iPhone SE, and it is the width the row has to survive."""
        pretend_sharing(page, True)
        page.set_viewport_size({'width': 320, 'height': 568})
        run(page, 'me')
        rows = {box['top'] for box in boxes(page, '.card-foot .share-system, '
                                                  '.card-foot a.share, .card-foot .download')}
        assert len(rows) == 1, 'the three buttons are still on one line'

    def test_before_and_after_is_a_pill_on_the_picture(self, page: Page):
        card = run(page, 'me')
        pill = card.locator('.frame .segmented.view-pill')
        expect(pill).to_be_visible()
        expect(card.locator('.card-foot .segmented')).to_be_hidden()

        pill.locator('label[data-view="before"]').click()
        expect(card.locator('.frame img.before')).to_be_visible()
        expect(card.locator('.frame img.after')).to_be_hidden()

        pill.locator('label[data-view="after"]').click()
        expect(card.locator('.frame img.after')).to_be_visible()

        pill.locator('label[data-view="animated"]').click()
        expect(card.locator('.frame img.animated')).to_be_visible()
        expect(card.locator('.frame img.after')).to_be_hidden()

    def test_the_pill_and_the_full_size_one_share_the_picture_without_overlapping(
            self, page: Page):
        """Three tabs and Full size, on the narrowest phone there is. This is
        why the third one says GIF."""
        page.set_viewport_size({'width': 320, 'height': 568})
        card = run(page, 'me')
        pill = card.locator('.frame .segmented.view-pill').bounding_box()
        full = card.locator('.open-full-pill').bounding_box()
        apart = (pill['x'] + pill['width'] <= full['x']
                 or full['x'] + full['width'] <= pill['x']
                 or pill['y'] + pill['height'] <= full['y']
                 or full['y'] + full['height'] <= pill['y'])
        assert apart, f'the view pill runs into Full size: {pill} against {full}'

    def test_full_size_is_a_pill_and_the_view_lifts_it_over_the_footer(self, page: Page):
        card = run(page, 'me')
        opener = card.locator('.open-full-pill')
        expect(opener).to_be_visible()
        expect(card.locator('.open-full')).to_be_hidden(), 'the link is the desktop control'

        opener.click()
        expect(card.locator('.lightbox-close')).to_be_visible()
        expect(opener).to_be_hidden(), 'it is already open'
        pill = card.locator('.frame .segmented.view-pill')
        expect(pill).to_be_visible()
        assert pill.evaluate('el => getComputedStyle(el).position') == 'fixed'
        foot = card.locator('.card-foot').bounding_box()
        box = pill.bounding_box()
        assert box['y'] + box['height'] <= foot['y'], (
            'the pill sits above the buttons, not on them'
        )
        pill.locator('label[data-view="before"]').click()
        expect(card.locator('.frame img.before')).to_be_visible()

    def test_the_bar_is_a_tab_bar_and_the_open_tab_is_the_accent(self, page: Page):
        run(page, 'me')
        expect(page.locator('.addbar svg')).to_have_count(5)
        accent = page.evaluate("""() => {
            const probe = document.createElement('span');
            probe.style.color = 'var(--accent)';
            const bar = document.querySelector('.addbar');
            bar.appendChild(probe);
            const colour = getComputedStyle(probe).color;
            probe.remove();
            return colour;
        }""")
        tab = page.locator('.addbar-item.show-url')
        assert tab.evaluate('el => getComputedStyle(el).color') != accent
        page.locator('.addbar-item.show-url .open').click()
        assert tab.evaluate('el => getComputedStyle(el).color') == accent

    def test_the_site_footer_is_centred(self, page: Page):
        run(page, 'me')
        container = page.locator('.site-footer .container')
        assert container.evaluate('el => getComputedStyle(el).textAlign') == 'center'

    def test_the_share_page_leads_back_into_the_app(self, page: Page):
        card = run(page, 'me')
        page.goto(card.locator('a.share').first.get_attribute('href'))
        cta = page.locator('.shared-cta .btn')
        expect(cta).to_be_visible()
        assert round(cta.bounding_box()['width']) == 350, 'full width inside the padding'
        expect(page.locator('.card-foot .copy-link')).to_be_visible()
        expect(page.locator('.card-share .copy-wrap')).to_be_hidden()
        cta.click()
        expect(page.locator('.dropzone')).to_be_visible()

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
    @staticmethod
    def columns(page: Page) -> int:
        return page.locator('.samples').evaluate(
            'el => getComputedStyle(el).gridTemplateColumns.split(" ").length')

    def test_it_is_three_across_on_a_desktop(self, page: Page):
        page.goto('/')
        assert self.columns(page) == 3

    def test_it_is_three_across_on_a_phone(self, page: Page):
        """It used to pack by tile size -- three across a narrow column, four
        across a wide one -- so the grid was a different shape on every
        screen. The phone's sideways strip over the add bar is a different
        control and keeps its own layout."""
        page.set_viewport_size({'width': 320, 'height': 568})
        page.goto('/')
        assert self.columns(page) == 3

    def test_every_sample_has_a_face_in_it(self, page: Page):
        """A tile whose only answer is that there was nothing to draw on
        spends somebody's click on a joke."""
        page.goto('/')
        captions = page.locator('.sample .caption span').all_text_contents()
        assert captions, 'the grid is not empty'
        assert 'No faces' not in captions

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
        """Full-size JPEGs in ~92px squares was 4 MB of landing page."""
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


class TestTheDownloadMenu:
    def test_the_formats_are_behind_one_button(self, page: Page):
        card = run(page, 'me')
        menu = card.locator('.download-menu')
        expect(menu.locator('summary')).to_have_text('Download')
        expect(menu.locator('.formats')).to_be_hidden()
        menu.locator('summary').click()
        expect(menu.locator('.formats')).to_be_visible()
        assert menu.locator('.formats a').count() >= 2

    def test_a_format_link_points_at_the_full_resolution_file(self, page: Page):
        card = run(page, 'me')
        card.locator('.download-menu summary').click()
        link = card.locator('.download-menu .formats a').first
        assert re.search(r'/i/[0-9a-f-]+/result\.\w+$', link.get_attribute('href'))
        assert link.get_attribute('download').startswith('deal-with-it-')

    def test_the_animation_is_offered_last(self, page: Page):
        """Everything above it is the same picture in another encoding."""
        card = run(page, 'me')
        card.locator('.download-menu summary').click()
        links = card.locator('.download-menu .formats a')
        last = links.nth(links.count() - 1)
        expect(last).to_have_text('GIF')
        assert last.get_attribute('href').endswith('/animation.gif')
        assert last.get_attribute('download').endswith('.gif')


class TestTheSystemShareButton:
    def test_it_stays_hidden_where_files_cannot_be_shared(self, page: Page):
        """Headless chromium has no share sheet, which is the case the
        button has to survive: it must not be offered half-working."""
        card = run(page, 'me')
        expect(card.locator('button.share-system')).to_be_hidden()

    def test_it_appears_once_the_browser_says_it_can(self, page: Page, context):
        context.add_init_script(
            'navigator.share = () => Promise.resolve();'
            'navigator.canShare = () => true;'
        )
        card = run(page, 'me')
        expect(card.locator('button.share-system')).to_be_visible()

    def test_it_hands_the_picture_to_the_system(self, page: Page, context):
        """What a phone does with it -- Photos, WhatsApp -- is the system's
        business; ours is handing over a real file."""
        context.add_init_script("""
            window.__shared = null;
            navigator.canShare = () => true;
            navigator.share = (data) => {
                window.__shared = data.files.map(f => [f.name, f.type, f.size]);
                return Promise.resolve();
            };
        """)
        card = run(page, 'me')
        card.locator('button.share-system').click()
        page.wait_for_function('window.__shared !== null', timeout=15000)
        [[name, kind, size]] = page.evaluate('window.__shared')
        assert name.startswith('deal-with-it-')
        assert kind.startswith('image/')
        assert size > 0

    def test_it_sends_whichever_tab_is_open(self, page: Page, context):
        """A Send button beside a picture of the animation that sent the
        still was a button that lied. The card opens on the GIF; switching
        to Before changes what the sheet is handed."""
        context.add_init_script("""
            window.__shared = null;
            navigator.canShare = () => true;
            navigator.share = (data) => {
                window.__shared = data.files.map(f => [f.name, f.type]);
                return Promise.resolve();
            };
        """)
        card = run(page, 'me')
        card.locator('button.share-system').click()
        page.wait_for_function('window.__shared !== null', timeout=15000)
        [[name, kind]] = page.evaluate('window.__shared')
        assert kind == 'image/gif' and name.endswith('.gif'), 'the tab it opened on'

        page.evaluate('window.__shared = null')
        card.locator('.card-foot label[data-view="before"]').click()
        card.locator('button.share-system').click()
        page.wait_for_function('window.__shared !== null', timeout=15000)
        [[name, kind]] = page.evaluate('window.__shared')
        assert kind == 'image/webp'
        assert name.endswith('-before.webp'), 'and it does not overwrite the result'


class TestFetchingTheOtherTabs:
    """A card fetches all three pictures, in order, as soon as it lands.

    Nobody opens a result to look at one tab, so the tabs it is not showing
    are no longer left to their own click. They are still deferred in the
    markup -- that is what keeps this an order rather than three downloads
    racing the one being looked at -- and `views.js` releases them one at a
    time behind the GIF.
    """

    LOADED = 'el => el.complete && el.naturalWidth > 0'

    def loaded(self, page: Page, card, tab: str, timeout: int = 20000) -> None:
        page.wait_for_function(self.LOADED, timeout=timeout,
                               arg=card.locator(f'.frame img.{tab}').element_handle())

    def test_the_tabs_a_card_is_not_showing_ship_deferred(self, page: Page):
        """The hint is the queue: released together, all three would start at
        once and the one being looked at would be racing the other two.

        Read off the served markup, not the live DOM -- by the time a card is
        on screen the script has already begun promoting them, which is the
        whole point of it.
        """
        card = run(page, 'me')
        job_id = card.get_attribute('id').removeprefix('job-')
        markup = page.request.get(f'/jobs/{job_id}',
                                  headers={'HX-Request': 'true'}).text()
        opened = markup.split('class="animated"')[0].rsplit('<img', 1)[1]
        assert 'loading' not in opened, 'the open tab is not deferred'
        assert markup.count('loading="lazy"') == 2, 'the other two are'

    def test_every_tab_is_fetched_without_being_asked_for(self, page: Page):
        card = run(page, 'me')
        for tab in ('animated', 'after', 'before'):
            self.loaded(page, card, tab)

    def test_the_switch_is_instant_once_they_are_in(self, page: Page):
        """The point of the whole thing: a press, and the picture is there."""
        card = run(page, 'me')
        self.loaded(page, card, 'before')
        card.locator('.card-foot label[data-view="before"]').click()
        expect(card.locator('.frame img.before')).to_be_visible()
        assert card.locator('.frame img.before').evaluate(self.LOADED)

    def test_they_arrive_gif_then_after_then_before(self, page: Page):
        """One at a time and in that order, so the tab being looked at is
        never made to share a connection with two nobody has asked for."""
        page.goto('/')
        finished = []
        page.on('response', lambda response: finished.append(response.url)
                if '/i/' in response.url else None)
        try_sample(page, 'me')
        card = first_card(page)
        expect(card.locator('.chip.done')).to_be_visible(timeout=TIMEOUT)
        self.loaded(page, card, 'before')
        pictures = [url.rsplit('/', 1)[1] for url in finished]
        # The thumbnail a running card shows comes through here too, and a
        # picture can be asked for twice; what matters is the order these
        # three were first reached for.
        order = []
        for name in pictures:
            if name in ('animation.gif', 'view.webp', 'before.webp') and name not in order:
                order.append(name)
        assert order == ['animation.gif', 'view.webp', 'before.webp'], pictures

class TestSharing:
    def test_a_finished_card_links_to_a_page_worth_passing_on(self, page: Page):
        card = run(page, 'me')
        # Two of them now: the desktop's is the one beside the expiry line.
        card.locator('.card-share a.share').click()
        expect(page.locator('h1')).to_contain_text('Someone dealt with it')
        expect(page.locator('.frame img.animated')).to_be_visible()
        page.locator('.frame img.animated').evaluate('img => img.decode()')


class TestNotFound:
    @pytest.mark.expects_404
    def test_an_unknown_url_gets_the_page(self, page: Page):
        page.goto('/no-such-thing')
        expect(page.locator('h1')).to_contain_text('Nothing here')
        expect(page.locator('.site-footer')).to_be_visible()


class TestTheSkeleton:
    """The placeholder a card wears until its first picture paints.

    An `<img>` with nothing in it yet has no height, so a finished card used
    to arrive as a footer with a gap above it and then jump when the picture
    landed. The frame now reserves the shape and runs a sheen across it.
    """

    SHEEN = "el => getComputedStyle(el, '::after').display"

    def test_it_holds_the_shape_until_a_picture_lands(self, page: Page):
        card = run(page, 'me')
        frame = card.locator('.frame')
        # Put the card back in the state it arrives in. Racing a real fetch
        # would test the network, not the rule.
        frame.evaluate("el => el.closest('.result').removeAttribute('data-ready')")
        assert frame.evaluate(self.SHEEN) != 'none', 'no sheen over the gap'
        assert frame.evaluate('el => parseFloat(getComputedStyle(el).minHeight)') == 260

    def test_it_retires_when_the_open_tab_has_painted(self, page: Page):
        card = run(page, 'me')
        page.wait_for_function("el => el.dataset.ready === '1'", timeout=20000,
                               arg=card.locator('.result').element_handle())
        frame = card.locator('.frame')
        assert frame.evaluate(self.SHEEN) == 'none', 'still animating behind the picture'
        assert frame.evaluate('el => getComputedStyle(el).minHeight') != '260px', (
            'a short picture would sit in a reserved box with grey above it'
        )

    def test_the_picture_covers_it_with_no_script_in_the_path(self, page: Page):
        """`data-ready` is a tidy-up. The sheen sits under everything in the
        frame, so a browser that ran none of our script still shows the
        result -- and the tap target still takes the click."""
        card = run(page, 'me')
        frame = card.locator('.frame')
        frame.evaluate("el => el.closest('.result').removeAttribute('data-ready')")
        assert frame.evaluate("el => getComputedStyle(el, '::after').zIndex") == '-1'
        assert frame.evaluate('el => getComputedStyle(el).zIndex') == '0', (
            'the -1 needs a stacking context here or it falls through'
        )
        # Which is the whole reason it is done this way round: lifting the
        # image over the skeleton instead put it over `.frame-tap` and the
        # phone's pills, and tapping a picture stopped opening it.
        # TestOnAPhone::test_tapping_the_picture_opens_it_full_size is where
        # that shows up, because the tap target is a phone's alone.
        assert card.locator('.frame img.animated').evaluate(
            'el => getComputedStyle(el).zIndex') == 'auto'
