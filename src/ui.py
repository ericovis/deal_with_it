"""The interface, in FastHTML.

Submitting the form enqueues a job and swaps in a fragment that polls itself
until the worker is done -- no page reloads, and no JavaScript of our own.
"""

import base64
import logging

from fasthtml.common import (
    H1,
    H2,
    H4,
    A,
    Button,
    Code,
    Div,
    Fieldset,
    Footer,
    Form,
    Header,
    Img,
    Input,
    Label,
    Li,
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
    Ul,
    UploadFile,
    fast_app,
)
from pydantic import ValidationError

from src import jobs
from src.config import get_settings
from src.models import ImageRequest, JobState

logger = logging.getLogger(__name__)

POLL_INTERVAL = '1s'


def _first_message(exc: ValidationError) -> str:
    """The one sentence out of a pydantic error worth showing a person."""
    message = exc.errors()[0]['msg'].removeprefix('Value error, ')
    return message.split(', relative URL without a base')[0]


def _absolute(path: str) -> str:
    base = get_settings().public_url.rstrip('/')
    return f'{base}{path}' if base else path


def head_tags() -> tuple:
    """Metadata that belongs in <head>. FastHTML hoists these out of a page."""
    card = _absolute('/static/img/deal_with_me.png')
    return (
        Title('Deal With It | Eric Magalhães'),
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
        Link(rel='stylesheet', href='/static/css/style.css'),
        Link(rel='stylesheet',
             href='https://fonts.googleapis.com/css2?family=Open+Sans:wght@400;700&display=swap'),
    )


def upload_form(replace: bool = False) -> Form:
    """The submit form.

    With ``replace``, it comes back marked for an out-of-band swap: htmx
    replaces the form already on the page with this fresh, empty one. That is
    how a finished job clears the URL and the file input without any
    JavaScript of ours.
    """
    return Form(
        Fieldset(
            Label('URL', _for='url'),
            Input(type='text', id='url', name='url',
                  placeholder='https://example.com/image.png'),
            Label('or a file from your computer', _for='image', cls='file-label'),
            Input(type='file', id='image', name='image', accept='image/*'),
        ),
        Div(Button('Deal with It!', type='submit'), cls='submit'),
        id='form',
        hx_swap_oob='true' if replace else None,
        hx_post='/submit',
        hx_target='#result',
        hx_swap='innerHTML',
        hx_encoding='multipart/form-data',
        hx_disabled_elt='find button',
    )


def working(job_id: str, step: str, percent: int | None = None) -> Div:
    """A fragment that replaces itself with the next poll's answer.

    ``load delay:`` rather than ``every``: the chain ends by itself as soon as
    a response comes back without the hx-get, so a finished job cannot leave a
    timer running against whatever replaced it.

    A queued job renders a <progress> with no value, which browsers animate
    as indeterminate -- honest, since nothing is happening yet.
    """
    label = f'{step}... {percent}%' if percent is not None else f'{step}...'
    return Div(
        Progress(value=percent, max=100),
        P(label, id='message'),
        id=f'job-{job_id}',
        cls='working',
        hx_get=f'/jobs/{job_id}',
        hx_trigger=f'load delay:{POLL_INTERVAL}',
        hx_swap='outerHTML',
    )


def failure(message: str, job_id: str | None = None) -> Div:
    return Div(
        P(message, id='message', cls='error'),
        id=f'job-{job_id}' if job_id else 'result-error',
    )


def gallery_item(job_id: str, image: str) -> Div:
    """One finished image, inserted at the top of the gallery.

    hx-swap-oob with a position and selector adds to #gallery rather than
    replacing anything, which is what accumulates the session's results.

    The outer div is a transport envelope and never reaches the page: for any
    swap style other than outerHTML, htmx inserts the *children* of the
    element carrying hx-swap-oob, not the element itself. Without the extra
    level the .gallery-item wrapper would be stripped, taking its styling --
    and the :has() rule that reveals the section -- with it.
    """
    return Div(
        Div(
            A(Img(src=image, alt='Deal with me'), href=image, target='_blank',
              title='Open full size'),
            P(A('Download', href=image, download=f'deal-with-it-{job_id[:8]}.png')),
            cls='gallery-item',
            id=f'result-{job_id}',
        ),
        hx_swap_oob='afterbegin:#gallery',
    )


def finished(job_id: str, image: str) -> tuple:
    """Three swaps at once, and the reason the poll chain ends here.

    The polling fragment empties out, the image joins the gallery, and the
    form comes back blank. The form is only reset on success: a failed job
    leaves what was submitted in place so it can be corrected and retried.
    """
    return (
        Div(id=f'job-{job_id}'),
        gallery_item(job_id, image),
        upload_form(replace=True),
    )


def showcase(before: str, after: str, before_label: str, after_label: str) -> Div:
    return Div(
        Div(P(before_label), Img(src=before, alt='Original picture'), cls='showcase-img'),
        Div(P(after_label), Img(src=after, alt='Processed picture'), cls='showcase-img'),
        cls='showcase',
    )


def page() -> tuple:
    return (
        *head_tags(),
        Span(A('Fork me on GitHub', href='https://github.com/ericovis/deal_with_it'),
             id='forkongithub'),
        Header(
            Nav(Div(Ul(
                Li(A('How it works', href='#how')),
                Li(A('Try it', href='#demo')),
                Li(A('API Docs', href='#api')),
                Li(A('License', href='#license')),
                cls='navigation'), cls='container')),
            H1('Deal With It!'),
            H2('A Python API for creating "Deal With It"-like Images'),
        ),
        Main(
            Section(Div(
                H2('How it works'),
                P('They say that "a picture is worth a thousand words". '
                  'So well, this is what this API does:'),
                showcase('/static/img/me.jpg', '/static/img/deal_with_me.png',
                         'Turns this:', 'Into this:'),
                P('It also works on pictures with multiple people:'),
                showcase('/static/img/multiple_people.jpg',
                         '/static/img/deal_with_multiple_people.png', 'Before:', 'After:'),
                P('Behind the scenes, this project uses the ',
                  A('face_recognition', href='https://github.com/ageitgey/face_recognition'),
                  ' library to identify faces in a given picture. On every identified face the '
                  'library returns, among other stuff, the information about face landmarks '
                  'which includes coordinates for a person\'s eyes.'),
                P('With that information, this package then calculates the angle on which a '
                  'person\'s head is leaned to correctly place the "deal with it" glasses on '
                  'the person\'s eyes.'),
                P('The image manipulation is done with ',
                  A('Pillow', href='https://github.com/python-pillow/Pillow'),
                  ' in a background worker managed by ',
                  A('RQ', href='https://python-rq.org'),
                  ', because detecting faces takes long enough that doing it inside the request '
                  'would block everything else. The page you are looking at is ',
                  A('FastHTML', href='https://fastht.ml'), ' and the REST API is ',
                  A('FastAPI', href='https://fastapi.tiangolo.com'), '.'),
                cls='container'), id='how'),
            Section(Div(
                H2('Try it'),
                P('Either insert a valid image URL or choose a picture from your computer, '
                  'then click "Deal with it!" to see the API in action.'),
                Div(id='result'),
                upload_form(),
                Div(
                    H4('This session'),
                    P('These live in this page only. Reloading loses them, so '
                      'download anything you want to keep.', cls='gallery-note'),
                    Div(id='gallery'),
                    id='gallery-section',
                ),
                cls='container'), id='demo'),
            Section(Div(
                H2('Api Docs'),
                P('There is a REST API documented at ', A('/api/docs', href='/api/docs'), '. '
                  'Processing happens in the background, so it takes two calls: one to submit '
                  'the image, one to collect the result.'),
                H4('Submit an image'),
                P('POST to ', Code('/api/jobs'), ' with either of these bodies:'),
                Pre(Code('{\n  "url": "https://ericovis.com/images/me.jpg"\n}', cls='json')),
                Pre(Code('{\n  "base64": "data:image/png;base64,..."\n}', cls='json')),
                P('The response points at the job you just created:'),
                Pre(Code('{\n  "job_id": "9f2c...",\n  "state": "queued",\n'
                         '  "status_url": "/api/jobs/9f2c..."\n}', cls='json')),
                H4('Collect the result'),
                P('GET the ', Code('status_url'), ' until ', Code('state'), ' is ',
                  Code('finished'), ' or ', Code('failed'), ':'),
                Pre(Code('{\n  "job_id": "9f2c...",\n  "state": "finished",\n'
                         '  "image": "data:image/png;base64,...",\n  "error": null,\n'
                         '  "progress": 100,\n  "step": "Done"\n}', cls='json')),
                P('While a job is running, ', Code('progress'), ' and ', Code('step'),
                  ' report which stage it has reached.'),
                cls='container'), id='api'),
            Section(Div(
                H2('License'),
                P(A('MIT License',
                    href='https://github.com/ericovis/deal_with_it/blob/master/LICENSE')),
                cls='container'), id='license'),
        ),
        Footer(Div('© 2018-2026 ', A('Eric Magalhães', href='https://ericovis.com'),
                   cls='container')),
    )


async def _request_from_form(url: str, image: UploadFile | str | None) -> ImageRequest:
    """Turn the two form fields into a validated request.

    An empty file input arrives as ``''`` rather than None, and a URL field
    left blank arrives as ``''`` too -- neither should count as "provided".
    """
    settings = get_settings()
    data: dict[str, str | None] = {'url': None, 'base64': None}

    if isinstance(image, UploadFile) and image.filename:
        content_type = image.content_type or ''
        if not content_type.startswith('image/'):
            raise ValueError('That file does not look like an image.')
        payload = await image.read()
        if len(payload) > settings.max_image_bytes:
            raise ValueError(
                f'That image is bigger than the {settings.max_image_bytes // 1024 // 1024} MB '
                'limit.'
            )
        if not payload:
            raise ValueError('That file is empty.')
        encoded = base64.b64encode(payload).decode()
        data['base64'] = f'data:{content_type};base64,{encoded}'
    elif url.strip():
        data['url'] = url.strip()
    else:
        raise ValueError('No image or URL was provided!')

    return ImageRequest.model_validate(data)


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

    @rt('/')
    def get():
        return page()

    @app.post('/submit')
    async def submit(url: str = '', image: UploadFile | str | None = None):
        try:
            request = await _request_from_form(url, image)
        except ValidationError as exc:
            # Caught before ValueError on purpose: ValidationError subclasses
            # it, and str() on one is a multi-line report, not a sentence.
            return failure(_first_message(exc))
        except ValueError as exc:
            return failure(str(exc))

        job_id, _ = jobs.enqueue(request.as_payload())
        return working(job_id, 'In the queue')

    @rt('/jobs/{job_id}')
    def job_fragment(job_id: str):
        result = jobs.result_for(job_id)
        if result is None:
            return failure('That job has expired. Try again?', job_id)
        if result.state == JobState.FINISHED:
            return finished(job_id, result.image)
        if result.state.is_terminal:
            return failure(result.error or 'That did not work.', job_id)
        if result.state == JobState.STARTED:
            return working(job_id, result.step or 'Working on it', result.progress)
        return working(job_id, 'In the queue')

    return app
