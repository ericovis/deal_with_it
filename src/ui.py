"""The interface, in FastHTML.

Two pages: the app at ``/`` and the docs at ``/docs``. Submitting a picture
swaps a card into ``#queue``; the card polls itself until the worker is done
and then turns into the result in place. Four pieces of script in the whole
interface, three of them enhancements the page reads correctly without: the
clipboard call on the docs page, ``UPLOAD_ONE_BY_ONE`` (which htmx has no
attribute for), ``countdown.js``, which turns a server-rendered expiry into a
ticking one, and ``share.js``, which reveals a button that cannot honestly be
offered until the browser has said it can share a file.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from fasthtml.common import (
    H1,
    H2,
    H3,
    A,
    Article,
    Aside,
    B,
    Button,
    Code,
    Details,
    Div,
    Em,
    Figcaption,
    Figure,
    Footer,
    Form,
    Header,
    Html,
    Img,
    Input,
    Label,
    Link,
    Main,
    Meta,
    Nav,
    P,
    Pre,
    Progress,
    Script,
    Section,
    Span,
    Summary,
    Time,
    Title,
    UploadFile,
    cookie,
    fast_app,
    to_xml,
)
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.responses import HTMLResponse, JSONResponse, Response

from src import blobs, images, jobs, throttle
from src.assets import asset
from src.config import get_settings
from src.models import (
    ImageRequest,
    JobImages,
    JobResult,
    JobSource,
    JobState,
    SourceKind,
)

logger = logging.getLogger(__name__)

POLL_INTERVAL = '1s'
HEALTH_INTERVAL = '30s'
REPO = 'https://github.com/ericovis/deal_with_it'
MEME = 'https://knowyourmeme.com/memes/deal-with-it'
LICENSE_URL = f'{REPO}/blob/master/LICENSE'
IMG_DIR = Path(__file__).resolve().parent / 'static' / 'img'
THEME_COOKIE = 'dwi_theme'
#: A year. The cookie carries a preference, not a session.
THEME_MAX_AGE = 60 * 60 * 24 * 365

#: The band colour in each palette. The band is what a translucent status
#: bar sits on, so it is also what ``theme-color`` reports.
BAND_COLOURS = {'light': '#24261F', 'dark': '#0f100c'}

EXPIRED = 'That job has expired. Try again?'
NOTHING_SUBMITTED = 'No image or URL was provided!'
URL_LOOKS_FINE = 'Looks good. Press Enter or Deal with it.'

KIND_LABELS = {
    SourceKind.UPLOAD: 'Uploaded file',
    SourceKind.URL: 'From URL',
    SourceKind.SAMPLE: 'Sample picture',
}

CHIPS = {
    JobState.QUEUED: ('Queued', 'chip'),
    JobState.STARTED: ('Working', 'chip working'),
    JobState.FINISHED: ('Done', 'chip done'),
    JobState.FAILED: ('Failed', 'chip failed'),
}


@dataclass(frozen=True)
class Sample:
    filename: str
    #: What the detector finds, in words. Pinned by tests/test_processor.py.
    title: str
    media_type: str


#: Public domain, credited in src/static/img/CREDITS.md, shown in this order.
SAMPLES = {
    'me': Sample('me.jpg', 'One face', 'image/jpeg'),
    'apollo': Sample('apollo_11_crew.jpg', 'Three faces', 'image/jpeg'),
    'group': Sample('multiple_people.jpg', 'Eight faces', 'image/jpeg'),
    'solvay': Sample('solvay_1927.jpg', 'Twenty-nine faces', 'image/jpeg'),

    'mona_lisa': Sample('mona_lisa.jpg', 'One face', 'image/jpeg'),
    'pearl_earring': Sample(
        'girl_with_a_pearl_earring.jpg', 'One face', 'image/jpeg'),
    'american_gothic': Sample('american_gothic.jpg', 'Two faces', 'image/jpeg'),
    'syndics': Sample('the_syndics.jpg', 'Six faces', 'image/jpeg'),
    'night_watch': Sample('the_night_watch.jpg', 'Nineteen faces', 'image/jpeg'),

    'dog_handler': Sample('dog_handler.jpg', 'One face', 'image/jpeg'),
    'princess_mary': Sample(
        'princess_mary_and_nelson.jpg', 'Two faces', 'image/jpeg'),
    'poker': Sample('dogs_playing_poker.jpg', 'Three faces', 'image/jpeg'),
    'cat_nap': Sample('cat_nap.jpg', 'No faces', 'image/jpeg'),
    'socks': Sample('socks_the_cat.jpg', 'No faces', 'image/jpeg'),

    'van_gogh': Sample('van_gogh_self_portrait.jpg', 'No faces', 'image/jpeg'),
    'scream': Sample('the_scream.jpg', 'No faces', 'image/jpeg'),
}

#: Which sample the API example points at, and where that picture came from.
EXAMPLE_SAMPLE = 'apollo'
EXAMPLE_SOURCE = 'https://upload.wikimedia.org/wikipedia/commons/3/3d/Apollo_11_Crew.jpg'


def sample_source(name: str) -> bytes:
    """A sample picture as submitted: the committed file, at full size.

    Copied into the job's directory rather than fetched from our own
    ``/static``, because the worker's SSRF guard refuses our own non-public
    host. Never the tile: SAMPLE_FACES pins a face count per sample and those
    counts move when the width does.
    """
    return (IMG_DIR / SAMPLES[name].filename).read_bytes()


def _first_message(exc: ValidationError) -> str:
    """The one sentence out of a pydantic error worth showing a person."""
    message = exc.errors()[0]['msg'].removeprefix('Value error, ')
    return message.split(', relative URL without a base')[0]


def _absolute(path: str) -> str:
    base = get_settings().public_url.rstrip('/')
    return f'{base}{path}' if base else path


def _example_url() -> str:
    """A URL for the API example that will actually fetch: our own copy once
    ``public_url`` is set, otherwise the picture's source on Commons. A
    localhost URL would be refused by the worker's SSRF guard."""
    base = get_settings().public_url.rstrip('/')
    if base.startswith(('http://', 'https://')):
        return f'{base}/static/img/{SAMPLES[EXAMPLE_SAMPLE].filename}'
    return EXAMPLE_SOURCE


# --------------------------------------------------------------- the shell


def _theme_colour(theme: str | None) -> tuple:
    """``theme-color``: once when the theme is known, twice when it is not.

    With no cookie the OS decides, so both colours go out behind a ``media``
    query. With one, a single unconditional tag makes the browser chrome
    follow the chosen theme instead of the OS.
    """
    if theme in BAND_COLOURS:
        return (Meta(name='theme-color', content=BAND_COLOURS[theme]),)
    return (
        Meta(name='theme-color', content=BAND_COLOURS['light'],
             media='(prefers-color-scheme: light)'),
        Meta(name='theme-color', content=BAND_COLOURS['dark'],
             media='(prefers-color-scheme: dark)'),
    )


def head_tags(title: str, theme: str | None, image: str | None = None,
              description: str | None = None) -> tuple:
    """Metadata that belongs in <head>. FastHTML hoists these out of a page.

    ``image`` overrides the social card, which is what makes a shared result
    unfurl into the actual picture rather than the site's own artwork.
    """
    card = _absolute(image or '/static/img/deal_with_me.png')
    blurb = description or 'A Python API for creating "Deal With It"-like Images'
    return (
        Title(title),
        # viewport-fit=cover lets the band paint under a phone's status bar;
        # the safe-area insets in style.css keep its contents clear of it.
        Meta(name='viewport',
             content='width=device-width,initial-scale=1,viewport-fit=cover'),
        Meta(name='author', content='Eric Magalhães'),
        Meta(name='description', content=blurb),
        Meta(name='application-name', content='Deal With It!'),
        Meta(name='apple-mobile-web-app-title', content='Deal With It!'),
        Meta(name='apple-mobile-web-app-capable', content='yes'),
        Meta(name='mobile-web-app-capable', content='yes'),
        Meta(name='apple-mobile-web-app-status-bar-style', content='black-translucent'),
        Meta(name='color-scheme', content='light dark'),
        *_theme_colour(theme),
        Meta(name='twitter:card', content='summary_large_image'),
        Meta(name='twitter:creator', content='@ericovis'),
        Meta(name='twitter:title', content='Deal with it!'),
        Meta(name='twitter:description', content=blurb),
        Meta(name='twitter:image', content=card),
        Meta(property='og:title', content='Deal with it!'),
        Meta(property='og:type', content='website'),
        Meta(property='og:image', content=card),
        Meta(property='og:description', content=blurb),
        Link(rel='icon', href=asset('/static/img/favicon.png'), type='image/png',
             sizes='16x16'),
        Link(rel='icon', href=asset('/static/img/icons/favicon-64.png'), type='image/png',
             sizes='64x64'),
        Link(rel='apple-touch-icon', href=asset('/static/img/icons/apple-touch-icon.png'),
             sizes='180x180'),
        Link(rel='manifest', href=asset('/static/manifest.webmanifest')),
        Link(rel='preconnect', href='https://fonts.googleapis.com'),
        Link(rel='stylesheet', href='https://fonts.googleapis.com/css2?'
                                    'family=Manrope:wght@400;500;600;700;800&'
                                    'family=Fira+Code:wght@400;500&display=swap'),
        Link(rel='stylesheet', href=asset('/static/css/style.css')),
        # The two scripts that are not htmx wiring. Both are enhancements:
        # the countdown turns a server-rendered expiry into a ticking one, and
        # share.js reveals a button that cannot be offered without first
        # asking the browser whether it can share a file at all.
        Script(src=asset('/static/js/countdown.js'), defer=True),
        Script(src=asset('/static/js/share.js'), defer=True),
    )


def theme_state(theme: str | None) -> Span:
    """The element ``style.css`` reads the theme from. An absent attribute
    means nobody has chosen, and ``prefers-color-scheme`` decides."""
    return Span(id='theme-state', hidden=True, data_theme=theme)


def theme_switch(theme: str | None) -> Span:
    """A checkbox drawn as a switch. Its visuals come from the theme tokens,
    not ``:checked``, so the knob shows the theme actually in force."""
    return Span(
        Input(type='checkbox', id='theme', name='theme', value='dark',
              checked=theme == 'dark', cls='visually-hidden', role='switch',
              aria_label='Dark theme',
              hx_post='/theme', hx_trigger='change',
              hx_target='#theme-state', hx_swap='outerHTML'),
        Label(Span(cls='knob'), fr='theme', cls='switch',
              title='Switch between light and dark'),
        theme_state(theme),
        cls='switch-wrap',
    )


def status_pill(reachable: bool) -> Span:
    """Whether the broker is answering. Refreshes itself in place."""
    return Span(
        'workers ready' if reachable else 'queue unreachable',
        id='health',
        cls='pill' if reachable else 'pill down',
        title='GET /api/health',
        hx_get='/health',
        hx_trigger=f'every {HEALTH_INTERVAL}',
        hx_swap='outerHTML',
    )


def site_header(active: str, theme: str | None, reachable: bool) -> Header:
    def nav_link(label: str, href: str, key: str = '') -> A:
        return A(label, href=href, cls='here' if key and key == active else None)

    return Header(
        Div(
            A(Img(src=asset('/static/img/glasses.svg'), alt=''), Span('Deal With It!'),
              href='/', cls='brand'),
            Nav(
                # Wrapped so the mobile grid can place the three of them as
                # one cell. `display: contents` above 600px, so the desktop
                # band is unchanged.
                Div(
                    nav_link('App', '/', 'app'),
                    nav_link('Docs', '/docs', 'docs'),
                    nav_link('GitHub ↗', REPO),
                    cls='nav-links',
                ),
                status_pill(reachable),
                theme_switch(theme),
                cls='nav',
            ),
            cls='container',
        ),
        cls='band',
    )


def site_footer(active: str) -> Footer:
    other = A('API docs', href='/docs') if active == 'app' else A('App', href='/')
    return Footer(
        Div(
            Span('© 2018–2026 ', A('Eric Magalhães', href='https://ericovis.com')),
            Span(A('MIT License', href=LICENSE_URL), other, A('GitHub', href=REPO),
                 cls='links'),
            cls='container',
        ),
        cls='site-footer',
    )


# ------------------------------------------------------------- the app page


def url_hint(message: str = '', ok: bool = False, oob: bool = False) -> P:
    """The line under the URL field. Empty when there is nothing to say."""
    return P(
        message,
        id='url-hint',
        cls=('ok' if ok else 'error') if message else None,
        aria_live='polite',
        hx_swap_oob='true' if oob else None,
    )


#: Posting a file input the ordinary way sends every file in one body, so
#: nothing appears until the last byte of the last picture has arrived. This
#: sends one request per file instead, in order, so each card lands as its own
#: upload finishes. htmx has no attribute that splits a file input, which is
#: why this is script rather than markup.
#:
#: ``etc.values`` *replaces* the ``image`` htmx collected from the form
#: (``overrideFormData`` deletes the key before appending), so each request
#: carries exactly one file. The input is emptied up front anyway: the files
#: are already in hand, and an empty input stops the form's own submit button
#: from re-sending the ones still waiting.
UPLOAD_ONE_BY_ONE = (
    "const input = this, files = [...input.files]; input.value = '';"
    'void (async () => { for (const file of files) {'
    " await htmx.ajax('POST', '/submit', {source: input, target: '#queue',"
    " swap: 'afterbegin', values: {image: file}})"
    # A dropped connection rejects, and one bad file must not swallow the
    # rest of the batch -- nor raise, which htmx has already reported.
    '  .catch(() => {}); } })();'
)


def upload_form(replace: bool = False) -> Form:
    """The submit form. With ``replace`` it swaps out of band over the one
    on the page, which is how a submission clears the fields.

    The file input sits before the card on purpose: the card is the file
    input's label, and dropping onto a file input's label counts as choosing.

    One input for every device. A phone's picker already offers the camera
    beside the library, so a second `capture` input only made the same sheet
    open from two places -- and `capture` actively *hides* the library, which
    was the opposite of what the second button promised.
    """
    return Form(
        Input(type='file', id='files', name='image', accept='image/*', multiple=True,
              cls='visually-hidden', hx_encoding='multipart/form-data',
              **{'hx-on:change': UPLOAD_ONE_BY_ONE}),
        Div(
            Label(
                Img(src=asset('/static/img/glasses.svg'), alt=''),
                # Two sets of words for one card: dropping is a desktop idea
                # and a phone has no computer to browse. Swapped by media
                # query rather than by sniffing the browser.
                Span('Drop pictures here', cls='title desktop-only'),
                Span('Add a picture', cls='title mobile-only'),
                Span('or ', Em('browse your computer'), cls='sub desktop-only'),
                Span('Camera or library, whichever you like', cls='sub mobile-only'),
                fr='files', cls='dropzone-face',
            ),
            Span('PNG or JPEG, up to 10 MB each. Several at once is fine.', cls='limits'),
            cls='dropzone',
        ),
        Div('or a URL', cls='divider'),
        Div(
            Input(type='text', id='url', name='url',
                  placeholder='https://example.com/photo.jpg',
                  autocomplete='off', spellcheck='false', inputmode='url',
                  hx_post='/validate', hx_trigger='input changed delay:400ms',
                  hx_target='#url-hint', hx_swap='outerHTML',
                  hx_sync='closest form:abort'),
            Button('Deal with it', type='submit', cls='btn'),
            cls='url-row',
        ),
        url_hint(),
        id='form',
        hx_swap_oob='true' if replace else None,
        hx_post='/submit',
        hx_target='#queue',
        hx_swap='afterbegin',
        hx_encoding='multipart/form-data',
        hx_disabled_elt='find button',
        # Otherwise the URL input inherits it and htmx logs a selector error
        # on every keystroke.
        hx_disinherit='hx-disabled-elt',
    )


def tile_url(sample: Sample) -> str:
    """The picture the grid shows. Never the one it submits: the counts in
    the captions depend on the original's width, so display and submission
    must not share a file. ``scripts/make_tiles.py`` writes these."""
    return asset(f'/static/img/tiles/{Path(sample.filename).stem}.webp')


def sample_tile(name: str) -> Button:
    sample = SAMPLES[name]
    return Button(
        # Square because the CSS crops it square anyway; width and height so
        # the grid does not reflow as sixteen of them arrive.
        Img(src=tile_url(sample), alt='', width=200, height=200),
        Span(B(sample.title), Span(sample.filename), cls='caption'),
        type='button',
        cls='sample',
        hx_post='/submit',
        hx_vals=json.dumps({'sample': name}),
        hx_target='#queue',
        hx_swap='afterbegin',
    )


def app_pane() -> Aside:
    return Aside(
        Div(
            H1('Give it a picture, get it back with the meme sunglasses on '
               'every face in it.'),
            P('Faces are found by a background worker, so a result takes a few '
              'seconds. ', A('How it works', href='/docs')),
            cls='lede',
        ),
        upload_form(),
        Div(
            P(Span('Or ', cls='lead-in'), 'try a sample', cls='samples-label'),
            Div(*(sample_tile(name) for name in SAMPLES), cls='samples'),
            cls='sample-strip',
        ),
        cls='pane',
    )


def session_section() -> Section:
    return Section(
        Div(
            H2('This session'),
            Button('Clear', type='button', cls='clear',
                   hx_get='/empty', hx_target='#queue', hx_swap='innerHTML'),
            cls='session-head',
        ),
        P('Results live in this tab only. Reloading loses them, so download '
          'anything you want to keep.',
          Span(' Swipe a card left to remove it.', cls='mobile-only'), cls='note'),
        Div(id='queue'),
        # After #queue: the CSS that reveals it is a sibling combinator.
        Div('Nothing here yet. Drop a picture, paste a URL, or try a sample.', cls='empty'),
        cls='session',
    )


def addbar() -> Nav:
    """The bar a phone gets once there is something in the list.

    "Add a picture" is a label for the one file input, which is the same
    control the dropzone card is. A phone's picker already offers the camera
    alongside the library, so splitting it in two only made the same sheet
    open from two places. URL and Samples are labels for radios: checking one
    is what the CSS re-lays the URL row or the sample grid over the bar for,
    and #show-none is how a second tap on the open one closes it again.
    """
    def panel(text: str, target: str) -> Div:
        return Div(
            Label(text, fr=target, cls='open'),
            Label(text, fr='show-none', cls='close'),
            cls=f'addbar-item {target}',
        )

    return Nav(
        Input(type='radio', id='show-none', name='addbar', checked=True,
              cls='visually-hidden', aria_label='Close the panel'),
        Input(type='radio', id='show-url', name='addbar', cls='visually-hidden',
              aria_label='Submit a URL'),
        Input(type='radio', id='show-samples', name='addbar', cls='visually-hidden',
              aria_label='Show the samples'),
        Label('Add a picture', fr='files', cls='addbar-item add'),
        panel('URL', 'show-url'),
        panel('Samples', 'show-samples'),
        cls='addbar',
    )


def page(theme: str | None, reachable: bool) -> tuple:
    return (
        *head_tags('Deal With It!', theme),
        site_header('app', theme, reachable),
        Main(app_pane(), session_section(), cls='container layout'),
        addbar(),
        site_footer('app'),
    )


def expiry_line(expires_at) -> Time:
    """When this result stops existing.

    The absolute time is the truth and is rendered server-side, so the
    sentence is correct with JavaScript off. countdown.js turns it into a
    ticking one; `datetime` is what it reads.
    """
    return Time(
        f'This image will be deleted at {expires_at:%H:%M} UTC',
        datetime=expires_at.strftime('%Y-%m-%dT%H:%M:%SZ'),
        data_expires=True,
        cls='expiry',
    )


def llms_txt() -> str:
    """The /llms.txt an agent reads before trying to use this.

    Generated rather than committed, because half of it is numbers that live
    in Settings -- a static file would quietly start lying the first time
    someone changed a limit. Follows llmstxt.org: a title, a summary
    blockquote, then sections of links.
    """
    settings = get_settings()
    base = settings.public_url.rstrip('/') or 'http://localhost:5000'
    megabytes = settings.max_image_bytes // 1024 // 1024
    minutes = settings.blob_ttl // 60
    return f"""# Deal With It!

> Puts the "Deal With It" sunglasses on every face in a picture. Submit an
> image by URL or as base64, poll a job id, and get back links to the result
> in several sizes and formats. Free, unauthenticated, and rate limited.

Processing is asynchronous: detection takes a few seconds, so a submission
returns a job id and you poll for the result. Results are deleted after
{minutes} minutes -- fetch anything you want to keep.

## Using the API

- [OpenAPI schema]({base}/api/openapi.json): the machine-readable contract.
- [Interactive docs]({base}/api/docs): the same, with a try-it button.
- [How it works]({base}/docs): prose, with the detection pipeline explained.

Two calls, in order:

```
POST {base}/api/jobs      {{"url": "https://example.com/photo.jpg"}}
                          -> 202 {{"job_id": "...", "status_url": "..."}}

GET  {base}/api/jobs/ID   -> {{"state": "queued"|"started"|"finished"|"failed"}}
```

Poll once a second. When `state` is `finished`:

- `images.view` -- WebP, 1600px. Show this to a person.
- `images.thumb` -- WebP, 160px. Appears *before* the job finishes.
- `images.before` -- the submitted picture, same size as `view`.
- `images.full` -- full resolution, in the format it was submitted in.
- `images.card` -- JPEG, 1200px, for `og:image` where WebP is not safe.
- `downloads` -- the full-resolution result keyed by format.
- `faces` -- a box, a score and 68 landmarks per face, in submitted-image
  coordinates. Useful if you want to draw something else instead.
- `expires_at` -- when every link above stops working.
- `share_url` -- a page showing the result, safe to hand to someone.

## Limits

- One image per request, up to {megabytes} MB and {settings.max_image_pixels:,} pixels.
- {settings.rate_limit} submissions per {settings.rate_window} seconds per client;
  over that is `429` with `Retry-After`.
- A full queue is `503`, also with `Retry-After`.
- A job is given {settings.job_timeout} seconds before it is abandoned.
- URLs must be public: private and loopback addresses are refused.

## Notes

- A `failed` job carries a readable `error` -- an unreachable URL, an
  undecodable image, or no faces found. Those are outcomes, not bugs.
- The detector finds dogs sometimes. The score does not separate them from
  people, so there is no threshold that would.
- Nothing here is durable storage, and there is no authentication. Do not
  submit anything you would mind a stranger seeing at an unguessable URL.

## Optional

- [Source]({REPO}): MIT licensed.
- [Load and limits]({REPO}/blob/master/docs/LOAD.md): measured throughput,
  memory and abuse behaviour.
"""


def not_found_page(theme: str | None, reachable: bool,
                   heading: str = 'Nothing here',
                   message: str = 'That page does not exist.',
                   expired: bool = False) -> tuple:
    """What a wrong or worn-out URL gets.

    Its own page rather than a bare status line, because most of the 404s this
    app produces are not typos: they are shared links whose pictures have
    expired, and "gone" is a different thing to say than "never existed".
    """
    return (
        *head_tags(f'{heading} | Deal With It', theme),
        site_header('app', theme, reachable),
        Main(
            Section(
                Img(src=asset('/static/img/glasses.svg'), alt='', cls='lost-glasses'),
                H1(heading),
                P(message, cls='lead'),
                P(
                    A('Deal with another picture', href='/', cls='btn'),
                    A('How it works', href='/docs', cls='btn ghost'),
                    cls='lost-actions',
                ),
                cls='lost',
            ),
            cls='container layout',
        ),
        site_footer('app'),
    )


def not_found_response(theme: str | None, **kwargs) -> HTMLResponse:
    """The 404 page as a response. Rendered here rather than returned as
    components, because an exception handler is plain Starlette and does not
    go through FastHTML's own rendering."""
    document = not_found_page(theme, jobs.is_broker_reachable(), **kwargs)
    # to_xml on an Html element emits the doctype itself.
    return HTMLResponse(to_xml(Html(*document)), status_code=404)


def share_page(job_id: str, record: dict, theme: str | None, reachable: bool) -> tuple:
    """One result, standing on its own, for someone who was not here.

    Rendered from the job's own meta.json rather than from Redis: the job
    record expires at `result_ttl` and the pictures at `blob_ttl`, so a
    Redis-backed page would go dark while its own images were still up.
    """
    pictures = JobImages(**{role: blobs.url(ref) for role, ref in record['images'].items()})
    downloads = {fmt: blobs.url(ref) for fmt, ref in (record.get('downloads') or {}).items()}
    expires_at = datetime.fromisoformat(record['expires_at'])
    faces = record.get('faces') or 0
    caption = ('Deal With It — glasses on '
               f'{faces} face{"" if faces == 1 else "s"}')
    return (
        *head_tags(f'{caption} | Deal With It', theme,
                   image=pictures.card or pictures.full, description=caption),
        site_header('app', theme, reachable),
        Main(
            Section(
                H1('Someone dealt with it'),
                P(caption, cls='lead'),
                _result_view(job_id, pictures, downloads, expires_at=expires_at),
                cls='shared',
            ),
            cls='container layout',
        ),
        site_footer('app'),
    )


EXPIRED_HEADING = 'That one has expired'
EXPIRED_MESSAGE = (
    'Results are kept for a short while and then deleted, so this link has '
    'outlived its picture. Nothing was lost that was not going to be.'
)


# ------------------------------------------------------------------- cards


def _faces(count: int | None) -> str:
    if count is None:
        return ''
    return f' · {count} face' if count == 1 else f' · {count} faces'


def _wait(ahead: int | None) -> str:
    """How much queue is in front of a card, in words.

    None means the id is not in the queue list: either a worker already has
    it, or this is one of RQ's deferred/scheduled states, which we never
    create. Both read the same to someone waiting.
    """
    if ahead is None:
        return 'In the queue'
    if ahead == 0:
        return 'Next up'
    return '1 job ahead' if ahead == 1 else f'{ahead} jobs ahead'


def _subtitle(result: JobResult, source: JobSource) -> str:
    kind = KIND_LABELS[source.kind]
    if result.state == JobState.STARTED:
        step = result.step or 'Working on it'
        return f'{step} · {result.progress}%' if result.progress is not None else step
    if not result.state.is_terminal:
        return _wait(result.ahead)
    if result.state == JobState.FINISHED:
        return f'{kind}{_faces(source.faces)}'
    return kind


def download_menu(job_id: str, downloads: dict[str, str]) -> Details:
    """One Download control that opens the formats.

    A `<details>` because it needs no script and closes on click-away with
    `::details-content`; the same element the docs contents list uses. The
    formats are whichever ones the worker wrote, which depends on what was
    submitted, so the list is never longer than it is useful.
    """
    return Details(
        Summary('Download', cls='download'),
        Div(
            *[A(fmt.upper(), href=url, download=f'deal-with-it-{job_id[:8]}.{fmt}')
              for fmt, url in sorted(downloads.items())],
            cls='formats',
        ),
        cls='download-menu',
    )


def _result_view(job_id: str, pictures: JobImages, downloads: dict[str, str],
                 share_url: str | None = None, expires_at=None) -> Div:
    """The finished image, its Before/After toggle, and the full-screen view.

    The full-screen view re-lays out the *same* frame rather than a copy, so
    the picture is referenced once. A checkbox drives it because ``<dialog>``
    cannot open without script.
    """
    toggle = f'full-{job_id}'
    zoom = f'zoom-{job_id}'
    image, before = pictures.view, pictures.before
    footer = [
        Span(cls='spacer'),
        Input(type='checkbox', id=toggle, cls='full-toggle visually-hidden'),
        Label(Span('View full size', cls='desktop-only'),
              Span('Full size', cls='mobile-only'), fr=toggle, cls='open-full'),
        # Only ever shown inside the overlay, and only on a phone: the frame
        # scrolled at 200% for a picture that fitting to the screen has made
        # too small to read. Native pinch still works either way.
        Input(type='checkbox', id=zoom, cls='zoom-toggle visually-hidden'),
        Label(Span('Zoom', cls='in'), Span('Fit', cls='out'), fr=zoom, cls='zoom'),
        # Hidden until a browser says it can actually share a file, which
        # share.js does. On a phone this is the system sheet -- save to
        # Photos, send on WhatsApp -- and there is no way to offer that
        # without asking the browser first.
        Button('Share', type='button', cls='share-system', hidden=True,
               data_share=pictures.full, data_name=f'deal-with-it-{job_id[:8]}'),
        download_menu(job_id, downloads),
    ]
    # Tapping the picture opens it, which is what a phone expects. Sized over
    # the frame rather than wrapping it, because the frame is also the
    # overlay: display:none there, so it cannot swallow the pinch.
    tap = Label(fr=toggle, cls='frame-tap', aria_label='View full size')

    if before is None:
        frame = Div(Img(src=image, alt='Result'), tap, cls='frame')
        controls = footer
    else:
        name = f'view-{job_id}'
        frame = Div(
            Img(src=before, alt='The picture as submitted', cls='before'),
            Img(src=image, alt='Result', cls='after'),
            tap,
            cls='frame',
        )
        controls = [
            Div(
                Input(type='radio', id=f'{name}-b', name=name, value='before'),
                Label('Before', fr=f'{name}-b', data_view='before'),
                Input(type='radio', id=f'{name}-a', name=name, value='after', checked=True),
                Label('After', fr=f'{name}-a', data_view='after'),
                cls='segmented',
            ),
            *footer,
        ]

    below = []
    if share_url or expires_at:
        below.append(Div(
            # Not "Share": the button above already is, and it does a
            # different thing. This one opens a page to send someone.
            *( [A('Get a link', href=share_url, cls='share')] if share_url else [] ),
            *( [expiry_line(expires_at)] if expires_at else [] ),
            cls='card-share',
        ))

    return Div(
        frame,
        # Clicking the space around the image closes it.
        Label(fr=toggle, cls='backdrop', aria_label='Close the full-size view'),
        Span('pinch or tap Zoom', cls='zoom-hint'),
        Label('×', fr=toggle, cls='lightbox-close', title='Close',
              aria_label='Close the full-size view'),
        Div(*controls, cls='card-foot'),
        *below,
        cls='result',
    )


def card(job_id: str, result: JobResult | None, source: JobSource,
         retryable: bool = True, dom_id: str | None = None) -> Article:
    """One job, in whatever state it is in.

    Queued and started cards carry the hx-get that fetches the next state;
    terminal ones do not, so the poll chain ends by itself. ``result is
    None`` means RQ has never heard of the id, so there is nothing to retry.
    """
    expired = result is None
    if result is None:
        result = JobResult(job_id=job_id, state=JobState.FAILED, error=EXPIRED)
        retryable = False

    finished = result.state == JobState.FINISHED
    polls = not result.state.is_terminal
    label, chip = CHIPS.get(
        result.state, CHIPS[JobState.QUEUED] if polls else CHIPS[JobState.FAILED])

    # A submission that came with a picture we already serve (a URL, a
    # sample tile) shows that; anything else waits for the worker to write a
    # thumbnail. Never the picture itself: inlined twice over, a 7.9 MB upload
    # made a 42 MB poll response on a host whose two cores the worker wants.
    pictures = result.images
    submitted = source.thumb or (pictures.thumb if pictures else None)

    body: list = []
    if result.state == JobState.STARTED:
        body.append(Progress(value=result.progress, max=100))
    elif polls:
        body.append(Progress(max=100))
    elif finished and pictures:
        body.append(_result_view(job_id, pictures, result.downloads or {},
                                 result.share_url, result.expires_at))
    else:
        message = result.error or 'That did not work.'
        retry = [Button('Retry', type='button', cls='retry',
                        hx_post=f'/jobs/{job_id}/retry',
                        hx_target='closest article', hx_swap='outerHTML')] if retryable else []
        body.append(Div(P(message), *retry, cls='card-error'))

    return Article(
        Div(
            Div(
                Img(src=submitted, alt='', cls='thumb') if submitted else Div(cls='thumb'),
                Div(B(source.label or ('Unknown job' if expired else 'Picture')),
                    Span('' if expired else _subtitle(result, source)), cls='card-id'),
                Span(label, cls=chip),
                Button('×', type='button', cls='remove', title='Remove', aria_label='Remove',
                       hx_get='/empty', hx_target='closest article', hx_swap='delete'),
                cls='card-head',
            ),
            *body,
            cls='card-body',
        ),
        # Swiping left on a phone snaps this into view; the card is a
        # scroll-snap strip of the two. `display: none` above 600px, where
        # the × in the head is the only way out.
        Div(
            Button('Remove', type='button', cls='swipe-remove',
                   hx_get='/empty', hx_target='closest article', hx_swap='delete'),
            cls='card-remove',
        ),
        id=dom_id or f'job-{job_id}',
        cls='card',
        hx_get=f'/jobs/{job_id}' if polls else None,
        hx_trigger=f'load delay:{POLL_INTERVAL}' if polls else None,
        hx_swap='outerHTML' if polls else None,
    )


def card_for(job_id: str) -> Article:
    """The card a job's current state calls for."""
    described = jobs.describe(job_id)
    if described is None:
        return card(job_id, None, JobSource())
    return card(job_id, *described)


def rejected_card(label: str, kind: SourceKind, message: str) -> Article:
    """A submission that never became a job, so there is nothing to retry."""
    job_id = f'rejected-{abs(hash((label, message))) % 10**8:08d}'
    return card(
        job_id,
        JobResult(job_id=job_id, state=JobState.FAILED, error=message),
        JobSource(kind=kind, label=label),
        retryable=False,
        dom_id=job_id,
    )


# ------------------------------------------------------------ the docs page


def snippet(text: str) -> Div:
    """A code block with a Copy button. The hx-on:click is the only script
    this project ships."""
    return Div(
        Pre(Code(text)),
        Button('Copy', type='button', cls='copy',
               **{'hx-on:click':
                  'navigator.clipboard.writeText(this.previousElementSibling.innerText)'}),
        Span('Copied', cls='copied'),
        cls='snippet',
    )


def figure(src: str, alt: str, caption: str) -> Figure:
    """A docs illustration, shown at about 372 CSS px. The WebP beside the
    original is what gets sent; the original stays because it is the file the
    prose is about, and `deal_with_me.png` is also the og:image, which
    scrapers want as a PNG."""
    derived = f'/static/img/figures/{Path(src).stem}.webp'
    return Figure(Img(src=asset(derived), alt=alt, loading='lazy'), Figcaption(caption))


def tile(name: str, description: str) -> Div:
    return Div(B(name), Span(description), cls='tile')


def stage(percent: str, label: str, last: bool = False) -> Div:
    return Div(B(percent), Span(label), cls='stage last' if last else 'stage')


def docs_page(theme: str | None, reachable: bool) -> tuple:
    return (
        *head_tags('How it works | Deal With It', theme),
        site_header('docs', theme, reachable),
        Main(
            # <details> rather than a nav: on a phone the contents fold into
            # a card that opens on tap, with no script. Above 860px the CSS
            # forces the content open and the summary reads as the label it
            # used to be.
            Details(
                Summary(Span('On this page', cls='label')),
                A('How it works', href='#how'),
                A('API', href='#api'),
                A('Submit an image', href='#submit', cls='sub'),
                A('Collect the result', href='#collect', cls='sub'),
                A('Health', href='#health', cls='sub'),
                A('License', href='#license'),
                A('Try it', href='/', cls='try-it'),
                cls='toc',
            ),
            Article(
                Section(
                    H1('How it works'),
                    P('Give it a picture, get it back with the ',
                      A('meme sunglasses', href=MEME),
                      ' on every face in it.', cls='lead'),
                    Div(
                        figure('/static/img/me.jpg', 'Original picture', 'Turns this'),
                        figure('/static/img/deal_with_me.png', 'Processed picture',
                               'Into this'),
                        # Two portraits still fit side by side on a phone;
                        # the group photos below would be a stripe.
                        cls='figures portraits',
                    ),
                    Div(
                        figure('/static/img/multiple_people.jpg',
                               'Original picture with several people', 'Several people, before'),
                        figure('/static/img/deal_with_multiple_people.png',
                               'Processed picture with several people', 'After'),
                        cls='figures',
                    ),
                    P('Group photo: the 2013 class of NASA astronauts, by Robert '
                      'Markowitz for ',
                      A('NASA', href='https://commons.wikimedia.org/wiki/'
                                     'File:2013_class_of_NASA_astronauts.jpg'),
                      '. Public domain.', cls='credit'),
                    P('Faces are found with ',
                      A('YuNet', href='https://github.com/opencv/opencv_zoo/tree/main/'
                                      'models/face_detection_yunet'),
                      ', a small ONNX detector run through OpenCV. If it finds nothing '
                      'it looks again at the picture doubled in size, then with its '
                      'contrast equalised. Each face then goes to ',
                      A('dlib', href='http://dlib.net'),
                      "'s 68-point shape predictor for the landmarks the glasses are "
                      'placed from.'),
                    P('The line between the outer corners of the eyes gives the tilt '
                      'of the head and the size of the frame; how far the nose sits '
                      'off that line gives the turn, so the glasses narrow and taper '
                      'on a face seen from the side. Every point found comes back in '
                      'the result, as ', Code('faces'), '. It also finds dogs now and '
                      'then; cats, never.'),
                    P('Compositing is done with ',
                      A('Pillow', href='https://github.com/python-pillow/Pillow'),
                      ' in a background worker managed by ',
                      A('RQ', href='https://python-rq.org'),
                      ': detecting faces takes long enough that doing it inside the '
                      'request would block everything else. The page is ',
                      A('FastHTML', href='https://fastht.ml'), ' + htmx, the REST API is ',
                      A('FastAPI', href='https://fastapi.tiangolo.com'), '.'),
                    Div(
                        tile('web', 'Serves the page and the JSON API. Never decodes an image.'),
                        tile('worker', 'Pulls jobs off the queue, fetches and decodes the '
                                       'image, draws the glasses.'),
                        tile('redis', 'The queue, and where results wait to be collected.'),
                        cls='tiles',
                    ),
                    id='how',
                ),
                Section(
                    H2('API'),
                    P('Documented at ', A('/api/docs', href='/api/docs'), '. Processing '
                      'happens in the background, so it takes two calls: one to submit '
                      'the image, one to collect the result.'),
                    H3(Span('1', cls='n'), 'Submit an image', id='submit'),
                    P(Code('POST /api/jobs'), ' with either a ', Code('url'), ' or a ',
                      Code('base64'), ' data URI, not both:'),
                    Div(
                        snippet('{\n  "url": "' + _example_url() + '"\n}'),
                        snippet('{\n  "base64": "data:image/png;base64,..."\n}'),
                        cls='pair',
                    ),
                    P('Responds ', Code('202'), ' with the job to poll:'),
                    snippet('{\n  "job_id": "9f2c...",\n  "state": "queued",\n'
                            '  "status_url": "http://localhost:5000/api/jobs/9f2c..."\n}'),
                    H3(Span('2', cls='n'), 'Collect the result', id='collect'),
                    P(Code('GET'), ' the ', Code('status_url'), ' until ', Code('state'),
                      ' is ', Code('finished'), ' or ', Code('failed'), ':'),
                    snippet('{\n  "job_id": "9f2c...",\n  "state": "finished",\n'
                            '  "images": {\n'
                            '    "view":   ".../i/9f2c.../view.webp",\n'
                            '    "thumb":  ".../i/9f2c.../thumb.webp",\n'
                            '    "before": ".../i/9f2c.../before.webp",\n'
                            '    "full":   ".../i/9f2c.../result.jpg"\n  },\n'
                            '  "downloads": {"jpg": "...", "webp": "..."},\n'
                            '  "share_url": ".../s/9f2c...",\n'
                            '  "expires_at": "2026-09-04T17:32:00Z",\n'
                            '  "error": null,\n'
                            '  "progress": 100,\n  "step": "Done",\n'
                            '  "detection": "plain",\n'
                            '  "faces": [{"box": [101, 420, 402, 197], "score": 0.95,\n'
                            '             "points": {"left_eye": [249.7, 225.3], ...},\n'
                            '             "landmarks": [[199.0, 208.0], ...]}]\n}'),
                    P(Code('images'), ' are links, not bytes. ', Code('view'),
                      ' is what to show someone -- WebP, 1600px, a fortieth of the '
                      'full-resolution file -- and ', Code('full'), ' keeps the format '
                      'the picture was submitted in. ', Code('downloads'),
                      ' offers the same picture in whichever formats were written for '
                      'it. Everything under ', Code('/i/'), ' stops existing at ',
                      Code('expires_at'), ', and so does ', Code('share_url'), '.'),
                    P(Code('state'), ' is one of ', Code('queued'), ', ', Code('started'),
                      ', ', Code('finished'), ' or ', Code('failed'), '. A failed job '
                      'carries a readable ', Code('error'), ': an unreachable URL, an '
                      'undecodable image, or no faces found. An unknown or expired job id '
                      'gives a ', Code('404'), '. The result is a JPEG when the input was '
                      'a JPEG and a PNG otherwise.'),
                    P('Before version 2, the result came back inline as ', Code('image'),
                      ': a base64 data URI, megabytes of it, in every poll. That field '
                      'is gone.'),
                    P('Submissions are limited per client and refused with ', Code('429'),
                      ' (too many in a minute) or ', Code('503'), ' (the queue is full); '
                      'both carry a ', Code('Retry-After'), ' header.'),
                    P('A ', Code('queued'), ' job also carries ', Code('ahead'), ': how '
                      'many jobs are in front of it, with ', Code('0'), ' meaning it is '
                      'next. It goes null once a worker picks the job up, which is when ',
                      Code('progress'), ' and ', Code('step'), ' take over.'),
                    P('While a job runs, ', Code('progress'), ' and ', Code('step'),
                      ' report the stage it has reached. They are checkpoints rather than '
                      'measurements: neither the detector nor Pillow reports how far '
                      'through it is.'),
                    Div(
                        stage('10', 'Fetching the image'),
                        stage('35', 'Looking for faces'),
                        stage('75', 'Drawing glasses on 8 faces'),
                        stage('90', 'Encoding the result'),
                        stage('100', 'Done', last=True),
                        cls='stages',
                    ),
                    H3('Health', id='health'),
                    P(Code('GET /api/health'), ' reports whether the broker is reachable. '
                      'The status pill in the header reads it every 30 seconds.'),
                    snippet('{ "status": "ok", "redis": true }'),
                    id='api',
                ),
                Section(
                    H2('License'),
                    P(A('MIT License', href=LICENSE_URL), '. Created by ',
                      A('Eric Magalhães', href='https://ericovis.com'), '.'),
                    id='license',
                ),
                cls='doc',
            ),
            cls='container docs',
        ),
        site_footer('docs'),
    )


# ---------------------------------------------------------------- handlers


async def _store_upload(job_id: str, upload: UploadFile) -> str:
    """Put an upload in the job's directory, or say why it cannot be used.

    Bytes, not an image: nothing here opens the file, so the web tier still
    never runs an attacker-supplied decoder. It is strictly less work than
    the base64 encode this replaced, and the picture no longer travels
    through Redis at all.
    """
    settings = get_settings()
    content_type = upload.content_type or ''
    if not content_type.startswith('image/'):
        raise ValueError('That file does not look like an image.')
    payload = await upload.read()
    if len(payload) > settings.max_image_bytes:
        raise ValueError(
            f'That image is bigger than the {settings.max_image_bytes // 1024 // 1024} MB limit.'
        )
    if not payload:
        raise ValueError('That file is empty.')
    # The extension comes from an allowlist, never from the client's own
    # string: these files are served straight off disk.
    name = f'source.{blobs.extension_for(content_type)}'
    return await run_in_threadpool(blobs.put, job_id, name, payload)


def _enqueue(payload: dict, kind: SourceKind, label: str,
             thumb: str | None = None, client: str | None = None,
             job_id: str | None = None) -> Article:
    """Validate, queue, and hand back the card for whatever state it is in."""
    if payload.get('url') is not None:
        try:
            request = ImageRequest.model_validate(
                {'url': payload['url'], 'base64': None})
        except ValidationError as exc:
            return rejected_card(label, kind, _first_message(exc))
        payload = {'url': str(request.url), 'blob': None}
    elif not payload.get('blob'):
        return rejected_card(label, kind, NOTHING_SUBMITTED)
    try:
        throttle.admit(client)
    except throttle.Throttled as exc:
        return rejected_card(label, kind, str(exc))
    job_id, _ = jobs.enqueue(
        payload, job_id=job_id,
        meta={'kind': str(kind), 'label': label, 'thumb': thumb},
    )
    return card_for(job_id)


def _client_of(request) -> str | None:
    return request.client.host if request.client else None


def create_ui():
    """Build the FastHTML app, minus its static catch-all (src/app.py serves
    /static)."""
    settings = get_settings()
    app, rt = fast_app(
        pico=False,
        surreal=False,
        default_hdrs=True,
        secret_key=settings.secret_key,
        exts=None,
    )
    app.router.routes = [
        route for route in app.router.routes if 'fname:path' not in getattr(route, 'path', '')
    ]

    def theme_of(request) -> str | None:
        chosen = request.cookies.get(THEME_COOKIE)
        return chosen if chosen in ('light', 'dark') else None

    @rt('/')
    def get(request):
        return page(theme_of(request), jobs.is_broker_reachable())

    @app.get('/docs')
    def documentation(request):
        return docs_page(theme_of(request), jobs.is_broker_reachable())

    @app.post('/submit')
    async def submit(request, url: str = '', image: list[UploadFile] | None = None,
                     sample: str = ''):
        # The Redis work runs in the threadpool: enqueueing a 10 MB upload
        # costs RQ half a second of zlib, which must not stall the loop.
        client = _client_of(request)
        if sample:
            return await run_in_threadpool(_submit_sample, sample, client)

        # An untouched file input still posts one empty part. The dropzone
        # sends one file per request (see UPLOAD_ONE_BY_ONE), so this is
        # normally a list of one -- but it stays a list, because the form's
        # own submit button would post whatever the input still holds.
        files = [f for f in (image or []) if isinstance(f, UploadFile) and f.filename]
        if files:
            # No replacement form: the script empties the input itself, and
            # swapping the form mid-batch would tear out the element the
            # remaining requests are posting from.
            return tuple([await _submit_file(f, client) for f in files])

        return await run_in_threadpool(_submit_url, url, client)

    def _submit_sample(name: str, client: str | None):
        if name not in SAMPLES:
            return rejected_card(name, SourceKind.SAMPLE, NOTHING_SUBMITTED)
        sample = SAMPLES[name]
        job_id = jobs.new_id()
        reference = blobs.put(job_id, f'source.{Path(sample.filename).suffix[1:]}',
                              sample_source(name))
        # Samples do not reset the form: the buttons sit outside it.
        return _enqueue(
            {'url': None, 'blob': reference},
            SourceKind.SAMPLE, sample.filename, tile_url(sample), client, job_id,
        )

    async def _submit_file(upload: UploadFile, client: str | None):
        label = upload.filename or 'Uploaded image'
        job_id = jobs.new_id()
        try:
            reference = await _store_upload(job_id, upload)
        except ValueError as exc:
            return rejected_card(label, SourceKind.UPLOAD, str(exc))
        except OSError:
            logger.exception('could not store an upload for job %s', job_id)
            return rejected_card(label, SourceKind.UPLOAD, 'Could not store that image.')
        return await run_in_threadpool(
            _enqueue, {'url': None, 'blob': reference},
            SourceKind.UPLOAD, label, None, client, job_id)

    def _submit_url(url: str, client: str | None):
        """A bad URL answers in the hint, not with a card: no job was created,
        and leaving the text in the field lets it be corrected."""
        text = url.strip()
        if not text:
            return url_hint(NOTHING_SUBMITTED, oob=True)
        if message := images.shape_error(text):
            return url_hint(message, oob=True)
        try:
            request = ImageRequest.model_validate({'url': text, 'base64': None})
        except ValidationError as exc:
            return url_hint(_first_message(exc), oob=True)
        try:
            throttle.admit(client)
        except throttle.Throttled as exc:
            return url_hint(str(exc), oob=True)

        payload = request.as_payload()
        job_id, _ = jobs.enqueue(payload, meta={
            'kind': str(SourceKind.URL), 'label': jobs.label_for(payload), 'thumb': payload['url'],
        })
        return card_for(job_id), upload_form(replace=True)

    @app.post('/validate')
    def validate(url: str = ''):
        """Shape only: a handler must not block on DNS. The real check runs
        in the worker."""
        text = url.strip()
        if not text:
            return url_hint()
        if message := images.shape_error(text):
            return url_hint(message)
        return url_hint(URL_LOOKS_FINE, ok=True)

    @app.get('/jobs/{job_id}')
    def job_fragment(job_id: str):
        return card_for(job_id)

    @app.post('/jobs/{job_id}/retry')
    def retry_job(request, job_id: str):
        try:
            throttle.admit(_client_of(request))
        except throttle.Throttled as exc:
            described = jobs.describe(job_id)
            if described is None:
                return card(job_id, None, JobSource())
            refused = JobResult(job_id=job_id, state=JobState.FAILED, error=str(exc))
            return card(job_id, refused, described[1])
        resubmitted = jobs.resubmit(job_id)
        if resubmitted is None:
            return card(job_id, None, JobSource())
        return card_for(resubmitted[0])

    @app.get('/health')
    def health():
        return status_pill(jobs.is_broker_reachable())

    @app.post('/theme')
    def set_theme(theme: str = ''):
        chosen = 'dark' if theme == 'dark' else 'light'
        return theme_state(chosen), cookie(
            THEME_COOKIE, chosen, max_age=THEME_MAX_AGE, path='/', samesite='lax',
        )

    async def _not_found(request, exc):
        """Only the interface answers with a page.

        `/api/*` is a published JSON contract and keeps its `{"detail": ...}`
        even for a path no route claims; `/static/*` and `/i/*` never reach
        here, and an HTML body would be a nonsense response to an <img>.
        """
        if request.url.path.startswith('/api/'):
            return JSONResponse({'detail': 'Not Found'}, status_code=404)
        return not_found_response(theme_of(request))

    app.add_exception_handler(404, _not_found)

    @app.get('/s/{job_id}')
    def shared(request, job_id: str):
        """A finished result, by link. Read off disk, so it lives exactly as
        long as the pictures it shows."""
        record = jobs.shared_record(job_id)
        if record is None:
            return not_found_response(theme_of(request), heading=EXPIRED_HEADING,
                                      message=EXPIRED_MESSAGE)
        return share_page(job_id, record, theme_of(request), jobs.is_broker_reachable())

    @app.get('/llms.txt')
    def llms(request):
        """What an agent reads before trying to use this. Plain text, because
        that is what llmstxt.org asks for and what a fetch tool expects."""
        return Response(llms_txt(), media_type='text/plain; charset=utf-8')

    @app.get('/empty')
    def empty():
        """Empty on purpose: Remove and Clear swap this in."""
        return Response('', media_type='text/html')

    return app
