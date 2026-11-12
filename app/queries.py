from sqlalchemy import select

from app.models import Balance, Event, Operation, Product, Warehouse


def warehouses(db):
    return [
        {"id": w.id, "name": w.name, "archived": w.archived}
        for w in db.scalars(select(Warehouse).order_by(Warehouse.id))
    ]


def products(db):
    return [
        {"id": p.id, "sku": p.sku, "name": p.name}
        for p in db.scalars(select(Product).order_by(Product.id))
    ]


def operation_dict(op, product=None, wh=None):
    wh = wh or {}
    return {
        "id": op.id,
        "request_key": op.request_key,
        "kind": op.kind,
        "status": op.status,
        "product_id": op.product_id,
        "quantity": op.quantity,
        "source_id": op.source_id,
        "destination_id": op.destination_id,
        "source": wh.get(op.source_id, f"ФФ #{op.source_id}" if op.source_id else "Внешний приход"),
        "destination": wh.get(
            op.destination_id, f"ФФ #{op.destination_id}" if op.destination_id else op.target
        ),
        "target": op.target,
        "sku": product.sku if product else None,
        "product_name": product.name if product else None,
        "created_at": op.created_at,
    }


def operations(db):
    wh = {w.id: w.name for w in db.scalars(select(Warehouse))}
    return [
        operation_dict(op, p, wh)
        for op, p in db.execute(
            select(Operation, Product).join(Product).order_by(Operation.id.desc())
        )
    ]


def stock(db):
    wh = warehouses(db)
    balances = {(b.product_id, b.warehouse_id): b for b in db.scalars(select(Balance))}
    positions = {}
    for op in db.scalars(select(Operation)):
        key = None
        if op.status == "in_transit":
            key = {"transfer": "transfer_transit", "fbs": "fbs_transit", "fbo": "fbo_transit"}[
                op.kind
            ]
        elif op.kind == "fbo" and op.status == "received":
            key = "fbo_received"
        elif op.kind == "fbs" and op.status == "delivered":
            key = "fbs_delivered"
        elif op.kind == "receipt":
            key = "receipts"
        if key:
            positions.setdefault(op.product_id, {}).setdefault(key, 0)
            positions[op.product_id][key] += op.quantity
    result = []
    for p in products(db):
        rows = []
        for w in wh:
            b = balances.get((p["id"], w["id"]))
            physical, reserved = (b.physical, b.reserved) if b else (0, 0)
            rows.append(
                {**w, "physical": physical, "reserved": reserved, "available": physical - reserved}
            )
        totals = {k: sum(r[k] for r in rows) for k in ["physical", "reserved", "available"]}
        totals.update(
            {
                k: positions.get(p["id"], {}).get(k, 0)
                for k in [
                    "transfer_transit",
                    "fbs_transit",
                    "fbo_transit",
                    "fbo_received",
                    "fbs_delivered",
                    "receipts",
                ]
            }
        )
        totals["accounted"] = sum(
            totals[k]
            for k in [
                "physical",
                "transfer_transit",
                "fbs_transit",
                "fbo_transit",
                "fbo_received",
                "fbs_delivered",
            ]
        )
        result.append({**p, "warehouses": rows, "totals": totals})
    return result


def history(db, product_id=None, warehouse_id=None):
    statement = select(Event, Product.sku).outerjoin(Product, Event.product_id == Product.id)
    if product_id:
        statement = statement.where(Event.product_id == product_id)
    # IDs are stored in the immutable event snapshot, including both ends of a transfer.
    events = []
    for e, sku in db.execute(statement.order_by(Event.id.desc())):
        if warehouse_id and warehouse_id not in (
            e.warehouse_id,
            e.details.get("source_id"),
            e.details.get("destination_id"),
        ):
            continue
        events.append(
            {
                "id": e.id,
                "operation_id": e.operation_id,
                "product_id": e.product_id,
                "sku": sku,
                "action": e.action,
                "quantity": e.quantity,
                "source": e.source_name,
                "destination": e.destination_name,
                "details": e.details,
                "created_at": e.created_at,
            }
        )
    return events
