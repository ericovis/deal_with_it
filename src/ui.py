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
    Section,
    Span,
    Title,
    Ul,
    UploadFile,
    fast_app,
)
from fasthtml.svg import Circle, Svg
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


def spinner() -> Svg:
    return Svg(
        Circle(cx=50, cy=50, r=42, transform='rotate(-90,50,50)'),
        cls='spinner', viewBox='0 0 100 100', width=20, height=20,
    )


def upload_form() -> Form:
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
        hx_post='/submit',
        hx_target='#result',
        hx_swap='innerHTML',
        hx_encoding='multipart/form-data',
        hx_disabled_elt='find button',
    )


def working(job_id: str, message: str = 'Working on it...') -> Div:
    """A fragment that replaces itself with the next poll's answer.

    ``load delay:`` rather than ``every``: the chain ends by itself as soon as
    a response comes back without the hx-get, so a finished job cannot leave a
    timer running against whatever replaced it.
    """
    return Div(
        spinner(),
        P(message, id='message'),
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


def finished(job_id: str, image: str) -> Div:
    return Div(
        Img(id='result-img', src=image, alt='Deal with me'),
        P(A('Download', href=image, download='deal_with_it.png'), id='message'),
        id=f'job-{job_id}',
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
                cls='container'), id='demo'),
            Section(Div(
                H2('Api Docs'),
                P('There is a REST API documented at ', A('/api/docs', href='/api/docs'), '. '
                  'Processing happens in the background, so it takes two calls: one to submit '
                  'the image, one to collect the result.'),
                H4('Submit an image'),
                P('POST to ', Code('/api/jobs'), ' with either of these bodies:'),
                Pre(Code('{\n  "url": "https://emagalha.es/images/me.jpg"\n}', cls='json')),
                Pre(Code('{\n  "base64": "data:image/png;base64,..."\n}', cls='json')),
                P('The response points at the job you just created:'),
                Pre(Code('{\n  "job_id": "9f2c...",\n  "state": "queued",\n'
                         '  "status_url": "/api/jobs/9f2c..."\n}', cls='json')),
                H4('Collect the result'),
                P('GET the ', Code('status_url'), ' until ', Code('state'), ' is ',
                  Code('finished'), ' or ', Code('failed'), ':'),
                Pre(Code('{\n  "job_id": "9f2c...",\n  "state": "finished",\n'
                         '  "image": "data:image/png;base64,...",\n  "error": null\n}',
                         cls='json')),
                cls='container'), id='api'),
            Section(Div(
                H2('License'),
                P(A('MIT License',
                    href='https://github.com/ericovis/deal_with_it/blob/master/LICENSE')),
                cls='container'), id='license'),
        ),
        Footer(Div('© 2018-2026 ', A('Eric Magalhães', href='https://emagalha.es'),
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
        return working(job_id)

    @rt('/jobs/{job_id}')
    def job_fragment(job_id: str):
        result = jobs.result_for(job_id)
        if result is None:
            return failure('That job has expired. Try again?', job_id)
        if result.state == JobState.FINISHED:
            return finished(job_id, result.image)
        if result.state.is_terminal:
            return failure(result.error or 'That did not work.', job_id)
        message = 'Looking for faces...' if result.state == JobState.STARTED else 'In the queue...'
        return working(job_id, message)

    return app
