from django.urls import include, path


urlpatterns = [
    path("", include("tutor.urls")),
]
