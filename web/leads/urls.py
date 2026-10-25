from django.urls import path

from web.leads import views

urlpatterns = [
    path("", views.board, name="board"),
    path("reviews/", views.review, name="review"),
]
