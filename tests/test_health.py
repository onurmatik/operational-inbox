import pytest
from django.urls import reverse


@pytest.mark.django_db
def test_live_health(client):
    response = client.get(reverse("health_live"))
    assert response.status_code == 200
    assert response.json() == {"status": "live"}


@pytest.mark.django_db
def test_ready_health(client):
    response = client.get(reverse("health_ready"))
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
