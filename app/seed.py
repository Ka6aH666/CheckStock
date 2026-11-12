"""Repeatable demo receipts; does not reset balances or overwrite manager edits."""

from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select, text

from app.db import SessionLocal
from app.models import Operation, Product, Warehouse
from app.schemas import OperationInput
from app.services import create_operation, warehouse_event


def seed(db):
    db.execute(text("SELECT pg_advisory_xact_lock(82490261)"))
    marker = str(uuid5(NAMESPACE_URL, "checkstock/seed/SKU-101/Север"))
    if db.scalar(select(Operation.id).where(Operation.request_key == marker)):
        return False
    warehouses = {}
    for name in ["Север", "Юг"]:
        warehouse = db.scalar(select(Warehouse).where(Warehouse.name == name))
        if warehouse is None:
            warehouse = Warehouse(name=name, archived=False)
            db.add(warehouse)
            db.flush()
            warehouse_event(db, warehouse, "warehouse_create")
        warehouses[name] = warehouse.id
    for sku, name, north, south in [
        ("SKU-101", "Органайзер", 10, 6),
        ("SKU-102", "Контейнер", 5, 0),
        ("SKU-103", "Корзина", 0, 4),
    ]:
        product = db.scalar(select(Product).where(Product.sku == sku))
        if product is None:
            product = Product(sku=sku, name=name)
            db.add(product)
            db.flush()
        for warehouse_name, quantity in [("Север", north), ("Юг", south)]:
            if quantity:
                create_operation(
                    db,
                    OperationInput(
                        request_key=uuid5(NAMESPACE_URL, f"checkstock/seed/{sku}/{warehouse_name}"),
                        kind="receipt",
                        product_id=product.id,
                        quantity=quantity,
                        destination_id=warehouses[warehouse_name],
                    ),
                )
    return True


def main():
    with SessionLocal.begin() as db:
        changed = seed(db)
    print("Demo data created." if changed else "Demo data already loaded; no changes.")


if __name__ == "__main__":
    main()
