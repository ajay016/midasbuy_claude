"""
Create (or promote) an admin panel user.

This replaces the old DEBUG-only "test@local" auto-merchant: there are no seeded
accounts anymore, so bootstrap your first admin with:

    python manage.py create_admin --email you@company.com --name "You"

You'll be prompted for a password (hidden) unless you pass --password. Re-running
for an existing email promotes that user to admin and (optionally) resets the
password — handy if you ever lock yourself out.
"""
import getpass

from django.core.management.base import BaseCommand, CommandError

from apiauth.models import Merchant


class Command(BaseCommand):
    help = "Create or promote an admin user for the panel."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True)
        parser.add_argument("--name", default="")
        parser.add_argument(
            "--password",
            default=None,
            help="Set non-interactively. Omit to be prompted (hidden input).",
        )

    def handle(self, *args, **opts):
        email = opts["email"].strip().lower()
        name = opts["name"].strip() or email.split("@")[0]
        password = opts["password"]

        existing = Merchant.objects.filter(email__iexact=email).first()

        if password is None:
            prompt = "New password" if not existing else "New password (blank = keep current)"
            password = getpass.getpass(f"{prompt}: ")
            if not existing or password:
                confirm = getpass.getpass("Confirm password: ")
                if password != confirm:
                    raise CommandError("Passwords do not match.")

        if not existing and len(password) < 8:
            raise CommandError("Password must be at least 8 characters.")

        if existing:
            existing.role = Merchant.ROLE_ADMIN
            existing.is_active = True
            if password:
                existing.set_password(password)
            existing.save()
            self.stdout.write(self.style.SUCCESS(f"Promoted {email} to admin."))
            return

        user = Merchant(name=name, email=email, role=Merchant.ROLE_ADMIN, is_active=True)
        user.set_password(password)
        user.save()
        self.stdout.write(self.style.SUCCESS(f"Created admin {email}."))
