import os
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = "Securely bootstrap initial system users (ADMIN and optional EMPLOYEE) from environment variables."

    def handle(self, *args, **options):
        admin_username = os.environ.get("BOOTSTRAP_ADMIN_USERNAME")
        admin_password = os.environ.get("BOOTSTRAP_ADMIN_PASSWORD")
        admin_email = os.environ.get("BOOTSTRAP_ADMIN_EMAIL", "admin@simulated.bebank.local")

        emp_username = os.environ.get("BOOTSTRAP_EMPLOYEE_USERNAME")
        emp_password = os.environ.get("BOOTSTRAP_EMPLOYEE_PASSWORD")
        emp_email = os.environ.get("BOOTSTRAP_EMPLOYEE_EMAIL", "employee@simulated.bebank.local")

        if not admin_username or not admin_password:
            raise CommandError("BOOTSTRAP_ADMIN_USERNAME and BOOTSTRAP_ADMIN_PASSWORD must be set in the environment.")

        has_emp_user = bool(emp_username)
        has_emp_pass = bool(emp_password)
        if has_emp_user != has_emp_pass:
            raise CommandError(
                "Both BOOTSTRAP_EMPLOYEE_USERNAME and BOOTSTRAP_EMPLOYEE_PASSWORD must be provided, or both omitted."
            )

        if has_emp_user and admin_username == emp_username:
            raise CommandError("Admin and Employee usernames cannot be identical.")

        User = get_user_model()

        # Pre-validate passwords before making any database modifications
        temp_admin = User(username=admin_username, email=admin_email, role="ADMIN")
        validate_password(admin_password, user=temp_admin)

        if has_emp_user:
            temp_emp = User(username=emp_username, email=emp_email, role="EMPLOYEE")
            validate_password(emp_password, user=temp_emp)

        # Check existing user conflicts before DB writes
        existing_admin = User.objects.filter(username=admin_username).first()
        if existing_admin:
            if not existing_admin.is_active:
                raise CommandError(
                    f"Admin user '{admin_username}' already exists but is inactive. "
                    "Cannot reactivate, elevate, or overwrite inactive admin account."
                )
            if not (existing_admin.role == "ADMIN" and existing_admin.is_staff and existing_admin.is_superuser):
                raise CommandError(
                    f"User '{admin_username}' already exists with role '{existing_admin.role}' "
                    f"(is_staff={existing_admin.is_staff}, is_superuser={existing_admin.is_superuser}). "
                    "Cannot elevate or overwrite conflicting account."
                )

        existing_emp = None
        if has_emp_user:
            existing_emp = User.objects.filter(username=emp_username).first()
            if existing_emp:
                if not existing_emp.is_active:
                    raise CommandError(
                        f"Employee user '{emp_username}' already exists but is inactive. "
                        "Cannot reactivate, elevate, or overwrite inactive employee account."
                    )
                if not (existing_emp.role == "EMPLOYEE" and not existing_emp.is_staff and not existing_emp.is_superuser):
                    raise CommandError(
                        f"User '{emp_username}' already exists with role '{existing_emp.role}' "
                        f"(is_staff={existing_emp.is_staff}, is_superuser={existing_emp.is_superuser}). "
                        "Cannot elevate or overwrite conflicting account."
                    )

        messages = []
        with transaction.atomic():
            if not existing_admin:
                User.objects.create_superuser(
                    username=admin_username,
                    email=admin_email,
                    password=admin_password,
                    role="ADMIN",
                )
                messages.append((self.style.SUCCESS, f"Successfully created admin user: {admin_username}"))
            else:
                messages.append((self.style.NOTICE, f"Admin user '{admin_username}' already exists; skipping creation."))

            if has_emp_user:
                if not existing_emp:
                    User.objects.create_user(
                        username=emp_username,
                        email=emp_email,
                        password=emp_password,
                        role="EMPLOYEE",
                        is_staff=False,
                        is_superuser=False,
                    )
                    messages.append((self.style.SUCCESS, f"Successfully created employee user: {emp_username}"))
                else:
                    messages.append(
                        (self.style.NOTICE, f"Employee user '{emp_username}' already exists; skipping creation.")
                    )

        # Output messages only after the atomic transaction has successfully committed
        for style_fn, msg in messages:
            self.stdout.write(style_fn(msg))
