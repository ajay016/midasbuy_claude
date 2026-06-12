"""
Log a Midasbuy account in and persist its browser session.

Login launches a HEADFUL Chromium (to save real session/cookies), so it must run
where a browser exists. In Docker that is the *worker* image, not the slim web
image. Inside the worker the browser needs a virtual display, so wrap it in xvfb:

    docker compose exec worker xvfb-run -a python manage.py login_account 1

Outside Docker (e.g. on your Windows desktop, with a visible window) just:

    python manage.py login_account 1
"""
from django.core.management.base import BaseCommand, CommandError

from accounts.models import MidasbuyAccount
from accounts.services.login_service import login_account_and_persist


class Command(BaseCommand):
    help = "Log a Midasbuy account in (headful browser) and persist its session."

    def add_arguments(self, parser):
        parser.add_argument("account_id", type=int, help="MidasbuyAccount primary key")

    def handle(self, *args, **opts):
        try:
            acct = MidasbuyAccount.objects.get(pk=opts["account_id"])
        except MidasbuyAccount.DoesNotExist:
            raise CommandError(f"account_id {opts['account_id']} not found")

        self.stdout.write(f"Logging in {acct.email} (account #{acct.pk}) …")
        result = login_account_and_persist(acct)

        success = getattr(result, "success", None)
        message = getattr(result, "message", "") or getattr(result, "error", "")
        if success:
            self.stdout.write(self.style.SUCCESS(f"Login OK. {message}"))
        else:
            self.stdout.write(self.style.ERROR(f"Login failed. {message}"))
