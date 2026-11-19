import threading
import uuid
from decimal import Decimal
from unittest.mock import patch
from django.db import connection
from django.test import TestCase, TransactionTestCase, Client
from accounts.models import User
from banking.models import BankAccount, Transaction


class SymmetricTransferLedgerTests(TestCase):
    """
    Test-Driven Development suite for Task 1.1: Reciprocal Two-Leg Transfer Ledger.
    Verifies balanced reciprocal ledger entries, unique reference ID sharing,
    before/after balance snapshots, locked/active account validation, and rollback safety.
    """

    def setUp(self):
        self.client_a = Client()
        self.client_b = Client()

        self.user_a = User.objects.create_user(
            username='alice', password='password123', role='CUSTOMER'
        )
        self.user_b = User.objects.create_user(
            username='bob', password='password123', role='CUSTOMER'
        )

        self.account_a = BankAccount.objects.create(
            user=self.user_a,
            account_number='10001',
            account_type='SAVINGS',
            balance=Decimal('1000.00')
        )
        self.account_b = BankAccount.objects.create(
            user=self.user_b,
            account_number='10002',
            account_type='SAVINGS',
            balance=Decimal('250.00')
        )

        self.client_a.force_login(self.user_a)
        self.client_b.force_login(self.user_b)

    def _post(self, client, data):
        """Helper to inject a fresh idempotency key if not explicitly provided."""
        payload = dict(data)
        if 'idempotency_key' not in payload:
            payload['idempotency_key'] = str(uuid.uuid4())
        return client.post('/bank/transfer/', data=payload)

    def test_successful_transfer_creates_two_balanced_records_with_shared_reference_and_snapshots(self):
        """
        A transfer of ₹400 from Alice (1000.00) to Bob (250.00) must:
        1. Debit Alice by 400 (balance -> 600.00).
        2. Credit Bob by 400 (balance -> 650.00).
        3. Create a TRANSFER_DEBIT record for Alice.
        4. Create a TRANSFER_CREDIT record for Bob.
        5. Both records share the exact same reference_id.
        6. Accurately snapshot balance_before and balance_after on both records.
        """
        key = str(uuid.uuid4())
        response = self._post(self.client_a, {
            'account_id': self.account_a.id,
            'account_number': self.account_b.account_number,
            'amount': '400.00',
            'idempotency_key': key
        })
        self.assertEqual(response.status_code, 302)

        self.account_a.refresh_from_db()
        self.account_b.refresh_from_db()

        self.assertEqual(self.account_a.balance, Decimal('600.00'))
        self.assertEqual(self.account_b.balance, Decimal('650.00'))

        txs = Transaction.objects.all().order_by('id')
        self.assertEqual(txs.count(), 2, "Exactly two transaction records must be created.")

        tx_debit = txs.filter(account=self.account_a).first()
        tx_credit = txs.filter(account=self.account_b).first()

        self.assertIsNotNone(tx_debit, "Sender debit transaction must exist.")
        self.assertIsNotNone(tx_credit, "Recipient credit transaction must exist.")

        # Verify Sender Leg (TRANSFER_DEBIT)
        self.assertEqual(tx_debit.transaction_type, 'TRANSFER_DEBIT')
        self.assertEqual(tx_debit.account, self.account_a)
        self.assertEqual(tx_debit.related_account, self.account_b)
        self.assertEqual(tx_debit.amount, Decimal('400.00'))
        self.assertEqual(tx_debit.balance_before, Decimal('1000.00'))
        self.assertEqual(tx_debit.balance_after, Decimal('600.00'))
        self.assertFalse(tx_debit.is_credit())

        # Verify Recipient Leg (TRANSFER_CREDIT)
        self.assertEqual(tx_credit.transaction_type, 'TRANSFER_CREDIT')
        self.assertEqual(tx_credit.account, self.account_b)
        self.assertEqual(tx_credit.related_account, self.account_a)
        self.assertEqual(tx_credit.amount, Decimal('400.00'))
        self.assertEqual(tx_credit.balance_before, Decimal('250.00'))
        self.assertEqual(tx_credit.balance_after, Decimal('650.00'))
        self.assertTrue(tx_credit.is_credit())

        # Verify Shared Unique Reference ID preserves full UUID entropy
        expected_ref = f"TRF-{uuid.UUID(key).hex.upper()}"
        self.assertEqual(tx_debit.reference_id, expected_ref)
        self.assertEqual(tx_credit.reference_id, expected_ref)

    def test_insufficient_funds_rejects_and_creates_no_records(self):
        """Transfer exceeding balance must be rejected without balance alteration or records."""
        response = self._post(self.client_a, {
            'account_id': self.account_a.id,
            'account_number': self.account_b.account_number,
            'amount': '1500.00'
        })
        self.assertEqual(response.status_code, 302)

        self.account_a.refresh_from_db()
        self.account_b.refresh_from_db()

        self.assertEqual(self.account_a.balance, Decimal('1000.00'))
        self.assertEqual(self.account_b.balance, Decimal('250.00'))
        self.assertEqual(Transaction.objects.count(), 0)

    def test_self_transfer_rejected_and_creates_no_records(self):
        """Transfer to the same account must be rejected."""
        response = self._post(self.client_a, {
            'account_id': self.account_a.id,
            'account_number': self.account_a.account_number,
            'amount': '100.00'
        })
        self.assertEqual(response.status_code, 302)

        self.account_a.refresh_from_db()
        self.assertEqual(self.account_a.balance, Decimal('1000.00'))
        self.assertEqual(Transaction.objects.count(), 0)

    def test_invalid_recipient_account_rejected_and_creates_no_records(self):
        """Transfer to non-existent account number must be rejected."""
        response = self._post(self.client_a, {
            'account_id': self.account_a.id,
            'account_number': '99999',
            'amount': '100.00'
        })
        self.assertEqual(response.status_code, 302)

        self.account_a.refresh_from_db()
        self.assertEqual(self.account_a.balance, Decimal('1000.00'))
        self.assertEqual(Transaction.objects.count(), 0)

    def test_locked_recipient_account_rejected_and_creates_no_records(self):
        """Transfer to a recipient whose user is locked must be rejected."""
        self.user_b.account_locked = True
        self.user_b.save()

        response = self._post(self.client_a, {
            'account_id': self.account_a.id,
            'account_number': self.account_b.account_number,
            'amount': '100.00'
        })
        self.assertEqual(response.status_code, 302)

        self.account_a.refresh_from_db()
        self.account_b.refresh_from_db()

        self.assertEqual(self.account_a.balance, Decimal('1000.00'))
        self.assertEqual(self.account_b.balance, Decimal('250.00'))
        self.assertEqual(Transaction.objects.count(), 0)

    def test_inactive_recipient_account_rejected_and_creates_no_records(self):
        """Transfer to a recipient whose user is inactive must be rejected."""
        self.user_b.is_active = False
        self.user_b.save()

        response = self._post(self.client_a, {
            'account_id': self.account_a.id,
            'account_number': self.account_b.account_number,
            'amount': '100.00'
        })
        self.assertEqual(response.status_code, 302)

        self.account_a.refresh_from_db()
        self.account_b.refresh_from_db()

        self.assertEqual(self.account_a.balance, Decimal('1000.00'))
        self.assertEqual(self.account_b.balance, Decimal('250.00'))
        self.assertEqual(Transaction.objects.count(), 0)

    def test_injected_failure_on_credit_leg_rolls_back_both_balances_and_records(self):
        """
        If an unexpected error occurs during credit leg record creation,
        the entire atomic transaction must roll back: both balances restored and 0 records created.
        """
        original_create = Transaction.objects.create
        call_count = {'count': 0}

        def mock_create_failure(*args, **kwargs):
            call_count['count'] += 1
            if call_count['count'] == 2:
                # Fail specifically on the second (credit leg) creation
                raise RuntimeError("Simulated database failure on recipient credit leg")
            return original_create(*args, **kwargs)

        with patch.object(Transaction.objects, 'create', side_effect=mock_create_failure):
            with self.assertRaises(RuntimeError):
                self._post(self.client_a, {
                    'account_id': self.account_a.id,
                    'account_number': self.account_b.account_number,
                    'amount': '300.00'
                })

        self.account_a.refresh_from_db()
        self.account_b.refresh_from_db()

        self.assertEqual(
            self.account_a.balance,
            Decimal('1000.00'),
            "Sender balance must roll back to 1000.00."
        )
        self.assertEqual(
            self.account_b.balance,
            Decimal('250.00'),
            "Recipient balance must roll back to 250.00."
        )
        self.assertEqual(
            Transaction.objects.count(),
            0,
            "No partial transaction records must remain."
        )

    def test_passbook_and_history_views_render_symmetric_transfer_records(self):
        """
        Verify that history and passbook views render both sides of the transfer properly:
        Alice sees debit (- ₹400), Bob sees credit (+ ₹400).
        """
        self._post(self.client_a, {
            'account_id': self.account_a.id,
            'account_number': self.account_b.account_number,
            'amount': '400.00'
        })

        # Alice checks history
        resp_a = self.client_a.get(f'/bank/history/{self.account_a.id}/')
        self.assertEqual(resp_a.status_code, 200)
        content_a = resp_a.content.decode()
        self.assertIn("Transfer Sent", content_a)
        self.assertIn("- ₹400.00", content_a)

        # Bob checks history
        resp_b = self.client_b.get(f'/bank/history/{self.account_b.id}/')
        self.assertEqual(resp_b.status_code, 200)
        content_b = resp_b.content.decode()
        self.assertIn("Transfer Received", content_b)
        self.assertIn("+ ₹400.00", content_b)

        # Bob checks PDF statement
        pdf_resp_b = self.client_b.get(f'/bank/history/pdf/{self.account_b.id}/')
        self.assertEqual(pdf_resp_b.status_code, 200)
        self.assertEqual(pdf_resp_b['Content-Type'], 'application/pdf')

    def test_legacy_transfer_records_remain_valid_and_readable(self):
        """
        Historical records with transaction_type='TRANSFER' and null reference_id/related_account
        must remain valid, readable, and renderable in history and PDF views.
        """
        legacy_tx = Transaction.objects.create(
            account=self.account_a,
            transaction_type='TRANSFER',
            amount=Decimal('150.00')
        )
        self.assertIsNone(legacy_tx.reference_id)
        self.assertIsNone(legacy_tx.related_account)
        self.assertIsNone(legacy_tx.balance_before)
        self.assertIsNone(legacy_tx.balance_after)

        resp = self.client_a.get(f'/bank/history/{self.account_a.id}/')
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        self.assertIn("Transfer", content)
        self.assertIn("150.00", content)


class TransferHardeningAndIdempotencyTests(TestCase):
    """
    Test suite for final financial hardening:
    - Missing & malformed idempotency key rejection
    - Reused key with identical details is an idempotent no-op (no double balance change)
    - Exactly two ledger records remain for the reference
    - Reused key with different details (recipient, amount) is strictly rejected
    - Independent keys process independent transfers
    - GET /bank/transfer/ provides a fresh UUID idempotency key in context
    """

    def setUp(self):
        self.client = Client()
        self.user_sender = User.objects.create_user(username='send_h', password='pwd', role='CUSTOMER')
        self.user_recip = User.objects.create_user(username='recip_h', password='pwd', role='CUSTOMER')
        self.user_other = User.objects.create_user(username='other_h', password='pwd', role='CUSTOMER')

        self.acc_sender = BankAccount.objects.create(
            user=self.user_sender, account_number='70001', account_type='SAVINGS', balance=Decimal('2000.00')
        )
        self.acc_recip = BankAccount.objects.create(
            user=self.user_recip, account_number='70002', account_type='SAVINGS', balance=Decimal('500.00')
        )
        self.acc_other = BankAccount.objects.create(
            user=self.user_other, account_number='70003', account_type='SAVINGS', balance=Decimal('300.00')
        )

        self.client.force_login(self.user_sender)

    def test_missing_idempotency_key_rejected(self):
        """POST without idempotency_key must be rejected without altering balances."""
        response = self.client.post('/bank/transfer/', data={
            'account_id': self.acc_sender.id,
            'account_number': self.acc_recip.account_number,
            'amount': '100.00'
        })
        self.assertEqual(response.status_code, 302)
        self.acc_sender.refresh_from_db()
        self.acc_recip.refresh_from_db()
        self.assertEqual(self.acc_sender.balance, Decimal('2000.00'))
        self.assertEqual(self.acc_recip.balance, Decimal('500.00'))
        self.assertEqual(Transaction.objects.count(), 0)

    def test_malformed_idempotency_key_rejected(self):
        """POST with non-UUID idempotency_key must be rejected."""
        response = self.client.post('/bank/transfer/', data={
            'account_id': self.acc_sender.id,
            'account_number': self.acc_recip.account_number,
            'amount': '100.00',
            'idempotency_key': 'not-a-valid-uuid-token'
        })
        self.assertEqual(response.status_code, 302)
        self.acc_sender.refresh_from_db()
        self.acc_recip.refresh_from_db()
        self.assertEqual(self.acc_sender.balance, Decimal('2000.00'))
        self.assertEqual(self.acc_recip.balance, Decimal('500.00'))
        self.assertEqual(Transaction.objects.count(), 0)

    def test_same_post_submitted_twice_does_not_change_balances_twice(self):
        """
        Submitting the identical transfer POST request twice with the same idempotency_key
        must execute exactly once. Balances must change only once, and exactly 2 records must remain.
        """
        key = str(uuid.uuid4())
        post_data = {
            'account_id': self.acc_sender.id,
            'account_number': self.acc_recip.account_number,
            'amount': '500.00',
            'idempotency_key': key
        }

        # First request
        resp1 = self.client.post('/bank/transfer/', data=post_data)
        self.assertEqual(resp1.status_code, 302)

        self.acc_sender.refresh_from_db()
        self.acc_recip.refresh_from_db()
        self.assertEqual(self.acc_sender.balance, Decimal('1500.00'))
        self.assertEqual(self.acc_recip.balance, Decimal('1000.00'))
        self.assertEqual(Transaction.objects.count(), 2)

        # Second request (retry/duplicate)
        resp2 = self.client.post('/bank/transfer/', data=post_data)
        self.assertEqual(resp2.status_code, 302)

        self.acc_sender.refresh_from_db()
        self.acc_recip.refresh_from_db()
        # Balances must NOT be debited or credited a second time!
        self.assertEqual(self.acc_sender.balance, Decimal('1500.00'), "Sender balance must not change on duplicate retry.")
        self.assertEqual(self.acc_recip.balance, Decimal('1000.00'), "Recipient balance must not change on duplicate retry.")
        self.assertEqual(Transaction.objects.count(), 2, "Exactly two ledger records must remain.")

        # Confirm the reference has exactly one debit and one credit leg
        expected_ref = f"TRF-{uuid.UUID(key).hex.upper()}"
        self.assertEqual(
            Transaction.objects.filter(reference_id=expected_ref, transaction_type='TRANSFER_DEBIT').count(),
            1
        )
        self.assertEqual(
            Transaction.objects.filter(reference_id=expected_ref, transaction_type='TRANSFER_CREDIT').count(),
            1
        )

    def test_reusing_key_with_different_amount_rejected(self):
        """Reusing an idempotency key with a different amount must be rejected."""
        key = str(uuid.uuid4())
        self.client.post('/bank/transfer/', data={
            'account_id': self.acc_sender.id,
            'account_number': self.acc_recip.account_number,
            'amount': '300.00',
            'idempotency_key': key
        })
        self.acc_sender.refresh_from_db()
        self.acc_recip.refresh_from_db()
        self.assertEqual(self.acc_sender.balance, Decimal('1700.00'))
        self.assertEqual(self.acc_recip.balance, Decimal('800.00'))
        self.assertEqual(Transaction.objects.count(), 2)

        # Attempt to reuse the key with different amount
        resp_conflict = self.client.post('/bank/transfer/', data={
            'account_id': self.acc_sender.id,
            'account_number': self.acc_recip.account_number,
            'amount': '999.00',
            'idempotency_key': key
        })
        self.assertEqual(resp_conflict.status_code, 302)

        self.acc_sender.refresh_from_db()
        self.acc_recip.refresh_from_db()
        self.assertEqual(self.acc_sender.balance, Decimal('1700.00'))
        self.assertEqual(self.acc_recip.balance, Decimal('800.00'))
        self.assertEqual(Transaction.objects.count(), 2)

    def test_reusing_key_with_different_recipient_rejected(self):
        """Reusing an idempotency key with a different recipient must be rejected."""
        key = str(uuid.uuid4())
        self.client.post('/bank/transfer/', data={
            'account_id': self.acc_sender.id,
            'account_number': self.acc_recip.account_number,
            'amount': '200.00',
            'idempotency_key': key
        })
        self.acc_sender.refresh_from_db()
        self.assertEqual(self.acc_sender.balance, Decimal('1800.00'))
        self.assertEqual(Transaction.objects.count(), 2)

        # Attempt to reuse the key to a different recipient
        resp_conflict = self.client.post('/bank/transfer/', data={
            'account_id': self.acc_sender.id,
            'account_number': self.acc_other.account_number,
            'amount': '200.00',
            'idempotency_key': key
        })
        self.assertEqual(resp_conflict.status_code, 302)

        self.acc_sender.refresh_from_db()
        self.acc_other.refresh_from_db()
        self.assertEqual(self.acc_sender.balance, Decimal('1800.00'))
        self.assertEqual(self.acc_other.balance, Decimal('300.00'))
        self.assertEqual(Transaction.objects.count(), 2)

    def test_different_valid_keys_process_independent_transfers(self):
        """Two requests with different valid UUID keys execute independently."""
        key1 = str(uuid.uuid4())
        key2 = str(uuid.uuid4())

        resp1 = self.client.post('/bank/transfer/', data={
            'account_id': self.acc_sender.id,
            'account_number': self.acc_recip.account_number,
            'amount': '100.00',
            'idempotency_key': key1
        })
        self.assertEqual(resp1.status_code, 302)

        resp2 = self.client.post('/bank/transfer/', data={
            'account_id': self.acc_sender.id,
            'account_number': self.acc_recip.account_number,
            'amount': '200.00',
            'idempotency_key': key2
        })
        self.assertEqual(resp2.status_code, 302)

        self.acc_sender.refresh_from_db()
        self.acc_recip.refresh_from_db()
        self.assertEqual(self.acc_sender.balance, Decimal('1700.00'))
        self.assertEqual(self.acc_recip.balance, Decimal('800.00'))
        self.assertEqual(Transaction.objects.count(), 4)

    def test_transfer_form_get_renders_fresh_idempotency_key(self):
        """GET /bank/transfer/ must provide a valid UUID idempotency_key in context and rendered HTML."""
        response = self.client.get('/bank/transfer/')
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn('name="idempotency_key"', content)
        key_in_context = response.context.get('idempotency_key')
        self.assertIsNotNone(key_in_context)
        # Verify it parses as a UUID
        uuid.UUID(str(key_in_context))


class ConcurrentTransferIdempotencyTests(TransactionTestCase):
    """
    Concurrency test verifying that simultaneous duplicate transfer POSTs
    sharing the same idempotency key are resolved safely through database row locks
    and the composite unique constraint, resulting in exactly one transfer.
    """

    def setUp(self):
        if connection.vendor != 'postgresql':
            self.skipTest(
                "Row-level locking concurrency verification requires PostgreSQL. "
                "SQLite does not support row-level locking (select_for_update)."
            )
        self.user_sender = User.objects.create_user(username='conc_s', password='pwd', role='CUSTOMER')
        self.user_recip = User.objects.create_user(username='conc_r', password='pwd', role='CUSTOMER')

        self.acc_sender = BankAccount.objects.create(
            user=self.user_sender, account_number='90001', account_type='SAVINGS', balance=Decimal('1000.00')
        )
        self.acc_recip = BankAccount.objects.create(
            user=self.user_recip, account_number='90002', account_type='SAVINGS', balance=Decimal('200.00')
        )

    def test_concurrent_duplicate_submissions_safely_result_in_one_transfer(self):
        """Two concurrent threads submitting the exact same idempotency_key simultaneously."""
        key = str(uuid.uuid4())
        barrier = threading.Barrier(2)
        errors = []
        status_codes = []

        def execute():
            from django.db import connection as thread_conn
            thread_conn.close()
            client = Client()
            client.force_login(self.user_sender)
            try:
                barrier.wait(timeout=5)
                resp = client.post('/bank/transfer/', data={
                    'account_id': self.acc_sender.id,
                    'account_number': self.acc_recip.account_number,
                    'amount': '300.00',
                    'idempotency_key': key
                })
                status_codes.append(resp.status_code)
            except Exception as e:
                errors.append(e)
            finally:
                thread_conn.close()

        t1 = threading.Thread(target=execute)
        t2 = threading.Thread(target=execute)

        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(len(errors), 0, f"Unexpected thread errors: {errors}")
        self.assertEqual(len(status_codes), 2)

        self.acc_sender.refresh_from_db()
        self.acc_recip.refresh_from_db()

        self.assertEqual(self.acc_sender.balance, Decimal('700.00'), "Sender balance must change only once.")
        self.assertEqual(self.acc_recip.balance, Decimal('500.00'), "Recipient balance must change only once.")
        self.assertEqual(Transaction.objects.count(), 2, "Exactly two ledger records must exist.")
