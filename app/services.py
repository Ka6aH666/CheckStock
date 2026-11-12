"""All inventory writes pass through this module, in one transaction per request.

Warehouse rows are the mutexes even when a balance does not exist yet. Lock order:
request advisory lock (creation only), warehouses sorted by ID, then operation.
Archive and rename follow the same warehouse lock protocol.
"""

import hashlib
import json

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import Balance, Event, Operation, Product, Warehouse
from app.schemas import OperationInput


def fail(message, code=409, **details):
    raise HTTPException(code, detail={"message": message, **details})


def lock_warehouses(db: Session, ids):
    result = {}
    for wid in sorted(set(i for i in ids if i is not None)):
        warehouse = db.scalar(select(Warehouse).where(Warehouse.id == wid).with_for_update())
        if warehouse is None:
            fail("Склад не найден", 404)
        result[wid] = warehouse
    return result


def balance(db, product_id, warehouse_id):
    row = db.scalar(
        select(Balance).where(
            Balance.product_id == product_id, Balance.warehouse_id == warehouse_id
        )
    )
    if row is None:
        row = Balance(product_id=product_id, warehouse_id=warehouse_id, physical=0, reserved=0)
        db.add(row)
        db.flush()
    return row


def require_active(warehouse):
    if warehouse.archived:
        fail("Склад в архиве: новые поступления и резервы запрещены")


def require_stock(db, row, quantity):
    available = row.physical - row.reserved
    if available < quantity:
        alternatives = db.execute(
            select(
                Warehouse.id, Warehouse.name, Warehouse.archived, Balance.physical, Balance.reserved
            )
            .join(Balance, Balance.warehouse_id == Warehouse.id)
            .where(Balance.product_id == row.product_id)
            .order_by(Warehouse.id)
        ).all()
        fail(
            "Недостаточно доступного товара на выбранном ФФ. Оформите перемещение с другого склада.",
            required=quantity,
            available=available,
            warehouses=[
                {
                    "id": r.id,
                    "name": r.name,
                    "archived": r.archived,
                    "available": r.physical - r.reserved,
                }
                for r in alternatives
            ],
        )


def record(db, op, action, warehouses, changes):
    db.flush()
    db.add(
        Event(
            operation_id=op.id,
            product_id=op.product_id,
            action=action,
            quantity=op.quantity,
            source_name=warehouses[op.source_id].name if op.source_id else "Внешний приход",
            destination_name=warehouses[op.destination_id].name if op.destination_id else op.target,
            details={
                "changes": changes,
                "source_id": op.source_id,
                "destination_id": op.destination_id,
                "status": op.status,
            },
        )
    )


def delta(row, physical=0, reserved=0):
    row.physical += physical
    row.reserved += reserved
    return {
        "warehouse_id": row.warehouse_id,
        "physical_delta": physical,
        "reserved_delta": reserved,
        "physical_after": row.physical,
        "reserved_after": row.reserved,
    }


def create_operation(db: Session, data: OperationInput):
    payload = data.model_dump(mode="json")
    key = payload.pop("request_key")
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    # Serialize retries before reading the unique request key, including concurrent first requests.
    advisory_key = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], signed=True)
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": advisory_key})
    existing = db.scalar(select(Operation).where(Operation.request_key == key))
    if existing:
        if existing.fingerprint != fingerprint:
            fail("Этот ключ запроса уже использован с другими данными")
        return existing
    warehouses = lock_warehouses(db, [data.source_id, data.destination_id])
    if db.get(Product, data.product_id) is None:
        fail("Товар не найден", 404)
    if data.destination_id:
        require_active(warehouses[data.destination_id])
    if data.kind == "fbs":
        require_active(warehouses[data.source_id])
    op = Operation(
        request_key=key,
        fingerprint=fingerprint,
        **payload,
        status="received"
        if data.kind == "receipt"
        else "reserved"
        if data.kind == "fbs"
        else "in_transit",
    )
    db.add(op)
    if data.kind == "receipt":
        row = balance(db, data.product_id, data.destination_id)
        changes = [delta(row, physical=data.quantity)]
        action = "receipt"
    else:
        row = balance(db, data.product_id, data.source_id)
        require_stock(db, row, data.quantity)
        changes = [
            delta(row, reserved=data.quantity)
            if data.kind == "fbs"
            else delta(row, physical=-data.quantity)
        ]
        action = "reserve" if data.kind == "fbs" else "ship"
    record(db, op, action, warehouses, changes)
    return op


def transition(db: Session, operation_id: int, action: str):
    # Route columns never change. Lock warehouses before the operation to avoid cycles.
    route = db.execute(
        select(Operation.source_id, Operation.destination_id).where(Operation.id == operation_id)
    ).first()
    if route is None:
        fail("Операция не найдена", 404)
    warehouses = lock_warehouses(db, route)
    op = db.scalar(
        select(Operation)
        .where(Operation.id == operation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    rules = {
        ("fbs", "ship"): ("reserved", "in_transit"),
        ("fbs", "cancel"): ("reserved", "cancelled"),
        ("fbs", "deliver"): ("in_transit", "delivered"),
        ("transfer", "receive"): ("in_transit", "received"),
        ("fbo", "receive"): ("in_transit", "received"),
    }
    rule = rules.get((op.kind, action))
    if not rule:
        fail("Это действие недоступно для данного типа операции")
    before, after = rule
    # A later FBS delivery also proves that the earlier ship has succeeded.
    if op.status == after or (action == "ship" and op.status == "delivered"):
        return op
    if op.status != before:
        fail("Действие недоступно в текущем статусе", status=op.status)
    changes = []
    if op.kind == "fbs" and action in ("ship", "cancel"):
        row = balance(db, op.product_id, op.source_id)
        changes = [
            delta(row, physical=-op.quantity if action == "ship" else 0, reserved=-op.quantity)
        ]
    elif op.kind == "transfer":
        row = balance(db, op.product_id, op.destination_id)
        changes = [delta(row, physical=op.quantity)]
    op.status = after
    record(db, op, action, warehouses, changes)
    return op


def warehouse_event(db, warehouse, action, previous=None):
    db.add(
        Event(
            warehouse_id=warehouse.id,
            action=action,
            source_name=previous,
            destination_name=warehouse.name,
            details={"archived": warehouse.archived},
        )
    )
