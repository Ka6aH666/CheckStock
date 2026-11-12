from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import services
from app.main import app
from app.models import Balance, Event, Operation
from app.seed import seed


def payload(kind="fbs", quantity=3, **overrides):
    data = {"request_key": str(uuid4()), "kind": kind, "product_id": 1, "quantity": quantity}
    if kind == "receipt":
        data["destination_id"] = 1
    else:
        data["source_id"] = 1
        if kind == "transfer":
            data["destination_id"] = 2
        else:
            data["target"] = "WB / заказ-001" if kind == "fbs" else "WB / Коледино"
    return {**data, **overrides}


def create(client, **kwargs):
    response = client.post("/api/operations", json=payload(**kwargs))
    assert response.status_code == 200, response.text
    return response.json()["id"]


def act(client, op_id, action, expected=200):
    response = client.post(f"/api/operations/{op_id}/{action}")
    assert response.status_code == expected, response.text
    return response.json()


def item(client, index=0):
    response = client.get("/api/stock")
    assert response.status_code == 200, response.text
    return response.json()[index]


def counts(client):
    return len(client.get("/api/operations").json()), len(client.get("/api/history").json())


def assert_conservation(client):
    for p in client.get("/api/stock").json():
        assert p["totals"]["receipts"] == p["totals"]["accounted"]
        for w in p["warehouses"]:
            assert w["physical"] >= w["reserved"] >= 0
            assert w["available"] == w["physical"] - w["reserved"]


def parallel(*tasks):
    barrier = Barrier(len(tasks))

    def worker(task):
        barrier.wait(timeout=10)
        with TestClient(app) as c:
            return task(c)

    with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
        return list(pool.map(worker, tasks))


def test_seed_and_http_entrypoints(client):
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/docs").status_code == 200
    assert len(client.get("/api/products").json()) == 3
    assert len(client.get("/api/warehouses").json()) == 2
    assert [
        (p["sku"], [w["physical"] for w in p["warehouses"]])
        for p in client.get("/api/stock").json()
    ] == [("SKU-101", [10, 6]), ("SKU-102", [5, 0]), ("SKU-103", [0, 4])]
    assert_conservation(client)


def test_receipt_and_retry(client):
    data = payload("receipt", 2)
    first = client.post("/api/operations", json=data)
    before = counts(client)
    assert client.post("/api/operations", json=data).json()["id"] == first.json()["id"]
    assert item(client)["warehouses"][0]["physical"] == 12
    assert counts(client) == before
    data["quantity"] = 3
    assert client.post("/api/operations", json=data).status_code == 409
    assert counts(client) == before
    assert_conservation(client)


def test_fbs_cancel_and_retry(client):
    op = create(client)
    w = item(client)["warehouses"][0]
    assert (w["physical"], w["reserved"], w["available"]) == (10, 3, 7)
    act(client, op, "cancel")
    before = counts(client)
    act(client, op, "cancel")
    assert counts(client) == before
    assert item(client)["warehouses"][0]["available"] == 10
    act(client, op, "ship", 409)
    assert_conservation(client)


def test_fbs_ship_deliver_and_retries(client):
    op = create(client)
    act(client, op, "ship")
    w = item(client)["warehouses"][0]
    assert (w["physical"], w["reserved"], w["available"]) == (7, 0, 7)
    assert item(client)["totals"]["fbs_transit"] == 3
    before = counts(client)
    act(client, op, "ship")
    assert counts(client) == before
    act(client, op, "cancel", 409)
    act(client, op, "deliver")
    before = counts(client)
    act(client, op, "deliver")
    act(client, op, "ship")
    assert counts(client) == before
    assert item(client)["totals"]["fbs_transit"] == 0
    assert item(client)["totals"]["fbs_delivered"] == 3
    assert_conservation(client)


def test_transfer_in_transit_and_receive_retry(client):
    op = create(client, kind="transfer", quantity=4)
    p = item(client)
    assert [w["physical"] for w in p["warehouses"]] == [6, 6]
    assert p["totals"]["transfer_transit"] == 4
    assert p["totals"]["available"] == 12
    act(client, op, "receive")
    before = counts(client)
    act(client, op, "receive")
    assert counts(client) == before
    assert [w["physical"] for w in item(client)["warehouses"]] == [6, 10]
    assert item(client)["totals"]["transfer_transit"] == 0
    assert_conservation(client)


def test_fbo_received_stays_separate(client):
    op = create(client, kind="fbo", quantity=2)
    assert item(client)["totals"]["fbo_transit"] == 2
    act(client, op, "receive")
    before = counts(client)
    act(client, op, "receive")
    assert counts(client) == before
    p = item(client)
    assert p["totals"]["fbo_transit"] == 0
    assert p["totals"]["fbo_received"] == 2
    assert p["totals"]["physical"] == 14
    assert_conservation(client)


@pytest.mark.parametrize("kind", ["fbs", "transfer", "fbo"])
def test_shortage_on_selected_warehouse_is_atomic(client, kind):
    before = client.get("/api/stock").json(), counts(client)
    response = client.post("/api/operations", json=payload(kind, 12))
    assert response.status_code == 409
    assert response.json()["detail"]["available"] == 10
    assert sum(w["available"] for w in response.json()["detail"]["warehouses"]) == 16
    assert (client.get("/api/stock").json(), counts(client)) == before


@pytest.mark.parametrize("kind", ["fbs", "transfer", "fbo"])
def test_reserved_stock_cannot_be_used(client, kind):
    create(client, quantity=8)
    response = client.post("/api/operations", json=payload(kind, 3))
    assert response.status_code == 409
    assert item(client)["warehouses"][0]["reserved"] == 8
    assert_conservation(client)


def test_archive_preserves_stock_and_history_and_is_repeatable(client):
    before = item(client)["totals"]
    assert client.delete("/api/warehouses/1").status_code == 200
    count = counts(client)
    assert client.delete("/api/warehouses/1").status_code == 200
    assert counts(client) == count
    assert item(client)["totals"] == before
    assert item(client)["warehouses"][0]["archived"]
    assert client.post("/api/operations", json=payload("receipt")).status_code == 409
    assert client.post("/api/operations", json=payload()).status_code == 409
    assert (
        client.post(
            "/api/operations", json=payload("transfer", source_id=2, destination_id=1)
        ).status_code
        == 409
    )
    op = create(client, kind="transfer", quantity=2)
    act(client, op, "receive")
    create(client, kind="fbo", quantity=1)
    assert item(client)["warehouses"][0]["physical"] == 7
    assert_conservation(client)


def test_archive_allows_preexisting_reservations_and_incoming_transfer(client):
    reserved = create(client, quantity=3)
    cancelled = create(client, quantity=2)
    incoming = create(client, kind="transfer", source_id=2, destination_id=1, quantity=1)
    client.delete("/api/warehouses/1")
    act(client, reserved, "ship")
    act(client, cancelled, "cancel")
    act(client, incoming, "receive")
    w = item(client)["warehouses"][0]
    assert (w["physical"], w["reserved"], w["available"]) == (8, 0, 8)
    assert_conservation(client)


def test_warehouse_management_and_historical_names(client):
    new = client.post("/api/warehouses", json={"name": " Запад "})
    assert new.status_code == 201
    assert new.json()["name"] == "Запад"
    assert client.post("/api/warehouses", json={"name": "Запад"}).status_code == 409
    assert client.post("/api/warehouses", json={"name": "   "}).status_code == 422
    assert client.patch("/api/warehouses/1", json={"name": "Юг"}).status_code == 409
    assert client.patch("/api/warehouses/1", json={"name": "Северный"}).status_code == 200
    assert item(client)["warehouses"][0]["name"] == "Северный"
    history = client.get("/api/history", params={"warehouse_id": 1, "product_id": 1}).json()
    assert len(history) == 1
    assert history[0]["destination"] == "Север"
    assert len(client.get("/api/history", params={"warehouse_id": 3}).json()) == 1


@pytest.mark.parametrize(
    "updates",
    [
        {"quantity": 0},
        {"quantity": -1},
        {"quantity": 1.5},
        {"quantity": True},
        {"quantity": "2"},
        {"target": " "},
        {"product_id": 0},
        {"request_key": "invalid"},
        {"kind": "unknown"},
        {"unknown_field": 1},
    ],
)
def test_invalid_input(client, updates):
    before = counts(client)
    assert client.post("/api/operations", json=payload(**updates)).status_code == 422
    assert counts(client) == before


def test_invalid_routes_and_missing_objects(client):
    assert (
        client.post("/api/operations", json=payload("transfer", destination_id=1)).status_code
        == 422
    )
    assert client.post("/api/operations", json=payload("receipt", source_id=1)).status_code == 422
    assert client.post("/api/operations", json=payload("fbo", destination_id=2)).status_code == 422
    assert client.post("/api/operations", json=payload(product_id=999)).status_code == 404
    assert client.post("/api/operations", json=payload(source_id=999)).status_code == 404
    assert client.delete("/api/warehouses/999").status_code == 404
    assert client.patch("/api/warehouses/999", json={"name": "Нет"}).status_code == 404
    act(client, 999, "receive", 404)
    op = create(client)
    act(client, op, "receive", 409)
    act(client, op, "deliver", 409)


def test_concurrent_reservations_do_not_oversell(client):
    a, b = payload(quantity=8), payload(quantity=8)
    responses = parallel(
        lambda c: c.post("/api/operations", json=a), lambda c: c.post("/api/operations", json=b)
    )
    assert sorted(r.status_code for r in responses) == [200, 409]
    assert item(client)["warehouses"][0]["reserved"] == 8
    assert_conservation(client)


def test_concurrent_identical_creations_are_one_operation(client):
    data = payload("receipt", 4)
    before = counts(client)
    responses = parallel(*(lambda c: c.post("/api/operations", json=data) for _ in range(4)))
    assert all(r.status_code == 200 for r in responses)
    assert len({r.json()["id"] for r in responses}) == 1
    assert counts(client) == (before[0] + 1, before[1] + 1)
    assert item(client)["warehouses"][0]["physical"] == 14
    assert_conservation(client)


@pytest.mark.parametrize(
    "kind,action", [("fbs", "ship"), ("transfer", "receive"), ("fbo", "receive")]
)
def test_concurrent_confirmations_are_one_event(client, kind, action):
    op = create(client, kind=kind)
    before = counts(client)
    responses = parallel(*(lambda c: c.post(f"/api/operations/{op}/{action}") for _ in range(4)))
    assert all(r.status_code == 200 for r in responses)
    assert counts(client) == (before[0], before[1] + 1)
    assert_conservation(client)


def test_concurrent_receipts_create_one_previously_missing_balance(client, session_factory):
    data_a = payload("receipt", 2, product_id=2, destination_id=2)
    data_b = payload("receipt", 3, product_id=2, destination_id=2)
    responses = parallel(
        lambda c: c.post("/api/operations", json=data_a),
        lambda c: c.post("/api/operations", json=data_b),
    )
    assert all(r.status_code == 200 for r in responses)
    assert item(client, 1)["warehouses"][1]["physical"] == 5
    with session_factory() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(Balance)
                .where(Balance.product_id == 2, Balance.warehouse_id == 2)
            )
            == 1
        )
    assert_conservation(client)


def test_concurrent_ship_and_cancel_have_single_winner(client):
    op = create(client, quantity=4)
    responses = parallel(
        lambda c: c.post(f"/api/operations/{op}/ship"),
        lambda c: c.post(f"/api/operations/{op}/cancel"),
    )
    assert sorted(r.status_code for r in responses) == [200, 409]
    assert item(client)["warehouses"][0]["reserved"] == 0
    assert_conservation(client)


def test_failure_rolls_back_operation_balance_and_event(client, monkeypatch):
    before = client.get("/api/stock").json(), counts(client)

    def broken_record(*args, **kwargs):
        args[0].flush()  # force SQL before simulating a later error
        raise RuntimeError("simulated audit failure")

    monkeypatch.setattr(services, "record", broken_record)
    with TestClient(app, raise_server_exceptions=False) as c:
        assert c.post("/api/operations", json=payload("receipt", 2)).status_code == 500
    assert (client.get("/api/stock").json(), counts(client)) == before


def test_seed_is_repeatable_after_rename_archive_and_usage(client, session_factory):
    create(client, quantity=4)
    client.patch("/api/warehouses/1", json={"name": "Переименованный"})
    client.delete("/api/warehouses/2")
    before = client.get("/api/dashboard").json(), counts(client)
    with session_factory.begin() as db:
        assert seed(db) is False
    assert (client.get("/api/dashboard").json(), counts(client)) == before


def test_journal_replays_exact_balance(client, session_factory):
    fbs = create(client, quantity=2)
    act(client, fbs, "ship")
    act(client, fbs, "deliver")
    transfer = create(client, kind="transfer", quantity=3)
    act(client, transfer, "receive")
    fbo = create(client, kind="fbo", quantity=1, source_id=2)
    act(client, fbo, "receive")
    create(client, kind="receipt", quantity=2)
    reserved = create(client, quantity=1)
    act(client, reserved, "cancel")
    with session_factory() as db:
        expected = {}
        for event in db.scalars(select(Event).order_by(Event.id)):
            for change in event.details.get("changes", []):
                key = event.product_id, change["warehouse_id"]
                physical, reserve = expected.get(key, (0, 0))
                expected[key] = (
                    physical + change["physical_delta"],
                    reserve + change["reserved_delta"],
                )
                assert expected[key] == (change["physical_after"], change["reserved_after"])
        actual = {
            (b.product_id, b.warehouse_id): (b.physical, b.reserved)
            for b in db.scalars(select(Balance))
        }
        assert expected == actual
        assert db.scalar(select(func.count()).select_from(Operation)) == 9
    assert_conservation(client)
