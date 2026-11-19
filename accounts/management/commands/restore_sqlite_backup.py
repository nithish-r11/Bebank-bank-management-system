import os
import sys
import sqlite3
import hashlib
from django.core.management.base import BaseCommand, CommandError
from django.conf import settings
from django.utils import timezone


class Command(BaseCommand):
    help = (
        "Safely restores the local SQLite database from a verified backup snapshot. "
        "Performs pre-flight integrity check, creates an automatic pre-restore safety snapshot, "
        "and verifies post-restore integrity. Requires --confirm flag."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            'backup_path',
            type=str,
            help='Absolute or relative path to the verified .sqlite3 backup file.'
        )
        parser.add_argument(
            '--confirm',
            action='store_true',
            help='Explicit confirmation flag to proceed with database restore.'
        )

    def handle(self, *args, **options):
        backup_path = options['backup_path']
        confirm = options['confirm']

        self.stdout.write(self.style.MIGRATE_HEADING("=== BEBANK SQLite Database Safe Recovery Console ==="))

        # 1. Target Database Engine Verification
        default_db = settings.DATABASES.get('default', {})
        engine = default_db.get('ENGINE', '')
        if 'sqlite' not in engine.lower():
            raise CommandError(
                f"Safety Check Failed: Configured default database engine '{engine}' is not SQLite. "
                "This recovery utility only operates on local SQLite databases."
            )

        target_path = str(default_db.get('NAME', ''))
        if not target_path or target_path == ':memory:':
            raise CommandError("Safety Check Failed: Target database is in-memory or not a valid disk path.")

        # Resolve paths
        target_path = os.path.abspath(target_path)
        if not os.path.isabs(backup_path):
            backup_path = os.path.abspath(backup_path)

        # 2. Source Backup File Existence
        if not os.path.exists(backup_path) or not os.path.isfile(backup_path):
            raise CommandError(f"Safety Check Failed: Backup file '{backup_path}' does not exist or is not a regular file.")

        # 3. Source Integrity & Header Check
        self.stdout.write(f"Inspecting source backup: {backup_path}")
        try:
            with open(backup_path, 'rb') as f:
                header = f.read(16)
                if not header.startswith(b'SQLite format 3\x00'):
                    raise CommandError("Safety Check Failed: File header is not a valid SQLite3 database.")
        except IOError as e:
            raise CommandError(f"Failed to read backup file: {e}")

        try:
            src_conn = sqlite3.connect(backup_path)
            cursor = src_conn.cursor()
            cursor.execute("PRAGMA integrity_check;")
            integrity_result = cursor.fetchone()[0]
            src_conn.close()
            if integrity_result != "ok":
                raise CommandError(f"Safety Check Failed: Backup failed SQLite integrity check: {integrity_result}")
        except Exception as e:
            raise CommandError(f"Safety Check Failed during SQLite integrity verification: {e}")

        # Compute backup checksum & size
        with open(backup_path, 'rb') as f:
            backup_sha256 = hashlib.sha256(f.read()).hexdigest()
        backup_size = os.path.getsize(backup_path)

        self.stdout.write(self.style.SUCCESS(f"✓ Source Backup Verified: Size={backup_size} bytes, Checksum={backup_sha256[:12]}..., Integrity=ok"))
        self.stdout.write(f"Target Database: {target_path}")

        # 4. Mandatory Confirmation
        if not confirm:
            self.stdout.write(self.style.WARNING(
                "\n[DRY-RUN] SAFETY NOTICE:\n"
                "1. Ensure the BEBANK web application server is STOPPED before executing restore.\n"
                "2. Restoring will replace all current database records with the backup snapshot.\n"
                "3. To proceed, run this command with the --confirm flag (Pass --confirm to execute):\n"
                f"   python manage.py restore_sqlite_backup \"{backup_path}\" --confirm\n"
            ))
            return

        # 5. Create Pre-Restore Safety Snapshot of Current Database
        backups_dir = os.path.join(settings.BASE_DIR, 'backups')
        os.makedirs(backups_dir, exist_ok=True)
        safety_filename = f"bebank_pre_restore_safety_{timezone.now().strftime('%Y%m%d_%H%M%S')}.sqlite3"
        safety_path = os.path.join(backups_dir, safety_filename)

        if os.path.exists(target_path):
            self.stdout.write(f"Creating pre-restore safety snapshot at: {safety_path}")
            try:
                curr_conn = sqlite3.connect(target_path)
                safe_conn = sqlite3.connect(safety_path)
                curr_conn.backup(safe_conn)
                safe_conn.close()
                curr_conn.close()
                self.stdout.write(self.style.SUCCESS("✓ Pre-restore safety snapshot created successfully."))
            except Exception as e:
                raise CommandError(f"Failed to create pre-restore safety snapshot: {e}")

        # 6. Execute Transactional SQLite Online Restore
        self.stdout.write("Restoring database from verified backup...")
        try:
            restore_src_conn = sqlite3.connect(backup_path)
            restore_target_conn = sqlite3.connect(target_path)
            restore_src_conn.backup(restore_target_conn)
            restore_target_conn.close()
            restore_src_conn.close()
        except Exception as e:
            raise CommandError(f"Database restore failed: {e}")

        # 7. Post-Restore Integrity Verification
        try:
            post_conn = sqlite3.connect(target_path)
            cursor = post_conn.cursor()
            cursor.execute("PRAGMA integrity_check;")
            post_integrity = cursor.fetchone()[0]
            post_conn.close()
            if post_integrity != "ok":
                raise CommandError(f"CRITICAL: Post-restore integrity check failed: {post_integrity}")
        except Exception as e:
            raise CommandError(f"Post-restore verification error: {e}")

        self.stdout.write(self.style.SUCCESS(
            f"\n✓ DATABASE RESTORE COMPLETED SUCCESSFULLY!\n"
            f"Target database '{target_path}' is verified intact.\n"
            f"Pre-restore safety snapshot preserved at: {safety_path}\n"
            "You may now safely restart the BEBANK web application server.\n"
        ))

