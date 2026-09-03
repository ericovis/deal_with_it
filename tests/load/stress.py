"""Load and abuse testing against a *real* stack. Not collected by pytest.

    uv run python tests/load/stress.py throughput --jobs 40 --concurrency 8
    uv run python tests/load/stress.py big --pixels 12
    uv run python tests/load/stress.py abuse
    uv run python tests/load/stress.py slowloris --sockets 200

See docs/LOAD.md for how to bring the stack up and what the numbers mean.
"""

import argparse
import base64
import contextlib
import socket
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path

import requests
from PIL import Image

HERE = Path(__file__).resolve().parent
IMG = HERE.parents[1] / 'src' / 'static' / 'img'


def pct(values, p):
    if not values:
        return float('nan')
    values = sorted(values)
    return values[min(len(values) - 1, int(round(p / 100 * (len(values) - 1))))]


def summary(name, values, unit='s'):
    if not values:
        return f'{name}: none'
    return (f'{name}: n={len(values)} p50={pct(values, 50):.3f}{unit} '
            f'p95={pct(values, 95):.3f}{unit} max={max(values):.3f}{unit}')


def data_uri(payload: bytes, mime: str = 'image/jpeg') -> str:
    return f'data:{mime};base64,' + base64.b64encode(payload).decode()


def upscaled_jpeg(source: Path, megapixels: float, quality: int = 80) -> bytes:
    """A sample blown up to a phone-camera size, so detection has real work."""
    image = Image.open(source).convert('RGB')
    w, h = image.size
    factor = (megapixels * 1_000_000 / (w * h)) ** 0.5
    image = image.resize((round(w * factor), round(h * factor)), Image.BICUBIC)
    buffer = BytesIO()
    image.save(buffer, format='JPEG', quality=quality)
    return buffer.getvalue()


def bomb_png(side: int = 20_000) -> bytes:
    """A few KB on disk, 400 megapixels once decoded."""
    buffer = BytesIO()
    Image.new('L', (side, side), 0).save(buffer, format='PNG', optimize=True)
    return buffer.getvalue()


class Client:
    def __init__(self, base: str, poll: float):
        self.base = base.rstrip('/')
        self.poll = poll
        self.session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=64, pool_maxsize=64)
        self.session.mount('http://', adapter)
        self.session.mount('https://', adapter)

    def submit(self, body: dict) -> tuple[int, dict, float]:
        t = time.perf_counter()
        response = self.session.post(f'{self.base}/api/jobs', json=body, timeout=60)
        elapsed = time.perf_counter() - t
        try:
            payload = response.json()
        except ValueError:
            payload = {'detail': response.text[:200]}
        return response.status_code, payload, elapsed

    def wait(self, job_id: str, deadline: float = 300) -> tuple[dict, list[float]]:
        polls = []
        started = time.perf_counter()
        while time.perf_counter() - started < deadline:
            t = time.perf_counter()
            response = self.session.get(f'{self.base}/api/jobs/{job_id}', timeout=60)
            polls.append(time.perf_counter() - t)
            result = response.json()
            if result.get('state') in ('finished', 'failed'):
                return result, polls
            time.sleep(self.poll)
        return {'state': 'timeout'}, polls

    def health_latency(self) -> float:
        t = time.perf_counter()
        self.session.get(f'{self.base}/api/health', timeout=30)
        return time.perf_counter() - t


def run_jobs(client: Client, bodies: list[dict], concurrency: int, label: str) -> dict:
    """Submit every body, wait for each, and report what happened."""
    submit_times, e2e_times, poll_times, health_times = [], [], [], []
    states: dict[str, int] = {}
    statuses: dict[int, int] = {}
    faces: list[int] = []
    result_bytes: list[int] = []
    lock = threading.Lock()
    stop = threading.Event()

    def probe_health():
        while not stop.is_set():
            latency = client.health_latency()
            with lock:
                health_times.append(latency)
            time.sleep(0.5)

    def one(body):
        code, payload, elapsed = client.submit(body)
        with lock:
            submit_times.append(elapsed)
            statuses[code] = statuses.get(code, 0) + 1
        if code != 202:
            with lock:
                states[f'refused:{code}'] = states.get(f'refused:{code}', 0) + 1
            return
        started = time.perf_counter()
        result, polls = client.wait(payload['job_id'])
        with lock:
            e2e_times.append(time.perf_counter() - started + elapsed)
            poll_times.extend(polls)
            states[result['state']] = states.get(result['state'], 0) + 1
            if result.get('image'):
                result_bytes.append(len(result['image']))
            if result.get('error'):
                states[f'error:{result["error"][:40]}'] = (
                    states.get(f'error:{result["error"][:40]}', 0) + 1)

    prober = threading.Thread(target=probe_health, daemon=True)
    prober.start()
    wall = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        list(pool.map(one, bodies))
    wall = time.perf_counter() - wall
    stop.set()
    prober.join()

    finished = states.get('finished', 0)
    print(f'\n== {label}: {len(bodies)} jobs, concurrency {concurrency} ==')
    print(f'wall: {wall:.1f}s  throughput: {finished / wall * 60:.1f} finished jobs/min')
    print('submit statuses:', dict(sorted(statuses.items())))
    print('outcomes:', dict(sorted(states.items())))
    print(summary('submit latency', submit_times))
    print(summary('end-to-end', e2e_times))
    print(summary('poll latency', poll_times))
    print(summary('health latency while loaded', health_times))
    if result_bytes:
        print(f'result data URI: mean {statistics.mean(result_bytes) / 1e6:.2f} MB, '
              f'max {max(result_bytes) / 1e6:.2f} MB')
    return {'wall': wall, 'throughput': finished / wall * 60, 'states': states,
            'statuses': statuses, 'faces': faces}


def cmd_throughput(args):
    client = Client(args.base, args.poll)
    names = args.images or ['me.jpg', 'multiple_people.jpg', 'solvay_1927.jpg',
                            'mona_lisa.jpg']
    bodies = []
    for i in range(args.jobs):
        name = names[i % len(names)]
        bodies.append({'base64': data_uri((IMG / name).read_bytes())})
    run_jobs(client, bodies, args.concurrency, f'samples {names}')


def cmd_big(args):
    client = Client(args.base, args.poll)
    payload = upscaled_jpeg(IMG / 'multiple_people.jpg', args.pixels, quality=args.quality)
    print(f'{args.pixels} MP JPEG is {len(payload) / 1e6:.2f} MB on the wire')
    bodies = [{'base64': data_uri(payload)} for _ in range(args.jobs)]
    run_jobs(client, bodies, args.concurrency, f'{args.pixels} MP uploads')


def cmd_abuse(args):
    client = Client(args.base, args.poll)
    base = client.base
    print('== abuse ==')

    def expect(name, ok, detail=''):
        print(f'[{"ok" if ok else "FAIL"}] {name} {detail}')

    # 1. Declared oversized body.
    limit = 10 * 1024 * 1024 * 4 // 3 + 64 * 1024
    r = client.session.post(f'{base}/api/jobs', data='x' * (limit + 1),
                            headers={'content-type': 'application/json'}, timeout=60)
    expect('declared oversized body is 413', r.status_code == 413, r.status_code)

    # 2. Chunked oversized body, no Content-Length.
    def chunks():
        sent = 0
        while sent < limit + 1_000_000:
            yield b'x' * 65536
            sent += 65536
    t = time.perf_counter()
    try:
        r = client.session.post(f'{base}/api/jobs', data=chunks(),
                                headers={'content-type': 'application/json'}, timeout=60)
        expect('chunked oversized body is 413', r.status_code == 413,
               f'{r.status_code} in {time.perf_counter() - t:.2f}s')
    except requests.RequestException as exc:
        # The server closes the connection mid-upload; that is also a refusal.
        expect('chunked oversized body is cut off', True,
               f'{type(exc).__name__} in {time.perf_counter() - t:.2f}s')

    # 3. Decompression bomb: tiny file, 400 MP decoded.
    code, payload, _ = client.submit({'base64': data_uri(bomb_png(), 'image/png')})
    result, _ = client.wait(payload['job_id'])
    expect('400 MP bomb is refused by the worker', 'pixel limit' in (result.get('error') or ''),
           result.get('error'))

    # 4. SSRF probes: every one must fail with the non-public message.
    probes = [
        'http://127.0.0.1:6379/', 'http://localhost/', 'http://0.0.0.0/',
        'http://[::1]/', 'http://[::ffff:127.0.0.1]/', 'http://2130706433/',
        'http://0x7f000001/', 'http://127.1/', 'http://169.254.169.254/latest/meta-data/',
        'http://[64:ff9b::7f00:1]/', 'http://10.0.0.1/', 'http://192.168.0.1/',
        'http://redis:6379/', 'http://web:5000/',
    ]
    for url in probes:
        code, payload, _ = client.submit({'url': url})
        if code == 202:
            result, _ = client.wait(payload['job_id'])
            error = result.get('error') or ''
            refused = 'non-public' in error or 'could not be resolved' in error
            expect(f'SSRF {url}', refused, error[:70])
        else:
            expect(f'SSRF {url}', code == 422, f'{code} {payload.get("detail")}'[:90])

    # 5. Rate limit / queue cap, from this one address.
    codes = {}
    for _ in range(args.flood):
        code, _, _ = client.submit({'url': 'https://example.com/nothing.png'})
        codes[code] = codes.get(code, 0) + 1
    expect(f'flood of {args.flood} URL submissions was throttled', 429 in codes or 503 in codes,
           dict(sorted(codes.items())))

    # 6. The loop is still responsive after all that.
    expect('health under 100ms', client.health_latency() < 0.1, f'{client.health_latency():.3f}s')


def cmd_slowloris(args):
    """Open many connections and send headers one byte at a time."""
    host, port = args.base.replace('http://', '').split(':')
    port = int(port)
    sockets = []
    for _ in range(args.sockets):
        try:
            s = socket.create_connection((host, port), timeout=5)
            s.send(b'POST /api/jobs HTTP/1.1\r\nHost: x\r\nContent-Length: 1000000\r\n')
            sockets.append(s)
        except OSError as exc:
            print('open failed:', exc)
            break
    print(f'holding {len(sockets)} half-open requests')
    client = Client(args.base, args.poll)
    for i in range(args.seconds):
        for s in sockets:
            with contextlib.suppress(OSError):
                s.send(b'X-A: b\r\n')
        latency = client.health_latency()
        print(f't={i + 1}s health latency {latency:.3f}s')
        time.sleep(1)
    for s in sockets:
        s.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--base', default='http://127.0.0.1:5001')
    parser.add_argument('--poll', type=float, default=0.5)
    sub = parser.add_subparsers(dest='command', required=True)

    p = sub.add_parser('throughput')
    p.add_argument('--jobs', type=int, default=40)
    p.add_argument('--concurrency', type=int, default=8)
    p.add_argument('--images', nargs='*')
    p.set_defaults(func=cmd_throughput)

    p = sub.add_parser('big')
    p.add_argument('--pixels', type=float, default=12)
    p.add_argument('--quality', type=int, default=80)
    p.add_argument('--jobs', type=int, default=6)
    p.add_argument('--concurrency', type=int, default=6)
    p.set_defaults(func=cmd_big)

    p = sub.add_parser('abuse')
    p.add_argument('--flood', type=int, default=60)
    p.set_defaults(func=cmd_abuse)

    p = sub.add_parser('slowloris')
    p.add_argument('--sockets', type=int, default=200)
    p.add_argument('--seconds', type=int, default=10)
    p.set_defaults(func=cmd_slowloris)

    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == '__main__':
    sys.exit(main())
