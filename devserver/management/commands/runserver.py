"""
`python manage.py runserver` for this project.

Django's built-in runserver serves only the Django part (WSGI). The JSON API
lives in FastAPI, mounted at /api by midasbuy_project/asgi.py, so under the
built-in runserver every /api call 404s with an HTML page and the panel shows
"Unexpected token '<'". This override runs the full ASGI app with uvicorn
instead, with auto-reload, so the familiar command just works.
"""
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Run the full app (Django panel + FastAPI /api) with uvicorn and auto-reload."

    # Uvicorn's reloader re-imports the app in a subprocess; Django's own
    # system checks would otherwise run twice on every start.
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("addrport", nargs="?", default="127.0.0.1:8000",
                            help="Optional port or ipaddr:port (default 127.0.0.1:8000).")
        parser.add_argument("--noreload", action="store_true",
                            help="Disable auto-reload on code changes.")
        # Accepted for compatibility with Django's runserver; not used.
        parser.add_argument("--nothreading", action="store_true", help="Ignored.")
        parser.add_argument("--ipv6", "-6", action="store_true", help="Ignored.")
        parser.add_argument("--insecure", action="store_true", help="Ignored.")
        parser.add_argument("--skip-checks", action="store_true", help="Ignored.")

    def handle(self, *args, **options):
        import uvicorn

        addrport = options["addrport"]
        host, _, port = addrport.rpartition(":")
        host = host or "127.0.0.1"
        try:
            port = int(port)
        except ValueError:
            raise CommandError(f'"{addrport}" is not a valid port or ipaddr:port.')

        self.stdout.write(
            f"Starting Django + FastAPI (uvicorn) at http://{host}:{port}/  (API at /api)"
        )
        uvicorn.run(
            "midasbuy_project.asgi:application",
            host=host,
            port=port,
            reload=not options["noreload"],
        )
