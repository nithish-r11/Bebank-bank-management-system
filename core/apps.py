from django.contrib.admin.apps import AdminConfig


class BebankAdminConfig(AdminConfig):
    """
    Custom AdminConfig enabling BebankAdminSite as the default admin site with autodiscovery.
    """
    default_site = "core.admin_site.BebankAdminSite"
