"""
Create (or promote) an admin user for the panel.

Bootstrap your first admin with:

    python manage.py create_admin --email you@company.com --name "You"

You'll be prompted for a password (hidden) unless you pass --password. Re-running
for an existing email promotes that user to admin and (optionally) resets the
password — handy if you ever lock yourself out. (This is a convenience wrapper
around the same thing `createsuperuser` does.)
"""
import getpass

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

User = get_user_model()


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

        existing = User.objects.filter(email__iexact=email).first()

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
            existing.role = User.ROLE_ADMIN
            existing.is_staff = True
            existing.is_superuser = True
            existing.is_active = True
            if password:
                existing.set_password(password)
            existing.save()
            self.stdout.write(self.style.SUCCESS(f"Promoted {email} to admin."))
            return

        User.objects.create_superuser(email=email, password=password, name=name)
        self.stdout.write(self.style.SUCCESS(f"Created admin {email}."))
