from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import queries, services
from app.db import get_db
from app.models import Warehouse
from app.schemas import OperationInput, WarehouseInput

ROOT = Path(__file__).resolve().parent
app = FastAPI(title="CheckStock", version="1.0.0", description="Учёт одного магазина и его ФФ")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
templates = Jinja2Templates(directory=ROOT / "templates")


@app.exception_handler(IntegrityError)
def integrity_error(request, exc):
    return JSONResponse(
        status_code=409,
        content={
            "detail": {
                "message": "Конфликт данных: название склада уже занято или нарушено ограничение учёта"
            }
        },
    )


@app.get("/", include_in_schema=False)
def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


@app.get("/health")
def health(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.get("/api/dashboard")
def dashboard(db: Session = Depends(get_db)):
    # One consistent snapshot across tables; a concurrent commit cannot split the balance view.
    with db.begin():
        db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        return {
            "products": queries.products(db),
            "warehouses": queries.warehouses(db),
            "stock": queries.stock(db),
            "operations": queries.operations(db),
        }


@app.get("/api/stock")
def stock(db: Session = Depends(get_db)):
    with db.begin():
        db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        return queries.stock(db)


@app.get("/api/products")
def products(db: Session = Depends(get_db)):
    return queries.products(db)


@app.get("/api/warehouses")
def warehouses(db: Session = Depends(get_db)):
    return queries.warehouses(db)


@app.post("/api/warehouses", status_code=201)
def create_warehouse(data: WarehouseInput, db: Session = Depends(get_db)):
    with db.begin():
        warehouse = Warehouse(name=data.name, archived=False)
        db.add(warehouse)
        db.flush()
        services.warehouse_event(db, warehouse, "warehouse_create")
    return {"id": warehouse.id, "name": warehouse.name, "archived": warehouse.archived}


@app.patch("/api/warehouses/{warehouse_id}")
def rename_warehouse(warehouse_id: int, data: WarehouseInput, db: Session = Depends(get_db)):
    with db.begin():
        warehouse = services.lock_warehouses(db, [warehouse_id])[warehouse_id]
        if warehouse.name != data.name:
            previous = warehouse.name
            warehouse.name = data.name
            services.warehouse_event(db, warehouse, "warehouse_rename", previous)
    return {"id": warehouse.id, "name": warehouse.name, "archived": warehouse.archived}


@app.delete("/api/warehouses/{warehouse_id}")
def archive_warehouse(warehouse_id: int, db: Session = Depends(get_db)):
    with db.begin():
        warehouse = services.lock_warehouses(db, [warehouse_id])[warehouse_id]
        if not warehouse.archived:
            warehouse.archived = True
            services.warehouse_event(db, warehouse, "warehouse_archive")
    return {"id": warehouse.id, "name": warehouse.name, "archived": warehouse.archived}


@app.get("/api/operations")
def list_operations(db: Session = Depends(get_db)):
    return queries.operations(db)


@app.post("/api/operations")
def create_operation(data: OperationInput, db: Session = Depends(get_db)):
    with db.begin():
        op = services.create_operation(db, data)
    return queries.operation_dict(op)


@app.post("/api/operations/{operation_id}/{action}")
def act(
    operation_id: int,
    action: Literal["ship", "cancel", "receive", "deliver"],
    db: Session = Depends(get_db),
):
    with db.begin():
        op = services.transition(db, operation_id, action)
    return queries.operation_dict(op)


@app.get("/api/history")
def history(
    product_id: int | None = Query(None, gt=0),
    warehouse_id: int | None = Query(None, gt=0),
    db: Session = Depends(get_db),
):
    return queries.history(db, product_id, warehouse_id)
