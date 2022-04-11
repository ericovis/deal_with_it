import os
import multiprocessing

PORT = os.environ.get('PORT', 5000)

bind = f"0.0.0.0:{PORT}"
wsgi_app = 'app:app'
workers = multiprocessing.cpu_count() * 2 + 1

