from decimal import Decimal
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction, IntegrityError
from banking.models import FixedDeposit, BankAccount, Transaction


class Command(BaseCommand):
    help = "Deterministically and idempotently settle all matured active Fixed Deposits into their linked accounts."

    def handle(self, *args, **options):
        now = timezone.now()
        eligible_fds = FixedDeposit.objects.filter(
            status='ACTIVE',
            maturity_date__lte=now
        ).select_related('account', 'account__user').order_by('id')

        total_eligible = eligible_fds.count()
        if total_eligible == 0:
            self.stdout.write(self.style.SUCCESS("No matured Fixed Deposits pending settlement."))
            return

        self.stdout.write(f"Found {total_eligible} eligible matured Fixed Deposit(s) for settlement...")

        settled_count = 0
        total_payout = Decimal('0.00')

        for fd in eligible_fds:
            reference_id = f"FD-MAT-{fd.id}"

            # Idempotency check: check if already settled in transaction ledger
            existing_tx = Transaction.objects.filter(
                reference_id=reference_id,
                transaction_type='FD_MATURITY_CREDIT'
            ).first()

            if existing_tx:
                self.stdout.write(self.style.WARNING(
                    f"FD #{fd.id} already has settlement ledger entry {reference_id}. Ensuring status marked MATURED."
                ))
                if fd.status != 'MATURED':
                    fd.status = 'MATURED'
                    fd.save(update_fields=['status'])
                continue

            try:
                with transaction.atomic():
                    locked_account = BankAccount.objects.select_for_update().get(id=fd.account_id)

                    # Re-verify under row lock
                    locked_fd = FixedDeposit.objects.select_for_update().get(id=fd.id)
                    if locked_fd.status != 'ACTIVE' or locked_fd.maturity_date > timezone.now():
                        self.stdout.write(self.style.NOTICE(f"Skipping FD #{fd.id}: not active or not yet matured."))
                        continue

                    # Verify account user is active and unlocked
                    if locked_account.user.account_locked or not locked_account.user.is_active:
                        self.stdout.write(self.style.ERROR(
                            f"Skipping FD #{fd.id}: account holder '{locked_account.user.username}' is locked or inactive."
                        ))
                        continue

                    payout = locked_fd.maturity_amount
                    balance_before = locked_account.balance
                    balance_after = balance_before + payout

                    locked_account.balance = balance_after
                    locked_account.save(update_fields=['balance'])

                    locked_fd.status = 'MATURED'
                    locked_fd.settled_at = timezone.now()
                    locked_fd.settlement_amount = payout
                    locked_fd.closure_type = 'MATURITY'
                    locked_fd.save(update_fields=['status', 'settled_at', 'settlement_amount', 'closure_type'])

                    Transaction.objects.create(
                        account=locked_account,
                        transaction_type='FD_MATURITY_CREDIT',
                        amount=payout,
                        balance_before=balance_before,
                        balance_after=balance_after,
                        reference_id=reference_id
                    )

                settled_count += 1
                total_payout += payout
                self.stdout.write(self.style.SUCCESS(
                    f"Successfully settled FD #{fd.id}: credited ₹{payout} to account {locked_account.account_number}."
                ))

            except IntegrityError:
                self.stdout.write(self.style.WARNING(f"Duplicate settlement race caught for FD #{fd.id}; safely rolled back."))
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"Error settling FD #{fd.id}: {str(e)}"))

        self.stdout.write(self.style.SUCCESS(
            f"Settlement process completed: {settled_count} FD(s) settled totaling ₹{total_payout}."
        ))

