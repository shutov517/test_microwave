import pytest
from fastapi.testclient import TestClient
from .main import app, get_redis_connection, generate_token, USER_DATA


@pytest.fixture
def client():
    with TestClient(app) as client:
        yield client


@pytest.fixture
def authenticated_headers():
    headers = {"Authorization": f"Bearer {generate_token('testuser')}"}
    return headers


@pytest.fixture(autouse=True)
async def redis():
    redis = get_redis_connection()
    await redis.flushdb()
    yield redis


@pytest.mark.asyncio
async def test_get_state(client):
    response = client.get("/microwave")
    assert response.status_code == 200
    assert response.json() == {"power": 0, "counter": 0, 'state': 'OFF'}


@pytest.mark.asyncio
async def test_increase_power(client):
    response = client.post("/microwave/power/increase")
    assert response.status_code == 200
    assert response.json() == {"power": 10, "counter": 0, 'state': 'ON'}
    response = client.post("/microwave/power/increase")
    assert response.status_code == 200
    assert response.json() == {"power": 20, "counter": 0, 'state': 'ON'}


@pytest.mark.asyncio
async def test_decrease_power(client, redis):
    await redis.set("power", 20)
    response = client.post("/microwave/power/decrease")
    assert response.status_code == 200
    assert response.json() == {"power": 10, "counter": 0, 'state': 'ON'}  # -10
    response = client.post("/microwave/power/decrease")
    assert response.status_code == 200
    assert response.json() == {"power": 0, "counter": 0, 'state': 'OFF'}  # -10 again
    response = client.post("/microwave/power/decrease")
    assert response.status_code == 200
    assert response.json() == {"power": 0, "counter": 0, 'state': 'OFF'}  # same, we cant get power < 0


@pytest.mark.asyncio
async def test_counter(client):
    # increasing
    response = client.post("/microwave/counter/increase")
    assert response.status_code == 200
    assert 9 <= response.json().get("counter", 0) <= 10  # not "== 10" because time is going
    response = client.post("/microwave/counter/increase")
    assert response.status_code == 200
    assert 19 <= response.json().get("counter", 0) <= 20

    # decreasing
    response = client.post("/microwave/counter/decrease")
    assert response.status_code == 200
    assert 9 <= response.json().get("counter", 0) <= 10

    # time cant be less than a 0
    client.post("/microwave/counter/decrease")
    response = client.post("/microwave/counter/decrease")
    assert response.json().get("counter", 0) == 0


@pytest.mark.asyncio
async def test_cancel_microwave(client, authenticated_headers):
    response = client.post("/microwave/cancel")
    assert response.status_code == 401  # no token

    client.post("/microwave/counter/increase")
    client.post("/microwave/power/increase")
    response = client.post("/microwave/cancel", headers=authenticated_headers)
    assert response.status_code == 200
    assert response.json() == {"power": 0, "counter": 0, 'state': 'OFF'}


def test_login(client):
    # now it test only that reviewer of this code can obtain a jwt-token
    response = client.post("/login", data={'username': 'testuser', 'password': 'testpassword'})
    assert response.status_code == 200
    assert "access_token" in response.json()
