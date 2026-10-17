from django.urls import path

from . import views


urlpatterns = [
    path("", views.index, name="index"),
    path("api/assignments/analyze/", views.analyze_assignment, name="analyze_assignment"),
    path("api/resources/search/", views.search_resources, name="search_resources"),
    path("api/tutor/chat/", views.chat, name="tutor_chat"),
    path("api/tutor/check/", views.check_work, name="check_work"),
]
