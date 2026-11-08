"""Single WSGI entrypoint for `serve`: /public/* -> the public proxy (public.public_api.App,
no key, its own rate limits), everything else -> the admin API (nexus_master.api.App,
unchanged, key required). One process, one port -- round-3-proxy-spec.md's "dispatcher
WSGI אחד", not two separate servers on two ports.
"""
from .api import App as AdminApp

try:
    from public.public_api import App as PublicApp
except ImportError as e:
    # engine/ not on sys.path as the sibling of nexus_master/ and public/ (e.g. this
    # module imported without `engine` as the top-level dir) -- surfaced with a clear
    # message instead of a bare ModuleNotFoundError pointing at an unrelated line.
    raise ImportError(
        "nexus_master.dispatcher needs engine/public on sys.path -- run with `engine` as "
        "the top-level dir (python -m nexus_master ..., or -t engine for tests)") from e

ALLOWED_PUBLIC_ORIGIN = "https://keonkkk.github.io"
_CORS_HEADERS = [
    ("Access-Control-Allow-Origin", ALLOWED_PUBLIC_ORIGIN),
    ("Access-Control-Allow-Methods", "GET, POST, OPTIONS"),
    ("Access-Control-Allow-Headers", "Content-Type"),
]


class Dispatcher:
    def __init__(self, conn, tenant="default", api_key=None):
        self.public_app = PublicApp(conn, tenant=tenant)
        self.admin_app = AdminApp(conn, tenant=tenant, api_key=api_key)

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "/")
        if path == "/public" or path.startswith("/public/"):
            return self._public(environ, start_response)
        return self.admin_app(environ, start_response)

    def _public(self, environ, start_response):
        if environ.get("REQUEST_METHOD") == "OPTIONS":
            # Preflight only: a JSON POST from pages/status.html is a non-simple fetch()
            # request and the browser never sends it without this responding first.
            start_response("204 No Content", _CORS_HEADERS + [("Content-Length", "0")])
            return [b""]
        return self.public_app(environ, lambda status, headers: start_response(status, headers + _CORS_HEADERS))
