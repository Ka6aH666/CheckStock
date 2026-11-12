"""Optional real-browser acceptance scenario. Uses only the isolated _test database.

Run after installing requirements-browser.txt. On Windows uses installed Edge;
else run `python -m playwright install chromium` first.
"""

import os
from pathlib import Path
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
import httpx  # noqa: E402
from playwright.sync_api import expect, sync_playwright  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.config import settings  # noqa: E402
from app.seed import seed  # noqa: E402


def main():
    os.chdir(ROOT)
    url = make_url(settings.test_database_url)
    prod = make_url(settings.database_url)
    if (
        url.get_backend_name() != "postgresql"
        or not (url.database or "").endswith("_test")
        or url.database == prod.database
    ):
        raise RuntimeError("Browser check requires a separate _test PostgreSQL database")
    config = Config("alembic.ini")
    config.attributes["database_url"] = settings.test_database_url
    command.upgrade(config, "head")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE events, operations, balances, warehouses, products RESTART IDENTITY CASCADE"
            )
        )
    with Session(engine) as db, db.begin():
        seed(db)
    engine.dispose()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    env = {**os.environ, "DATABASE_URL": settings.test_database_url}
    (ROOT / ".local").mkdir(exist_ok=True)
    errors = []
    with (ROOT / ".local/browser-server.log").open("w", encoding="utf-8") as log:
        server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            env=env,
            stdout=log,
            stderr=log,
        )
        try:
            for _ in range(100):
                try:
                    if httpx.get(base + "/health").status_code == 200:
                        break
                except httpx.ConnectError:
                    pass
                time.sleep(0.1)
            else:
                raise RuntimeError(
                    "Browser test server did not start; see .local/browser-server.log"
                )
            with sync_playwright() as p:
                edge = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
                browser = p.chromium.launch(executable_path=str(edge) if edge.exists() else None)
                page = browser.new_page(
                    viewport={"width": 1440, "height": 1000}, device_scale_factor=1
                )
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(base)
                expect(page.locator("#stock-table tbody tr")).to_have_count(3)
                page.screenshot(path=str(ROOT / ".local/dashboard.png"), full_page=True)
                page.locator("#stock-search").fill("Корзина")
                expect(page.locator("#stock-table tbody tr")).to_have_count(1)
                page.locator("#stock-search").fill("")
                page.locator('.nav[data-page="operations"]').click()
                page.locator("#op-kind").select_option("fbs")
                page.locator('[name="quantity"]').fill("12")
                page.locator('[name="target"]').fill("Демо FBS-001")
                page.get_by_role("button", name="Оформить операцию").click()
                expect(page.locator("#message")).to_contain_text("Недостаточно")
                expect(page.locator("#message")).to_contain_text("Доступно на выбранном ФФ: 10")
                page.locator("#op-kind").select_option("transfer")
                page.locator("#op-source").select_option("2")
                page.locator("#op-destination").select_option("1")
                page.locator('[name="quantity"]').fill("2")
                page.get_by_role("button", name="Оформить операцию").click()
                expect(page.locator("#message")).to_contain_text("В пути")
                page.get_by_role("button", name="Принять", exact=True).click()
                expect(page.locator("#message")).to_contain_text("Действие выполнено")
                page.locator("#op-kind").select_option("fbs")
                page.locator("#op-source").select_option("1")
                page.locator('[name="quantity"]').fill("12")
                page.get_by_role("button", name="Оформить операцию").click()
                expect(page.locator("#message")).to_contain_text("В резерве")
                page.get_by_role("button", name="Отправить", exact=True).click()
                expect(page.get_by_role("button", name="Доставлено", exact=True)).to_be_visible()
                # Repeat through the API because the UI correctly removes the old action.
                with httpx.Client(base_url=base) as api:
                    fbs = next(
                        op for op in api.get("/api/operations").json() if op["kind"] == "fbs"
                    )
                    assert api.post(f"/api/operations/{fbs['id']}/ship").status_code == 200
                    product = api.get("/api/stock").json()[0]
                    assert product["totals"]["fbs_transit"] == 12
                    assert product["warehouses"][0]["physical"] == 0
                page.locator("#op-kind").select_option("fbo")
                page.locator("#op-product").select_option("2")
                page.locator('[name="quantity"]').fill("1")
                page.locator('[name="target"]').fill("WB / Коледино")
                page.get_by_role("button", name="Оформить операцию").click()
                expect(page.get_by_role("button", name="Принять", exact=True)).to_be_visible()
                page.get_by_role("button", name="Принять", exact=True).click()
                expect(page.locator("#message")).to_contain_text("Действие выполнено")
                page.locator('.nav[data-page="warehouses"]').click()
                page.once("dialog", lambda dialog: dialog.accept("Запад"))
                page.locator(".warehouse-card").first.get_by_role(
                    "button", name="Переименовать"
                ).click()
                expect(page.locator(".warehouse-card").first).to_contain_text("Запад")
                page.once("dialog", lambda dialog: dialog.accept())
                page.locator(".warehouse-card").nth(1).get_by_role(
                    "button", name="Удалить в архив"
                ).click()
                expect(page.locator(".warehouse-card").nth(1)).to_have_class(
                    "warehouse-card archived"
                )
                page.locator('.nav[data-page="stock"]').click()
                expect(page.locator("#stock-table")).to_contain_text("Архив")
                page.locator('.nav[data-page="history"]').click()
                expect(page.locator("#history-table")).to_contain_text("Север")
                page.locator("#history-product").select_option("1")
                expect(page.locator("#history-table")).to_contain_text("SKU-101")
                expect(page.locator("#history-table")).not_to_contain_text("SKU-102")
                page.set_viewport_size({"width": 390, "height": 844})
                page.locator('.nav[data-page="stock"]').click()
                page.screenshot(path=str(ROOT / ".local/mobile.png"), full_page=True)
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                assert not errors, errors
                browser.close()
            print(
                "Browser acceptance passed: shortage, transfer, FBS, repeat ship, FBO, rename, archive, history, mobile."
            )
        finally:
            server.terminate()
            server.wait(timeout=10)


if __name__ == "__main__":
    main()
