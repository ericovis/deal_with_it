import os

PORT = os.environ.get('PORT', 5000)
WORKERS = os.environ.get('WORKERS', 2)

bind = f"0.0.0.0:{PORT}"
wsgi_app = 'app:app'
workers = WORKERS

