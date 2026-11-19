import getpass
import os
import sys
from decimal import Decimal
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from banking.models import BankAccount


class Command(BaseCommand):
    help = "Safely provision local demo login accounts (CUSTOMER, EMPLOYEE, ADMIN) into local SQLite."

    def add_arguments(self, parser):
        parser.add_argument(
            '--customer-password',
            dest='customer_password',
            help='Password for demo customer (manojkumar). If omitted, read from DEMO_CUSTOMER_PASSWORD env var or prompt.',
        )
        parser.add_argument(
            '--employee-password',
            dest='employee_password',
            help='Password for demo employee (murugananthan). If omitted, read from DEMO_EMPLOYEE_PASSWORD env var or prompt.',
        )
        parser.add_argument(
            '--admin-password',
            dest='admin_password',
            help='Password for demo admin (nithishkumar). If omitted, read from DEMO_ADMIN_PASSWORD env var or prompt.',
        )

    def _verify_local_sqlite(self):
        """Strictly verify that the active database is local SQLite and never remote/production."""
        if os.environ.get("DATABASE_URL"):
            raise CommandError(
                "DATABASE_URL environment variable is set. Demo account provisioning is strictly restricted to local SQLite."
            )

        if connection.vendor != 'sqlite':
            raise CommandError(
                f"Active database vendor is '{connection.vendor}'. Demo account provisioning is strictly restricted to local SQLite."
            )

        db_engine = connection.settings_dict.get('ENGINE', '')
        if 'sqlite' not in db_engine:
            raise CommandError(
                f"Active database engine is '{db_engine}'. Demo account provisioning is strictly restricted to local SQLite."
            )

    def _get_password(self, cli_val, env_var_name, prompt_label):
        """Retrieve password from CLI option, environment variable, or secure interactive prompt."""
        if cli_val:
            return cli_val

        env_val = os.environ.get(env_var_name)
        if env_val:
            return env_val

        if sys.stdin.isatty():
            entered = getpass.getpass(f"Enter password for {prompt_label}: ")
            if not entered:
                raise CommandError(f"Password for {prompt_label} cannot be empty.")
            return entered

        raise CommandError(
            f"Missing password for {prompt_label}. Set {env_var_name} or pass the corresponding CLI argument."
        )

    def handle(self, *args, **options):
        self._verify_local_sqlite()

        User = get_user_model()

        # 1. Credentials retrieval
        cust_user = "manojkumar"
        cust_role = "CUSTOMER"
        cust_email = "manojkumar@bebank.local"
        cust_pass = self._get_password(
            options.get('customer_password'),
            'DEMO_CUSTOMER_PASSWORD',
            f"Customer ({cust_user})"
        )

        emp_user = "murugananthan"
        emp_role = "EMPLOYEE"
        emp_name = "Murugananthan"
        emp_email = "murugananthan@bebank.local"
        emp_pass = self._get_password(
            options.get('employee_password'),
            'DEMO_EMPLOYEE_PASSWORD',
            f"Employee ({emp_user})"
        )

        adm_user = "nithishkumar"
        adm_role = "ADMIN"
        adm_email = "nithishkumar@bebank.local"
        adm_pass = self._get_password(
            options.get('admin_password'),
            'DEMO_ADMIN_PASSWORD',
            f"Admin ({adm_user})"
        )

        # 2. Pre-validate passwords
        validate_password(cust_pass, user=User(username=cust_user, role=cust_role))
        validate_password(emp_pass, user=User(username=emp_user, role=emp_role))
        validate_password(adm_pass, user=User(username=adm_user, role=adm_role))

        # 3. Check existing role conflicts before writes
        configs = [
            (cust_user, cust_role),
            (emp_user, emp_role),
            (adm_user, adm_role),
        ]
        for uname, expected_role in configs:
            existing = User.objects.filter(username=uname).first()
            if existing and existing.role != expected_role:
                raise CommandError(
                    f"User '{uname}' already exists with conflicting role '{existing.role}' "
                    f"(expected '{expected_role}'). Aborting to prevent unintended privilege alteration."
                )

        messages = []

        with transaction.atomic():
            # A. Provision Customer
            cust_obj = User.objects.filter(username=cust_user).first()
            if not cust_obj:
                cust_obj = User.objects.create_user(
                    username=cust_user,
                    email=cust_email,
                    password=cust_pass,
                    role=cust_role,
                    is_staff=False,
                    is_superuser=False,
                    kyc_status='APPROVED',
                    kyc_verified=True,
                )
                messages.append((self.style.SUCCESS, f"Created Customer user: '{cust_user}' (Role: {cust_role})"))
            else:
                cust_obj.set_password(cust_pass)
                cust_obj.is_active = True
                cust_obj.account_locked = False
                cust_obj.failed_login_attempts = 0
                cust_obj.email = cust_email
                cust_obj.kyc_status = 'APPROVED'
                cust_obj.kyc_verified = True
                cust_obj.save()
                messages.append((self.style.NOTICE, f"Updated existing Customer user: '{cust_user}' (Role: {cust_role})"))

            # Ensure linked BankAccount for Customer
            account = BankAccount.objects.filter(user=cust_obj).first()
            if not account:
                account = BankAccount.objects.create(
                    user=cust_obj,
                    account_type='SAVINGS',
                    balance=Decimal('0.00'),
                    email=cust_email,
                    phone='9876543210',
                    aadhaar='123456789012',
                    pan='ABCDE1234F',
                    address='Bebank Customer Avenue',
                    pincode='560001'
                )
                messages.append((self.style.SUCCESS, f"Created BankAccount '{account.account_number}' (SAVINGS, ₹{account.balance}) for '{cust_user}'"))
            else:
                # If account has balance with zero ledger transactions, reconcile to 0.00 to preserve ledger consistency without fabricating a deposit
                if account.balance != Decimal('0.00') and account.transaction_set.count() == 0:
                    account.balance = Decimal('0.00')
                    account.save(update_fields=['balance'])
                    messages.append((self.style.NOTICE, f"Reconciled BankAccount '{account.account_number}' balance to ₹0.00 matching zero ledger transactions."))
                else:
                    messages.append((self.style.NOTICE, f"Customer '{cust_user}' already has linked BankAccount '{account.account_number}'; skipping duplicate account creation."))

            # B. Provision Employee
            emp_obj = User.objects.filter(username=emp_user).first()
            if not emp_obj:
                emp_obj = User.objects.create_user(
                    username=emp_user,
                    email=emp_email,
                    password=emp_pass,
                    role=emp_role,
                    first_name=emp_name,
                    is_staff=False,
                    is_superuser=False,
                )
                messages.append((self.style.SUCCESS, f"Created Employee user: '{emp_user}' (Name: {emp_name}, Role: {emp_role})"))
            else:
                emp_obj.set_password(emp_pass)
                emp_obj.is_active = True
                emp_obj.account_locked = False
                emp_obj.failed_login_attempts = 0
                emp_obj.first_name = emp_name
                emp_obj.email = emp_email
                emp_obj.is_staff = False
                emp_obj.is_superuser = False
                emp_obj.save()
                messages.append((self.style.NOTICE, f"Updated existing Employee user: '{emp_user}' (Role: {emp_role})"))

            # C. Provision Admin
            adm_obj = User.objects.filter(username=adm_user).first()
            if not adm_obj:
                adm_obj = User.objects.create_superuser(
                    username=adm_user,
                    email=adm_email,
                    password=adm_pass,
                    role=adm_role,
                )
                messages.append((self.style.SUCCESS, f"Created Admin superuser: '{adm_user}' (Role: {adm_role})"))
            else:
                adm_obj.set_password(adm_pass)
                adm_obj.is_active = True
                adm_obj.account_locked = False
                adm_obj.failed_login_attempts = 0
                adm_obj.email = adm_email
                adm_obj.is_staff = True
                adm_obj.is_superuser = True
                adm_obj.save()
                messages.append((self.style.NOTICE, f"Updated existing Admin user: '{adm_user}' (Role: {adm_role})"))

        for style_fn, msg in messages:
            self.stdout.write(style_fn(msg))

