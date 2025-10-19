from django.urls import path
from . import views

urlpatterns = [
    path("", views.index, name="index"),
    path("api/myteam", views.api_myteam, name="api_myteam"),
    path("api/data", views.api_data, name="api_data"),
    path("api/suggestions", views.api_suggestions, name="api_suggestions"),
    path("api/forecast", views.api_forecast, name="api_forecast"),
    path("api/player-summary/<int:player_id>", views.api_player_summary, name="api_player_summary"),
    path("api/pricechanges", views.api_pricechanges, name="api_pricechanges"),
    path("api/pricechanges_fpl", views.api_pricechanges_fpl, name="api_pricechanges_fpl"),
]
