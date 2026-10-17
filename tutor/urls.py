from django.urls import path

from . import views


urlpatterns = [
    path("", views.index, name="index"),
    path("api/learning/reset/", views.reset_learning_session, name="reset_learning_session"),
    path("api/assignments/analyze/", views.analyze_assignment, name="analyze_assignment"),
    path("api/learning/session/", views.learning_session, name="learning_session"),
    path("api/learning/validate/", views.validate_step, name="validate_step"),
    path("api/learning/hint/", views.request_step_hint, name="request_step_hint"),
    path("api/resources/search/", views.search_resources, name="search_resources"),
    path("api/tutor/chat/", views.chat, name="tutor_chat"),
    path("api/tutor/check/", views.check_work, name="check_work"),
]
