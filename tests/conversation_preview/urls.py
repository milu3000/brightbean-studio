from django.contrib.auth.views import LoginView
from django.urls import path

from . import views

urlpatterns = [
    path("login/", LoginView.as_view(template_name="preview_login.html"), name="preview_login"),
    path("", views.index, name="preview_index"),
    path("scenario/<slug:scenario>/", views.timeline, name="preview_timeline"),
]
