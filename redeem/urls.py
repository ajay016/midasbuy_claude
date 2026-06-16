from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="index"),
    path("docs/", views.docs_view, name="api_docs"),
    path("bulk/", views.bulk_page, name="bulk_page"),
    path("login/", views.login_view, name="panel_login"),
    path("logout/", views.logout_view, name="panel_logout"),

    # Team management (admin only)
    path("team/", views.team_list, name="team_list"),
    path("team/add/", views.team_add, name="team_add"),
    path("team/<int:pk>/", views.team_edit, name="team_edit"),
    path("team/<int:pk>/delete/", views.team_delete, name="team_delete"),
    path("team/<int:pk>/keys/new/", views.team_apikey_create, name="team_apikey_create"),
    path("team/<int:pk>/keys/<str:key_id>/revoke/", views.team_apikey_revoke,
         name="team_apikey_revoke"),
    path("team/<int:pk>/sub/<str:plan>/set/", views.team_subscription_set,
         name="team_subscription_set"),
    path("team/<int:pk>/sub/<str:plan>/revoke/", views.team_subscription_revoke,
         name="team_subscription_revoke"),

    # Subscription packages (admin only)
    path("subscriptions/", views.package_list, name="package_list"),
    path("subscriptions/new/", views.package_create, name="package_create"),
    path("subscriptions/<int:pk>/edit/", views.package_edit, name="package_edit"),
    path("subscriptions/<int:pk>/delete/", views.package_delete, name="package_delete"),

    # Usage overview (admin only)
    path("usage/", views.usage_overview, name="usage_overview"),

    # Self-service API keys
    path("api-keys/", views.my_api_keys, name="my_api_keys"),
    path("api-keys/new/", views.my_apikey_create, name="my_apikey_create"),
    path("api-keys/<str:key_id>/revoke/", views.my_apikey_revoke, name="my_apikey_revoke"),
]
