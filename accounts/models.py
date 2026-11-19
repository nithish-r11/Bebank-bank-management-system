from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):

    ROLE_CHOICES = (
        ('CUSTOMER', 'Customer'),
        ('EMPLOYEE', 'Employee'),
        ('ADMIN', 'Admin'),
    )

    role = models.CharField(max_length=20, choices=ROLE_CHOICES)
    account_locked = models.BooleanField(default=False)

    # ⭐ login security fields
    failed_login_attempts = models.IntegerField(default=0)

    # ⭐ KYC + profile fields
    KYC_STATUS_CHOICES = (
        ('NOT_SUBMITTED', 'Not Submitted'),
        ('PENDING', 'Pending Verification'),
        ('UNDER_REVIEW', 'Under Review'),
        ('APPROVED', 'Approved'),
        ('REJECTED', 'Rejected'),
    )

    SIMULATED_DOC_CHOICES = (
        ('SIMULATED_PASSPORT', 'Simulated Demo Passport'),
        ('SIMULATED_DRIVING_LICENSE', 'Simulated Demo Driver License'),
        ('SIMULATED_NATIONAL_ID', 'Simulated Demo National ID Card'),
        ('SIMULATED_VOTER_CARD', 'Simulated Demo Voter ID'),
    )

    phone = models.CharField(max_length=15, blank=True, null=True)
    aadhaar = models.CharField(max_length=20, blank=True, null=True)
    pan = models.CharField(max_length=20, blank=True, null=True)
    address = models.TextField(blank=True, null=True)
    pincode = models.CharField(max_length=10, blank=True, null=True)
    kyc_verified = models.BooleanField(default=False)

    kyc_status = models.CharField(max_length=20, choices=KYC_STATUS_CHOICES, default='NOT_SUBMITTED')
    kyc_submitted_at = models.DateTimeField(null=True, blank=True)
    kyc_reviewed_at = models.DateTimeField(null=True, blank=True)
    kyc_reviewed_by = models.ForeignKey(
        'self',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='reviewed_kycs'
    )
    kyc_rejection_reason = models.TextField(blank=True, default='')
    simulated_id_type = models.CharField(max_length=50, blank=True, choices=SIMULATED_DOC_CHOICES, default='')
    simulated_id_number = models.CharField(max_length=50, blank=True, default='')

    # ⭐ Employee-controlled profile update audit fields
    profile_updated_at = models.DateTimeField(null=True, blank=True)
    profile_updated_by = models.ForeignKey(
        'self',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='updated_profiles'
    )

    def get_profile_completeness(self):
        """Calculate customer profile completeness percentage (0-100%)."""
        score = 0
        if self.first_name or self.last_name:
            score += 15
        if self.email:
            score += 15
        if self.phone:
            score += 15
        if self.address:
            score += 15
        if self.pincode:
            score += 15
        if self.kyc_status == 'APPROVED':
            score += 25
        elif self.kyc_status in ('PENDING', 'UNDER_REVIEW'):
            score += 15
        return min(score, 100)


class ProfileAuditLog(models.Model):
    """Audit log capturing staff updates to customer personal/contact profiles."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='profile_audit_logs')
    updated_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='performed_profile_audits')
    updated_at = models.DateTimeField(auto_now_add=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    changes = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        updater = self.updated_by.username if self.updated_by else "System"
        return f"ProfileAuditLog for {self.user.username} by {updater} at {self.updated_at}"
