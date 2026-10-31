from functools import wraps
from django.shortcuts import redirect
from django.core.exceptions import PermissionDenied
from django.contrib import messages
from django.contrib.messages.api import MessageFailure
from django.contrib.auth.views import redirect_to_login


def _is_locked_or_inactive(user):
    """Check whether a user account is explicitly locked or inactive."""
    return getattr(user, 'account_locked', False) or not getattr(user, 'is_active', True)


def _handle_locked_or_inactive(request):
    """Deny access to locked or inactive users using project account-locking semantics."""
    try:
        messages.error(request, "Account is blocked ❌")
    except MessageFailure:
        pass
    return redirect("login")


def login_required_with_lock_check(view_func):
    """
    Decorator for views that requires authentication and checks that the user
    account is neither locked nor inactive.

    - Anonymous users are redirected to the login page with the original destination preserved.
    - Inactive or locked users are redirected to the login page with an error notice.
    - Active authenticated users proceed to the view.
    """
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path(), '/login/')
        if _is_locked_or_inactive(request.user):
            return _handle_locked_or_inactive(request)
        return view_func(request, *args, **kwargs)
    return _wrapped_view


def role_required(*allowed_roles):
    """
    Decorator for views that enforces Role-Based Access Control (RBAC).
    Supports one or multiple role arguments (e.g. 'ADMIN', 'EMPLOYEE') or an iterable of roles.

    - Anonymous users are redirected to /login/?next={destination}.
    - Inactive or locked users are redirected to /login/ with an error notice.
    - Authenticated users whose role is not in allowed_roles receive PermissionDenied (HTTP 403).
    - Users with an allowed role proceed to the view.
    """
    if len(allowed_roles) == 1 and isinstance(allowed_roles[0], (list, tuple, set)):
        roles_set = set(allowed_roles[0])
    else:
        roles_set = set(allowed_roles)

    def decorator(view_func):
        @wraps(view_func)
        def _wrapped_view(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect_to_login(request.get_full_path(), '/login/')
            if _is_locked_or_inactive(request.user):
                return _handle_locked_or_inactive(request)
            if getattr(request.user, 'role', None) not in roles_set:
                raise PermissionDenied("You do not have permission to access this resource.")
            return view_func(request, *args, **kwargs)
        return _wrapped_view
    return decorator
