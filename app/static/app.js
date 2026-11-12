"use strict";

const $ = (selector) => document.querySelector(selector);
const escapeHtml = (value) => String(value ?? "—").replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
}[c]));
const kinds = {receipt: "Приход", transfer: "Между ФФ", fbs: "FBS", fbo: "FBO"};
const statuses = {received: "Принято", reserved: "В резерве", in_transit: "В пути", cancelled: "Отменено", delivered: "Доставлено"};
const actions = {receipt: "Приход", reserve: "Резерв FBS", ship: "Отправка", cancel: "Отмена резерва", receive: "Приёмка", deliver: "Доставка клиенту", warehouse_create: "Создание ФФ", warehouse_rename: "Переименование ФФ", warehouse_archive: "ФФ в архив"};
const pages = {
  stock: ["Остатки и местонахождение", "Товар на ФФ, в резерве и в пути к получателю"],
  operations: ["Движение товара", "Приходы, перемещения между ФФ и отправки FBS / FBO"],
  warehouses: ["Склады фулфилмента", "Остатки активных и архивных ФФ"],
  history: ["Журнал операций", "Движение каждой единицы товара и изменения складов"]
};
let state = {products: [], warehouses: [], stock: [], operations: []};
let currentPage = "stock";
let pendingOperation = null;
let busy = false;
let historyVersion = 0;

async function api(path, method = "GET", body) {
  const response = await fetch(path, {method, headers: {"Content-Type": "application/json"}, body: body === undefined ? undefined : JSON.stringify(body)});
  let data;
  try { data = await response.json(); } catch { throw new Error("Сервер вернул некорректный ответ. Обновите данные."); }
  if (!response.ok) {
    const detail = data.detail;
    let message = Array.isArray(detail) ? detail.map((d) => d.msg).join("; ") : detail?.message || "Не удалось выполнить действие";
    if (detail?.warehouses) {
      message += `\nНужно: ${detail.required}. Доступно на выбранном ФФ: ${detail.available}.`;
      message += "\nПо складам: " + detail.warehouses.map((w) => `${w.name}${w.archived ? " (архив)" : ""}: ${w.available}`).join("; ");
    }
    throw new Error(message);
  }
  return data;
}

function showMessage(text, error = false) {
  const box = $("#message");
  box.textContent = text;
  box.classList.toggle("error", error);
  box.hidden = false;
}

function selectOptions(element, rows, text, emptyLabel) {
  const previous = element.value;
  element.innerHTML = (emptyLabel ? `<option value="">${escapeHtml(emptyLabel)}</option>` : "") + rows.map((r) => `<option value="${r.id}">${escapeHtml(text(r))}</option>`).join("");
  if ([...element.options].some((o) => o.value === previous)) element.value = previous;
}

function warehouseLabel(w) { return w.name + (w.archived ? " · архив" : ""); }
function badge(status) { return `<span class="badge ${escapeHtml(status)}">${escapeHtml(statuses[status] || status)}</span>`; }
function date(value) { return new Date(value).toLocaleString("ru-RU", {dateStyle: "short", timeStyle: "short"}); }
function route(source, destination) {
  return `<div class="route-line"><span>${escapeHtml(source)}</span><span><i class="sr-only">Куда: </i>${escapeHtml(destination)}</span></div>`;
}
function table(headers, rows) {
  return rows.length ? `<table><thead><tr>${headers.map((h) => `<th scope="col">${h}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table>` : '<div class="empty">Нет записей по выбранным условиям</div>';
}

function renderStock() {
  const sum = (key) => state.stock.reduce((total, p) => total + p.totals[key], 0);
  $("#summary").innerHTML = [
    ["Доступно на ФФ", sum("available"), "Для резерва и отправки"],
    ["Физически на ФФ", sum("physical"), "Включая резерв"],
    ["В резерве FBS", sum("reserved"), "До отправки или отмены"],
    ["Всего в пути", sum("transfer_transit") + sum("fbs_transit") + sum("fbo_transit"), "Между ФФ, FBO и FBS"],
    ["Завершённые отправки", sum("fbo_received") + sum("fbs_delivered"), "Принято FBO + доставлено FBS"]
  ].map(([label, value, note]) => `<div class="stat"><div class="label">${label}</div><strong>${value}</strong><small>${note}</small></div>`).join("");
  const search = $("#stock-search").value.trim().toLowerCase();
  const products = state.stock.filter((p) => `${p.sku} ${p.name}`.toLowerCase().includes(search));
  $("#product-count").textContent = search ? `${products.length} / ${state.stock.length}` : String(products.length);
  const headers = ["Артикул / товар", ...state.warehouses.map((w) => `${escapeHtml(w.name)}${w.archived ? ' <span class="badge">Архив</span>' : ""}<small>доступно</small>`), "Итого на ФФ<small>доступно</small>", "Между ФФ<small>в пути</small>", "FBO<small>в пути</small>", "FBS<small>в пути</small>", "FBO<small>принято</small>", "FBS<small>доставлено</small>", "Баланс<small>учтено / приход</small>"];
  const rows = products.map((p) => `<tr><td class="sku">${escapeHtml(p.sku)}<small>${escapeHtml(p.name)}</small></td>${p.warehouses.map((w) => `<td class="stock-cell num"><span class="available">${w.available}</span><small>физ. ${w.physical} · рез. ${w.reserved}</small></td>`).join("")}<td class="stock-cell stock-total num"><strong>${p.totals.available}</strong><small>физ. ${p.totals.physical} · рез. ${p.totals.reserved}</small></td>${["transfer_transit", "fbo_transit", "fbs_transit", "fbo_received", "fbs_delivered"].map((k) => `<td class="num ${p.totals[k] === 0 ? "zero" : ""}">${p.totals[k]}</td>`).join("")}<td class="num"><span class="balance-check ${p.totals.accounted === p.totals.receipts ? "" : "mismatch"}">${p.totals.accounted} / ${p.totals.receipts}</span></td></tr>`);
  $("#stock-table").innerHTML = table(headers, rows);
}

function renderOperationForm() {
  const kind = $("#op-kind").value;
  const active = state.warehouses.filter((w) => !w.archived);
  selectOptions($("#op-source"), kind === "fbs" ? active : state.warehouses, warehouseLabel);
  selectOptions($("#op-destination"), active, warehouseLabel);
  $("#source-field").hidden = kind === "receipt";
  $("#destination-field").hidden = !["receipt", "transfer"].includes(kind);
  $("#target-field").hidden = !["fbs", "fbo"].includes(kind);
  $("#op-source").required = kind !== "receipt";
  $("#op-destination").required = ["receipt", "transfer"].includes(kind);
  $("[name=target]").required = ["fbs", "fbo"].includes(kind);
  $("#target-label").textContent = kind === "fbo" ? "Маркетплейс / склад назначения" : "Клиент / номер заказа";
  renderHint();
}

function renderHint() {
  const product = state.stock.find((p) => p.id === Number($("#op-product").value));
  const warehouse = product?.warehouses.find((w) => w.id === Number($("#op-source").value));
  $("#operation-hint").textContent = $("#op-kind").value === "receipt" ? "Приход увеличит физический остаток выбранного ФФ" : warehouse ? `На выбранном ФФ доступно ${warehouse.available} ед. · Физически: ${warehouse.physical}, резерв: ${warehouse.reserved}` : "Выберите товар и склад";
}

function renderOperations() {
  const filter = $("#operation-filter").value;
  const rows = state.operations.filter((op) => !filter || op.status === filter).map((op) => {
    const buttons = [];
    if (op.kind === "fbs" && op.status === "reserved") buttons.push(["ship", "Отправить"], ["cancel", "Отменить резерв"]);
    if (op.status === "in_transit") buttons.push(op.kind === "fbs" ? ["deliver", "Доставлено"] : ["receive", "Принять"]);
    return `<tr><td><strong>#${op.id}</strong><div class="muted">${date(op.created_at)}</div></td><td>${kinds[op.kind]}</td><td class="sku">${escapeHtml(op.sku)}<small>${escapeHtml(op.product_name)}</small></td><td class="num">${op.quantity}</td><td class="route">${route(op.source, op.destination)}</td><td>${badge(op.status)}</td><td><div class="actions">${buttons.map(([action, label]) => `<button class="secondary" data-op="${op.id}" data-action="${action}">${label}</button>`).join("")}</div></td></tr>`;
  });
  $("#operations-table").innerHTML = table(["Операция", "Тип", "Товар", "Ед.", "Маршрут", "Статус", "Действия"], rows);
}

function renderWarehouses() {
  $("#warehouse-cards").innerHTML = state.warehouses.map((w) => {
    const totals = {physical: 0, reserved: 0, available: 0};
    state.stock.forEach((p) => { const b = p.warehouses.find((r) => r.id === w.id); for (const k of Object.keys(totals)) totals[k] += b?.[k] || 0; });
    return `<article class="warehouse-card ${w.archived ? "archived" : ""}"><div class="warehouse-top"><h2>${escapeHtml(w.name)}</h2><span class="badge ${w.archived ? "" : "received"}">${w.archived ? "Архив" : "Активный ФФ"}</span></div><div class="warehouse-stats">${[["physical", "Физически"], ["available", "Доступно"], ["reserved", "Резерв FBS"]].map(([k, label]) => `<div class="muted">${label}<strong>${totals[k]}</strong></div>`).join("")}</div><div class="actions"><button class="secondary" data-wh="${w.id}" data-action="rename">Переименовать</button>${w.archived ? '<span class="muted">Остатки включены в общий учёт</span>' : `<button class="danger" data-wh="${w.id}" data-action="archive">Удалить в архив</button>`}</div></article>`;
  }).join("") || '<div class="empty">Добавьте первый ФФ</div>';
}

async function renderHistory() {
  const version = ++historyVersion;
  const params = new URLSearchParams();
  if ($("#history-product").value) params.set("product_id", $("#history-product").value);
  if ($("#history-warehouse").value) params.set("warehouse_id", $("#history-warehouse").value);
  const events = await api(`/api/history?${params}`);
  if (version !== historyVersion) return;
  $("#history-table").innerHTML = table(["Дата / операция", "Действие", "Артикул", "Ед.", "Откуда → куда", "Изменение на ФФ"], events.map((e) => {
    const changes = (e.details.changes || []).map((c) => {
      const name = state.warehouses.find((w) => w.id === c.warehouse_id)?.name || `ФФ #${c.warehouse_id}`;
      const signed = (n) => n > 0 ? `+${n}` : String(n);
      return `${escapeHtml(name)}: физ. ${signed(c.physical_delta)}, резерв ${signed(c.reserved_delta)}<br>После: физ. ${c.physical_after}, резерв ${c.reserved_after}`;
    }).join("<br>");
    return `<tr><td>${date(e.created_at)}<div class="muted">${e.operation_id ? `#${e.operation_id}` : `событие #${e.id}`}</div></td><td>${escapeHtml(actions[e.action] || e.action)}</td><td>${escapeHtml(e.sku)}</td><td class="num">${escapeHtml(e.quantity)}</td><td class="route">${route(e.source, e.destination)}</td><td class="event-changes">${changes || "—"}</td></tr>`;
  }));
}

async function refresh() {
  state = await api("/api/dashboard");
  selectOptions($("#op-product"), state.products, (p) => `${p.sku} · ${p.name}`);
  selectOptions($("#history-product"), state.products, (p) => p.sku, "Все товары");
  selectOptions($("#history-warehouse"), state.warehouses, warehouseLabel, "Все склады");
  renderStock(); renderOperationForm(); renderOperations(); renderWarehouses();
  if (currentPage === "history") await renderHistory();
  $("#updated").textContent = `Обновлено в ${new Date().toLocaleTimeString("ru-RU")}`;
}

async function run(task) {
  if (busy) return;
  busy = true;
  document.querySelectorAll("button").forEach((b) => b.disabled = true);
  try { await task(); } catch (error) { showMessage(error.message || "Нет связи с сервером", true); }
  finally { busy = false; document.querySelectorAll("button").forEach((b) => b.disabled = false); }
}

async function afterMutation(message) {
  showMessage(message);
  try { await refresh(); } catch { showMessage(`${message}\nДействие сохранено, но обновить экран не удалось. Нажмите «Обновить».`, true); }
}

document.querySelectorAll(".nav").forEach((button) => button.addEventListener("click", () => {
  currentPage = button.dataset.page;
  document.querySelectorAll(".nav").forEach((b) => {
    b.classList.toggle("active", b === button);
    if (b === button) b.setAttribute("aria-current", "page");
    else b.removeAttribute("aria-current");
  });
  document.querySelectorAll(".page").forEach((p) => p.hidden = p.id !== `page-${currentPage}`);
  $("#page-title").textContent = pages[currentPage][0];
  $("#page-subtitle").textContent = pages[currentPage][1];
  $("#message").hidden = true;
  if (currentPage === "history") run(renderHistory);
}));
$("#refresh").addEventListener("click", () => run(refresh));
$("#stock-search").addEventListener("input", renderStock);
$("#op-kind").addEventListener("change", renderOperationForm);
$("#op-product").addEventListener("change", renderHint);
$("#op-source").addEventListener("change", renderHint);
$("#operation-filter").addEventListener("change", renderOperations);
$("#history-product").addEventListener("change", () => renderHistory().catch((e) => showMessage(e.message, true)));
$("#history-warehouse").addEventListener("change", () => renderHistory().catch((e) => showMessage(e.message, true)));

$("#operation-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const data = new FormData(event.currentTarget);
  const kind = data.get("kind");
  const payload = {kind, product_id: Number(data.get("product_id")), quantity: Number(data.get("quantity"))};
  if (kind !== "receipt") payload.source_id = Number(data.get("source_id"));
  if (["receipt", "transfer"].includes(kind)) payload.destination_id = Number(data.get("destination_id"));
  if (["fbs", "fbo"].includes(kind)) payload.target = data.get("target").trim();
  const signature = JSON.stringify(payload);
  if (pendingOperation?.signature !== signature) pendingOperation = {signature, key: crypto.randomUUID()};
  payload.request_key = pendingOperation.key;
  run(async () => {
    const operation = await api("/api/operations", "POST", payload);
    pendingOperation = null;
    await afterMutation(`Операция #${operation.id}: ${statuses[operation.status]}`);
  });
});

$("#warehouse-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const name = new FormData(form).get("name").trim();
  run(async () => { await api("/api/warehouses", "POST", {name}); form.reset(); await afterMutation("Склад добавлен"); });
});

$("#operations-table").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-op]");
  if (!button) return;
  run(async () => { await api(`/api/operations/${button.dataset.op}/${button.dataset.action}`, "POST"); await afterMutation("Действие выполнено. Остатки обновлены."); });
});

$("#warehouse-cards").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-wh]");
  if (!button) return;
  const warehouse = state.warehouses.find((w) => w.id === Number(button.dataset.wh));
  if (button.dataset.action === "rename") {
    const name = window.prompt("Новое название ФФ", warehouse.name);
    if (!name?.trim() || name.trim() === warehouse.name) return;
    run(async () => { await api(`/api/warehouses/${warehouse.id}`, "PATCH", {name: name.trim()}); await afterMutation("Название изменено. Прошлые события сохранены."); });
  } else if (window.confirm(`Перевести ФФ «${warehouse.name}» в архив? Остатки сохранятся за этим складом и останутся в общем учёте.`)) {
    run(async () => { await api(`/api/warehouses/${warehouse.id}`, "DELETE"); await afterMutation("ФФ в архиве. Остатки и история сохранены."); });
  }
});

run(refresh);
