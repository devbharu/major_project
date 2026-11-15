"""Production entry point:  gunicorn wsgi:app  (single worker: the job queue, event bus and rate limiter live in-process)."""
from app import create_app

app = create_app()
