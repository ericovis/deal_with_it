"""The interface, in FastHTML.

Two pages: the app at ``/`` and the docs at ``/docs``. Submitting a picture
enqueues a job and swaps a card into ``#queue``; the card polls itself until
the worker is done and then turns into the result in place. No page reloads,
and no JavaScript of our own beyond the one clipboard line on the docs page.
"""

import base64
import json
import logging
from dataclasses import dataclass
from functools import cache
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
    Div,
    Em,
    Figcaption,
    Figure,
    Footer,
    Form,
    Header,
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
    Section,
    Span,
    Title,
    UploadFile,
    cookie,
    fast_app,
)
from pydantic import ValidationError
from starlette.responses import Response

from src import images, jobs
from src.config import get_settings
from src.models import ImageRequest, JobResult, JobSource, JobState, SourceKind

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
    """One of the three one-click pictures, all of them repo assets."""

    filename: str
    title: str
    media_type: str


#: Ordered easiest to hardest, ending with the one that is meant to fail.
#: Counts are what the detector actually finds -- see tests/test_processor.py.
SAMPLES = {
    'me': Sample('me.jpg', 'One face', 'image/jpeg'),
    'apollo': Sample('apollo_11_crew.jpg', 'Three faces', 'image/jpeg'),
    'group': Sample('multiple_people.jpg', 'Eight faces', 'image/jpeg'),
    'solvay': Sample('solvay_1927.jpg', 'Twenty-nine faces', 'image/jpeg'),
    'earth': Sample('blue_marble.jpg', 'No faces · fails', 'image/jpeg'),
}


@cache
def sample_data_uri(name: str) -> str:
    """A sample picture as a data URI, read once per process.

    Samples go through the queue as base64 rather than as a URL to
    ``/static``: the worker's SSRF guard refuses to fetch anything that
    resolves to a private address, and our own host is one. Safe to cache --
    a fixed handful of files, fixed at build time, and the strings are
    immutable.
    """
    sample = SAMPLES[name]
    payload = base64.b64encode((IMG_DIR / sample.filename).read_bytes()).decode()
    return f'data:{sample.media_type};base64,{payload}'


def _first_message(exc: ValidationError) -> str:
    """The one sentence out of a pydantic error worth showing a person."""
    message = exc.errors()[0]['msg'].removeprefix('Value error, ')
    return message.split(', relative URL without a base')[0]


def _absolute(path: str) -> str:
    base = get_settings().public_url.rstrip('/')
    return f'{base}{path}' if base else path


# --------------------------------------------------------------- the shell


def head_tags(title: str) -> tuple:
    """Metadata that belongs in <head>. FastHTML hoists these out of a page."""
    card = _absolute('/static/img/deal_with_me.png')
    return (
        Title(title),
        Meta(name='viewport', content='width=device-width,initial-scale=1'),
        Meta(name='author', content='Eric Magalhães'),
        Meta(name='description', content='A Python API for creating "Deal With It"-like Images'),
        Meta(name='twitter:card', content='summary_large_image'),
        Meta(name='twitter:creator', content='@ericovis'),
        Meta(name='twitter:title', content='Deal with it!'),
        Meta(name='twitter:description',
             content='A Python API for creating "Deal With It"-like Images'),
        Meta(name='twitter:image', content=card),
        Meta(property='og:title', content='Deal with it!'),
        Meta(property='og:type', content='website'),
        Meta(property='og:image', content=card),
        Meta(property='og:description',
             content='A Python API for creating "Deal With It"-like Images.'),
        Link(rel='icon', href='/static/img/favicon.png', type='image/png', sizes='16x16'),
        Link(rel='preconnect', href='https://fonts.googleapis.com'),
        Link(rel='stylesheet', href='https://fonts.googleapis.com/css2?'
                                    'family=Manrope:wght@400;500;600;700;800&'
                                    'family=Fira+Code:wght@400;500&display=swap'),
        Link(rel='stylesheet', href='/static/css/style.css'),
    )


def theme_state(theme: str | None) -> Span:
    """The single element the whole palette hangs off.

    ``style.css`` reads it with ``body:has(#theme-state[data-theme="dark"])``.
    An absent attribute means nobody has chosen yet, which is the only case
    where ``prefers-color-scheme`` gets a say. Flipping the switch swaps this
    one span, so the page changes colour without a reload and without a line
    of script -- and the same response sets the cookie, so the next load
    already agrees with what is on screen.
    """
    return Span(id='theme-state', hidden=True, data_theme=theme)


def theme_switch(theme: str | None) -> Span:
    """A checkbox drawn as a switch. The visuals come from the theme tokens
    rather than from ``:checked``, so the knob shows the theme actually in
    force -- including a first visit on a dark desktop, where the cookie does
    not exist and the box is therefore unchecked."""
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
            A(Img(src='/static/img/glasses.svg', alt=''), Span('Deal With It!'),
              href='/', cls='brand'),
            Nav(
                nav_link('App', '/', 'app'),
                nav_link('Docs', '/docs', 'docs'),
                nav_link('GitHub ↗', REPO),
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


def upload_form(replace: bool = False) -> Form:
    """The submit form.

    With ``replace``, it comes back marked for an out-of-band swap: htmx
    replaces the form already on the page with this fresh, empty one. That is
    how a submitted job clears the URL, the file input and the hint without
    any JavaScript of ours.

    The file input sits *before* its label on purpose: the label is the
    dropzone, and a file input is a native drop target, so dropping onto the
    label counts as choosing files. Browsers also fire :hover throughout a
    drag, which is the entire drag-over state.
    """
    return Form(
        Input(type='file', id='files', name='image', accept='image/*', multiple=True,
              cls='visually-hidden',
              hx_post='/submit', hx_trigger='change', hx_encoding='multipart/form-data',
              hx_target='#queue', hx_swap='afterbegin'),
        Label(
            Img(src='/static/img/glasses.svg', alt=''),
            Span('Drop pictures here', cls='title'),
            Span('or ', Em('browse your computer'), cls='sub'),
            Span('PNG or JPEG, up to 10 MB each. Several at once is fine.', cls='limits'),
            fr='files', cls='dropzone',
        ),
        Div('or a URL', cls='divider'),
        Div(
            Input(type='text', id='url', name='url',
                  placeholder='https://example.com/photo.jpg',
                  autocomplete='off', spellcheck='false',
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
        # Without this the URL field inherits hx-disabled-elt and htmx logs
        # `The selector "find button" ... returned no matches!` on every
        # keystroke, because an <input> has no descendants to find.
        hx_disinherit='hx-disabled-elt',
    )


def sample_tile(name: str) -> Button:
    sample = SAMPLES[name]
    return Button(
        Img(src=f'/static/img/{sample.filename}', alt=''),
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
            P('Or try a sample', cls='samples-label'),
            Div(*(sample_tile(name) for name in SAMPLES), cls='samples'),
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
          'anything you want to keep.', cls='note'),
        Div(id='queue'),
        # After #queue rather than before it, because the rule that reveals
        # this is a sibling combinator -- and an empty #queue is zero tall, so
        # it still reads as sitting where the list will be.
        Div('Nothing here yet. Drop a picture, paste a URL, or try a sample.', cls='empty'),
        cls='session',
    )


def page(theme: str | None, reachable: bool) -> tuple:
    return (
        *head_tags('Deal With It!'),
        site_header('app', theme, reachable),
        Main(app_pane(), session_section(), cls='container layout'),
        site_footer('app'),
    )


# ------------------------------------------------------------------- cards


def _faces(count: int | None) -> str:
    if count is None:
        return ''
    return f' · {count} face' if count == 1 else f' · {count} faces'


def _subtitle(result: JobResult, source: JobSource) -> str:
    kind = KIND_LABELS[source.kind]
    if result.state == JobState.STARTED:
        step = result.step or 'Working on it'
        return f'{step} · {result.progress}%' if result.progress is not None else step
    if not result.state.is_terminal:
        # queued, and RQ's deferred/scheduled, which we never create but
        # which would otherwise fall through to the failed branch.
        return 'In the queue'
    if result.state == JobState.FINISHED:
        return f'{kind}{_faces(source.faces)}'
    return kind


def _result_view(job_id: str, image: str, before: str | None) -> Div:
    """The finished image, its Before/After toggle, and the full-screen view.

    ``:has()`` does all the switching, so none of it costs a line of script.

    The full-screen view is the *same* frame and footer re-laid-out as an
    overlay rather than a second copy of them: duplicating the markup would
    mean sending the whole result data URI twice in every card. A checkbox
    drives it because ``<dialog>`` cannot be opened without ``showModal()``.
    """
    toggle = f'full-{job_id}'
    footer = [
        Span(cls='spacer'),
        Input(type='checkbox', id=toggle, cls='full-toggle visually-hidden'),
        Label('View full size', fr=toggle, cls='open-full'),
        A('Download PNG', href=image, download=f'deal-with-it-{job_id[:8]}.png',
          cls='download'),
    ]

    if before is None:
        frame = Div(Img(src=image, alt='Result'), cls='frame')
        controls = footer
    else:
        name = f'view-{job_id}'
        frame = Div(
            Img(src=before, alt='The picture as submitted', cls='before'),
            Img(src=image, alt='Result', cls='after'),
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

    return Div(
        frame,
        # Clicking the space around the image closes it. The frame above is
        # pointer-events:none while open so those clicks land here.
        Label(fr=toggle, cls='backdrop', aria_label='Close the full-size view'),
        Label('×', fr=toggle, cls='lightbox-close', title='Close',
              aria_label='Close the full-size view'),
        Div(*controls, cls='card-foot'),
        cls='result',
    )


def card(job_id: str, result: JobResult | None, source: JobSource,
         retryable: bool = True, dom_id: str | None = None) -> Article:
    """One job, in whatever state it is in.

    Every state is the same element, so a card can replace itself with the
    next answer: queued and started ones carry the hx-get that fetches the
    next one, terminal ones do not, and the poll chain therefore ends by
    itself rather than leaving a timer running against whatever replaced it.

    ``result is None`` means RQ has never heard of the id -- expired, or made
    up. There is nothing to retry in that case.
    """
    expired = result is None
    if result is None:
        result = JobResult(job_id=job_id, state=JobState.FAILED, error=EXPIRED)
        retryable = False

    finished = result.state == JobState.FINISHED
    polls = not result.state.is_terminal
    label, chip = CHIPS.get(
        result.state, CHIPS[JobState.QUEUED] if polls else CHIPS[JobState.FAILED])

    # A data URI is only worth sending once the card has stopped polling: as a
    # thumbnail on a card that refetches every second it would be the whole
    # upload, every second. Anything cheap (a remote URL, a static path) goes
    # on every state.
    thumb = source.thumb or (source.original if finished else None)

    body: list = []
    if result.state == JobState.STARTED:
        body.append(Progress(value=result.progress, max=100))
    elif polls:
        body.append(Progress(max=100))
    elif finished and result.image:
        # Prefer the cheap copy for "before" too: a sample's static path says
        # the same thing as its data URI in 24 bytes instead of 300 KB.
        body.append(_result_view(job_id, result.image, source.thumb or source.original))
    else:
        message = result.error or 'That did not work.'
        retry = [Button('Retry', type='button', cls='retry',
                        hx_post=f'/jobs/{job_id}/retry',
                        hx_target='closest article', hx_swap='outerHTML')] if retryable else []
        body.append(Div(P(message), *retry, cls='card-error'))

    return Article(
        Div(
            Img(src=thumb, alt='', cls='thumb') if thumb else Div(cls='thumb'),
            Div(B(source.label or ('Unknown job' if expired else 'Picture')),
                Span('' if expired else _subtitle(result, source)), cls='card-id'),
            Span(label, cls=chip),
            Button('×', type='button', cls='remove', title='Remove', aria_label='Remove',
                   hx_get='/empty', hx_target='closest article', hx_swap='delete'),
            cls='card-head',
        ),
        *body,
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
    """A submission that never became a job.

    It still gets a card rather than a bare sentence: a person who dropped
    six files wants to see which one was refused, next to the five that were
    not. There is no job behind it, so there is nothing to retry either.
    """
    # Not id="job-..." -- there is no job, and nothing should be able to poll
    # or retry it by mistaking the element for one.
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
    """A code block with a Copy button.

    The hx-on:click is the only script this project ships, and the ``pre``
    keeps ``user-select: all`` so one click still selects the whole thing for
    anyone whose browser refuses clipboard access.
    """
    return Div(
        Pre(Code(text)),
        Button('Copy', type='button', cls='copy',
               **{'hx-on:click':
                  'navigator.clipboard.writeText(this.previousElementSibling.innerText)'}),
        Span('Copied', cls='copied'),
        cls='snippet',
    )


def figure(src: str, alt: str, caption: str) -> Figure:
    return Figure(Img(src=src, alt=alt), Figcaption(caption))


def tile(name: str, description: str) -> Div:
    return Div(B(name), Span(description), cls='tile')


def stage(percent: str, label: str, last: bool = False) -> Div:
    return Div(B(percent), Span(label), cls='stage last' if last else 'stage')


def docs_page(theme: str | None, reachable: bool) -> tuple:
    return (
        *head_tags('How it works | Deal With It'),
        site_header('docs', theme, reachable),
        Main(
            Nav(
                Span('On this page', cls='label'),
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
                        cls='figures',
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
                    P('Faces are found with the ',
                      A('face_recognition', href='https://github.com/ageitgey/face_recognition'),
                      ' library. For every face it returns landmarks, including the '
                      'coordinates of both eyes.'),
                    P('The angle between the eyes tells how far the head is tilted, so '
                      'the glasses are rotated to sit straight on the face.'),
                    P('Compositing is done with ',
                      A('Pillow', href='https://github.com/python-pillow/Pillow'),
                      ' in a background worker managed by ',
                      A('RQ', href='https://python-rq.org'),
                      ': detecting faces takes long enough that doing it inside the '
                      'request would block everything else. The page is ',
                      A('FastHTML', href='https://fastht.ml'), ' + htmx, the REST API is ',
                      A('FastAPI', href='https://fastapi.tiangolo.com'), '.'),
                    Div(
                        tile('web', 'Serves the page and the JSON API. Never touches an image.'),
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
                        snippet('{\n  "url": "https://example.com/photo.jpg"\n}'),
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
                            '  "image": "data:image/png;base64,...",\n  "error": null,\n'
                            '  "progress": 100,\n  "step": "Done"\n}'),
                    P(Code('state'), ' is one of ', Code('queued'), ', ', Code('started'),
                      ', ', Code('finished'), ' or ', Code('failed'), '. A failed job '
                      'carries a readable ', Code('error'), ': an unreachable URL, an '
                      'undecodable image, or no faces found. An unknown or expired job id '
                      'gives a ', Code('404'), '.'),
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


async def _upload_payload(upload: UploadFile) -> str:
    """Read an upload into a data URI, or say why it cannot be used."""
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
    return f'data:{content_type};base64,{base64.b64encode(payload).decode()}'


def _enqueue(payload: dict, kind: SourceKind, label: str,
             thumb: str | None = None) -> Article:
    """Validate, queue, and hand back the card for whatever state it is in."""
    try:
        request = ImageRequest.model_validate(payload)
    except ValidationError as exc:
        return rejected_card(label, kind, _first_message(exc))
    job_id, _ = jobs.enqueue(
        request.as_payload(),
        meta={'kind': str(kind), 'label': label, 'thumb': thumb},
    )
    return card_for(job_id)


def create_ui():
    """Build the FastHTML app.

    Static assets are served by the outer FastAPI app instead of FastHTML's
    catch-all route, which is a bare FileResponse over the process CWD.
    """
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
    async def submit(url: str = '', image: list[UploadFile] | None = None, sample: str = ''):
        if sample:
            return _submit_sample(sample)

        # An untouched file input still posts one empty part, so the filename
        # is what separates "chose nothing" from "chose something".
        files = [f for f in (image or []) if isinstance(f, UploadFile) and f.filename]
        if files:
            cards = [await _submit_file(f) for f in files]
            return (*cards, upload_form(replace=True))

        return await _submit_url(url)

    def _submit_sample(name: str):
        if name not in SAMPLES:
            return rejected_card(name, SourceKind.SAMPLE, NOTHING_SUBMITTED)
        sample = SAMPLES[name]
        # Samples do not reset the form: the buttons sit outside it, and
        # wiping a half-typed URL because someone clicked a sample is rude.
        return _enqueue(
            {'url': None, 'base64': sample_data_uri(name)},
            SourceKind.SAMPLE, sample.filename, f'/static/img/{sample.filename}',
        )

    async def _submit_file(upload: UploadFile):
        label = upload.filename or 'Uploaded image'
        try:
            payload = await _upload_payload(upload)
        except ValueError as exc:
            return rejected_card(label, SourceKind.UPLOAD, str(exc))
        return _enqueue({'url': None, 'base64': payload}, SourceKind.UPLOAD, label)

    async def _submit_url(url: str):
        """A bad URL answers in the hint, not with a card.

        There is nothing to show a card *about* -- no job was created and no
        picture was named -- and leaving the text in the field is what lets it
        be corrected rather than retyped.
        """
        text = url.strip()
        if not text:
            return url_hint(NOTHING_SUBMITTED, oob=True)
        if message := images.shape_error(text):
            return url_hint(message, oob=True)
        try:
            request = ImageRequest.model_validate({'url': text, 'base64': None})
        except ValidationError as exc:
            return url_hint(_first_message(exc), oob=True)

        job_id, _ = jobs.enqueue(
            request.as_payload(),
            meta={'kind': str(SourceKind.URL), 'label': jobs.label_for(request.as_payload())},
        )
        return card_for(job_id), upload_form(replace=True)

    @app.post('/validate')
    def validate(url: str = ''):
        """Shape only: a handler that resolved a hostname would block the
        event loop on whatever DNS the caller chose. The real check runs in
        the worker, against the address the name actually resolves to."""
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
    def retry_job(job_id: str):
        resubmitted = jobs.resubmit(job_id)
        if resubmitted is None:
            return card(job_id, None, JobSource())
        return card_for(resubmitted[0])

    @app.get('/health')
    def health():
        return status_pill(jobs.is_broker_reachable())

    @app.post('/theme')
    def set_theme(theme: str = ''):
        """The switch posts here; the response is the one span the palette
        hangs off, plus the cookie that makes the next load agree."""
        chosen = 'dark' if theme == 'dark' else 'light'
        return theme_state(chosen), cookie(
            THEME_COOKIE, chosen, max_age=THEME_MAX_AGE, path='/', samesite='lax',
        )

    @app.get('/empty')
    def empty():
        """Nothing, on purpose: Remove and Clear are swaps that need a
        response body to discard."""
        return Response('', media_type='text/html')

    return app
