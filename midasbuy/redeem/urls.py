from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="index"),
    path("login/", views.login_view, name="panel_login"),
    path("logout/", views.logout_view, name="panel_logout"),
]
