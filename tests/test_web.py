import os

import django
from django.test import Client

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "web.config.settings")
django.setup()


def test_board_requires_sign_in():
    response = Client().get("/")
    assert response.status_code == 302
    assert "/login/" in response.url
