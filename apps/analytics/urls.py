"""URLs for the analytics page."""

from django.urls import path

from . import status_views, views

app_name = "analytics"

urlpatterns = [
    path("post/<uuid:post_id>/status/", status_views.confirm_post_status, name="confirm_post_status"),
    path("", views.analytics_index, name="index"),
    path("post/<uuid:post_id>/", views.post_detail, name="post_detail"),
    path("<uuid:account_id>/", views.analytics_account, name="account"),
]
