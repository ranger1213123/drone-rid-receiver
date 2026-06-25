"""pytest fixtures: isolated Flask app + test client with in-memory SQLite.

Usage:
    pip install pytest
    pytest tests/ -v
"""

import pytest
from app.server import create_app


@pytest.fixture(scope="session")
def _test_app():
    """Session-scoped Flask app with in-memory SQLite — shared across tests.

    Uses a file-backed temp DB (not :memory:) because SQLAlchemy's create_all
    + multiple connections interact poorly with pure in-memory SQLite.
    """
    import tempfile
    import atexit
    import os

    _tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_url = f"sqlite:///{_tmp.name}"

    os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-do-not-use-in-prod")
    os.environ.setdefault("WEB_SECRET_KEY", "test-web-secret-do-not-use-in-prod")

    app = create_app(
        database_url=db_url,
        config_overrides={
            "TESTING": True,
            "JWT_SECRET_KEY": "test-jwt-secret",
        },
    )

    def _cleanup():
        import os as _os
        try:
            _os.unlink(_tmp.name)
        except OSError:
            pass

    atexit.register(_cleanup)
    return app, _tmp.name


@pytest.fixture
def app(_test_app):
    """Per-test app reference (same instance, session-scoped DB)."""
    return _test_app[0]


@pytest.fixture
def client(app):
    """Flask test client."""
    return app.test_client()


@pytest.fixture
def csrf(client):
    """Fetch a CSRF token as a logged-in admin, returns the token string."""
    # Login first
    client.post("/login", data={"username": "admin", "password": "admin123"},
                follow_redirects=True)
    resp = client.get("/api/csrf-token")
    data = resp.get_json()
    return data.get("token", "")


@pytest.fixture
def admin_client(client, csrf):
    """Test client pre-authenticated as admin with CSRF token set."""
    client.environ_base.setdefault("HTTP_X_CSRF_TOKEN", csrf)
    return client


@pytest.fixture
def api(admin_client):
    """Convenience: call api.post(path, data) etc. with admin auth."""
    import json

    class ApiHelper:
        @staticmethod
        def _headers():
            return {"Content-Type": "application/json"}

        def get(self, path, **kwargs):
            return admin_client.get(path, **kwargs)

        def post(self, path, data):
            return admin_client.post(
                path, data=json.dumps(data), headers=self._headers()
            )

        def put(self, path, data):
            return admin_client.put(
                path, data=json.dumps(data), headers=self._headers()
            )

        def delete(self, path, data=None):
            return admin_client.delete(
                path, data=json.dumps(data) if data else None, headers=self._headers()
            )

    return ApiHelper()
