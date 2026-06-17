"""WSGI entry point — for gunicorn"""
import os
from app.server import create_app

db_url = os.environ.get("DATABASE_URL", "sqlite:///data/center.db")
app = create_app(database_url=db_url)
