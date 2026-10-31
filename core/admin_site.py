from django.contrib.admin import AdminSite


class BebankAdminSite(AdminSite):
    """
    Custom Django AdminSite that enforces strict dual-key authorization:
    Access requires all three conditions:
    1. request.user.is_active is True
    2. request.user.is_staff is True
    3. request.user.role == 'ADMIN'
    """
    site_header = "Bebank Administration"
    site_title = "Bebank Admin Portal"
    index_title = "Welcome to Bebank Administration"

    def has_permission(self, request):
        """
        Return True if the user is active, not locked, is staff, and has the role 'ADMIN'.
        Non-admin, non-staff, locked, or inactive users receive False, prompting
        PermissionDenied (HTTP 403) or redirect to login.
        """
        user = getattr(request, "user", None)
        return bool(
            user
            and user.is_authenticated
            and user.is_active
            and not getattr(user, "account_locked", False)
            and user.is_staff
            and getattr(user, "role", None) == "ADMIN"
        )
