"use strict";

const PAGE_SIZE = 50;
const paymentKinds = {
  meal: "餐费",
  delivery_fee: "配送费",
  packaging_fee: "包装费",
  discount: "优惠",
  milk_tea: "奶茶",
  purchase: "采购",
  other: "其他"
};

const filterForm = document.querySelector("#filter-form");
const pageFeedback = document.querySelector("#page-feedback");
const historyCount = document.querySelector("#history-count");
const historyMessage = document.querySelector("#history-message");
const recordList = document.querySelector("#record-list");
const loadMoreButton = document.querySelector("#load-more");
const editorPanel = document.querySelector("#editor-panel");
const mealForm = document.querySelector("#meal-form");
const mealError = document.querySelector("#meal-error");
const itemRows = document.querySelector("#item-rows");
const paymentRows = document.querySelector("#payment-rows");
const paymentSection = document.querySelector("#payments-section");
const paymentEditorPanel = document.querySelector("#payment-editor-panel");
const paymentForm = document.querySelector("#payment-form");
const paymentError = document.querySelector("#payment-error");
const state = { offset: 0, total: 0, loading: false, pendingCreate: null, draft: null, paymentOriginal: null };

function showPageFeedback(message, isError) {
  pageFeedback.textContent = message;
  pageFeedback.hidden = message === "";
  pageFeedback.classList.toggle("error", Boolean(isError));
}

function formatMoney(cents) {
  if (cents === null || cents === undefined) return "未记录";
  return "¥" + (cents / 100).toFixed(2);
}

function formatPaymentAmount(payment) {
  if (payment.amount_cents !== null && payment.amount_cents !== undefined) {
    return formatMoney(payment.amount_cents);
  }
  const min = payment.amount_min_cents;
  const max = payment.amount_max_cents;
  if (min !== null && min !== undefined && max !== null && max !== undefined) {
    return "约 " + formatMoney(min) + "–" + formatMoney(max);
  }
  if (min !== null && min !== undefined) return "约 ≥ " + formatMoney(min);
  if (max !== null && max !== undefined) return "约 ≤ " + formatMoney(max);
  return "金额未知";
}

function formatDuration(timing) {
  if (!timing) return "未知";
  const min = timing.duration_min;
  const max = timing.duration_max;
  if (min === null || min === undefined) {
    return max === null || max === undefined ? "未知" : "不超过 " + max + " 分钟";
  }
  if (max === null || max === undefined) return "至少 " + min + " 分钟";
  if (min === max) return min + " 分钟";
  return min + "–" + max + " 分钟";
}

function readFilters() {
  const form = new FormData(filterForm);
  const payload = { limit: PAGE_SIZE, offset: 0 };
  const dateFrom = form.get("date_from");
  const dateTo = form.get("date_to");
  const mealType = form.get("meal_type");
  const mode = form.get("mode");
  if (dateFrom) payload.date_from = dateFrom;
  if (dateTo) payload.date_to = dateTo;
  if (mealType) payload.meal_type = mealType;
  if (mode) payload.mode = mode;
  return payload;
}

async function loadHistory(reset) {
  if (state.loading) return;
  state.loading = true;
  const payload = readFilters();
  payload.limit = PAGE_SIZE;
  payload.offset = reset ? 0 : state.offset;
  historyMessage.textContent = "正在读取记录…";
  historyMessage.hidden = false;
  if (reset) {
    recordList.replaceChildren();
    state.offset = 0;
    state.total = 0;
    historyCount.textContent = "正在读取记录…";
  }
  loadMoreButton.disabled = true;
  try {
    const result = await callAction("meal_list", payload);
    state.total = result.total;
    state.offset = result.offset + result.items.length;
    renderRecords(result.items, reset);
    historyCount.textContent = "共 " + result.total + " 条用餐记录";
    historyMessage.textContent = result.items.length === 0 && reset ? "没有符合条件的记录。" : "";
    historyMessage.hidden = historyMessage.textContent === "";
    loadMoreButton.hidden = state.offset >= state.total;
    showPageFeedback("", false);
    return true;
  } catch (error) {
    historyMessage.textContent = "读取失败：" + error.message;
    historyMessage.hidden = false;
    if (reset) historyCount.textContent = "无法读取记录";
    loadMoreButton.hidden = true;
    return false;
  } finally {
    state.loading = false;
    loadMoreButton.disabled = false;
  }
}

function itemDescription(item) {
  const details = [];
  if (item.quantity !== null && item.quantity !== undefined && item.quantity !== "") {
    details.push(String(item.quantity) + (item.unit ? String(item.unit) : ""));
  }
  if (item.notes) details.push(String(item.notes));
  return String(item.name) + (details.length ? "（" + details.join(" · ") + "）" : "");
}

function renderRecords(records, reset) {
  if (reset) recordList.replaceChildren();
  const template = document.querySelector("#record-template");
  records.forEach(function (record) {
    const fragment = template.content.cloneNode(true);
    const card = fragment.querySelector(".record");
    const items = record.items || [];
    const payments = record.payments || [];
    card.querySelector(".record-kicker").textContent =
      record.date + " · " + (mealTypes[record.meal_type] || record.meal_type) + " · " + (modes[record.mode] || record.mode);
    card.querySelector(".record-title").textContent =
      items.length ? items.map(itemDescription).join("、") : "未填写菜品";
    card.querySelector(".record-total").textContent = formatMoney(record.meal_amount_cents);
    card.querySelector(".record-total").classList.toggle("amount-unknown", record.meal_amount_cents === null || record.meal_amount_cents === undefined);
    card.querySelector(".record-total").classList.toggle("amount-zero", record.meal_amount_cents === 0);
    card.querySelector(".record-duration").textContent = formatDuration(record.timing);
    card.querySelector(".record-location").textContent = record.location || "未记录";
    const note = card.querySelector(".record-note");
    note.textContent = record.note || "";
    note.hidden = !record.note;
    const editButton = card.querySelector(".edit-record");
    editButton.setAttribute("aria-label", "更正 " + record.date + " " + (mealTypes[record.meal_type] || record.meal_type));
    editButton.addEventListener("click", function () {
      openMealEditor("edit", record);
    });
    const paymentContainer = card.querySelector(".record-payments");
    payments.forEach(function (payment) {
      const detail = document.createElement("span");
      detail.className = "payment-detail";
      const label = (payment.scope === "extra" ? "附加 · " : "整餐 · ") + (paymentKinds[payment.kind] || payment.kind) + "：" + formatPaymentAmount(payment);
      const text = document.createElement("span");
      text.textContent = label;
      if (payment.amount_cents === null || payment.amount_cents === undefined) text.classList.add("amount-unknown");
      if (payment.amount_cents === 0) text.classList.add("amount-zero");
      detail.append(text);
      const button = document.createElement("button");
      button.className = "button button-quiet";
      button.type = "button";
      button.textContent = "更改";
      button.setAttribute("aria-label", "更改" + label);
      button.addEventListener("click", function () {
        openPaymentEditor(record, payment);
      });
      detail.append(button);
      paymentContainer.append(detail);
    });
    recordList.append(fragment);
  });
}

function emptyToNull(value) {
  const trimmed = value.trim();
  return trimmed === "" ? null : trimmed;
}

function yuanToCents(input) {
  const value = input.value.trim();
  if (value === "") return null;
  if (!/^\d+(?:\.\d{1,2})?$/.test(value)) {
    throw new Error("金额请填写非负数字，最多保留两位小数。");
  }
  const parts = value.split(".");
  return Number(parts[0]) * 100 + Number((parts[1] || "").padEnd(2, "0"));
}

function numberOrNull(input) {
  return input.value === "" ? null : Number(input.value);
}

function dateTimeToIso(input) {
  return input.value === "" ? null : new Date(input.value).toISOString();
}

function dateTimeToInput(value) {
  if (!value) return "";
  const date = new Date(value);
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}

function readItems() {
  return Array.from(itemRows.querySelectorAll(".item-row")).map(function (row) {
    const value = function (name) { return row.querySelector('[data-field="' + name + '"]').value; };
    const item = {
      name: value("name").trim(),
      quantity: emptyToNull(value("quantity")),
      unit: emptyToNull(value("unit")),
      notes: emptyToNull(value("notes"))
    };
    if (mealForm.dataset.mode === "edit" && row.dataset.itemId) item.item_id = row.dataset.itemId;
    return item;
  });
}

function readPaymentRow(row) {
  const value = function (name) { return row.querySelector('[data-field="' + name + '"]').value; };
  const amount = yuanToCents(row.querySelector('[data-field="amount"]'));
  const min = yuanToCents(row.querySelector('[data-field="amount-min"]'));
  const max = yuanToCents(row.querySelector('[data-field="amount-max"]'));
  validateAmountChoice(amount, min, max);
  validatePaymentRange(min, max);
  const source = emptyToNull(value("source"));
  validatePaymentSource(source, amount, min);
  if (min !== null && max !== null && min > max) throw new Error("付款估算下限不能高于上限。");
  return {
    kind: value("kind"),
    scope: value("scope"),
    amount_cents: amount,
    amount_min_cents: min,
    amount_max_cents: max,
    source: source
  };
}

function readPayments() {
  return Array.from(paymentRows.querySelectorAll(".payment-row")).map(readPaymentRow);
}

function validateAmountChoice(amount, min, max) {
  if (amount !== null && (min !== null || max !== null)) {
    throw new Error("单笔金额与估算范围请只填写一种。");
  }
}

function validatePaymentRange(min, max) {
  if ((min === null) !== (max === null)) throw new Error("估算范围请同时填写下限和上限。");
}

function validatePaymentSource(source, amount, min) {
  if (source === "actual" && amount === null) throw new Error("金额来源为实际时，请填写单笔金额。");
  if (source === "estimate" && amount === null && min === null) throw new Error("金额来源为估算时，请填写单值金额或估算范围。");
  if (source === "unknown" && (amount !== null || min !== null)) throw new Error("金额来源为未知时，请清空单笔金额和估算范围。");
}

function readTiming() {
  return {
    start_at: dateTimeToIso(document.querySelector("#start-at")),
    ready_at: dateTimeToIso(document.querySelector("#ready-at")),
    ended_at: dateTimeToIso(document.querySelector("#ended-at")),
    return_at: dateTimeToIso(document.querySelector("#return-at")),
    duration_min: numberOrNull(document.querySelector("#duration-min")),
    duration_max: numberOrNull(document.querySelector("#duration-max"))
  };
}

function fillTiming(timing) {
  const value = timing || {};
  document.querySelector("#start-at").value = dateTimeToInput(value.start_at);
  document.querySelector("#ready-at").value = dateTimeToInput(value.ready_at);
  document.querySelector("#ended-at").value = dateTimeToInput(value.ended_at);
  document.querySelector("#return-at").value = dateTimeToInput(value.return_at);
  document.querySelector("#duration-min").value = value.duration_min ?? "";
  document.querySelector("#duration-max").value = value.duration_max ?? "";
}

function addItemRow(item) {
  const fragment = document.querySelector("#item-row-template").content.cloneNode(true);
  const row = fragment.querySelector(".item-row");
  const value = item || {};
  row.querySelector('[data-field="name"]').value = value.name || "";
  row.querySelector('[data-field="quantity"]').value = value.quantity ?? "";
  row.querySelector('[data-field="unit"]').value = value.unit || "";
  row.querySelector('[data-field="notes"]').value = value.notes || "";
  if (mealForm.dataset.mode === "edit" && value.item_id) row.dataset.itemId = value.item_id;
  row.querySelector(".remove-row").addEventListener("click", function () { row.remove(); });
  itemRows.append(fragment);
  row.querySelector('[data-field="name"]').focus();
}

function addPaymentRow(payment) {
  const fragment = document.querySelector("#payment-row-template").content.cloneNode(true);
  const row = fragment.querySelector(".payment-row");
  const value = payment || {};
  row.querySelector('[data-field="kind"]').value = value.kind || "meal";
  row.querySelector('[data-field="scope"]').value = value.scope || "meal";
  row.querySelector('[data-field="amount"]').value = value.amount_cents === null || value.amount_cents === undefined ? "" : (value.amount_cents / 100).toFixed(2);
  row.querySelector('[data-field="amount-min"]').value = value.amount_min_cents === null || value.amount_min_cents === undefined ? "" : (value.amount_min_cents / 100).toFixed(2);
  row.querySelector('[data-field="amount-max"]').value = value.amount_max_cents === null || value.amount_max_cents === undefined ? "" : (value.amount_max_cents / 100).toFixed(2);
  row.querySelector('[data-field="source"]').value = value.source || "";
  row.querySelector(".remove-row").addEventListener("click", function () { row.remove(); });
  paymentRows.append(fragment);
  row.querySelector('[data-field="kind"]').focus();
}

function openMealEditor(mode, record) {
  if (state.pendingCreate) return;
  paymentEditorPanel.hidden = true;
  mealForm.reset();
  itemRows.replaceChildren();
  paymentRows.replaceChildren();
  mealError.hidden = true;
  mealError.textContent = "";
  mealForm.dataset.mode = mode;
  state.draft = null;
  const editing = mode === "edit";
  const mealAmountInput = document.querySelector("#meal-amount");
  const mealAmountUnknown = document.querySelector("#meal-amount-unknown");
  document.querySelector("#editor-title").textContent = editing ? "更正用餐记录" : "补记一餐";
  document.querySelector("#save-meal").textContent = editing ? "保存更正" : "保存记录";
  document.querySelector("#amount-help").textContent = editing
    ? "留空且未编辑表示不更改；0.00 表示确认 0 元，勾选可明确清空总额。"
    : "0.00 表示确认 0 元；留空时按整餐付款明细推导，勾选可明确记录未知。";
  mealAmountUnknown.checked = false;
  mealAmountInput.disabled = false;
  mealAmountInput.dataset.touched = "false";
  paymentSection.hidden = editing;
  document.querySelector("#meal-id").value = editing ? record.id : "";
  document.querySelector("#meal-version").value = editing ? record.expected_version : "";
  if (editing) {
    document.querySelector("#meal-date").value = record.date;
    document.querySelector("#meal-type").value = record.meal_type;
    document.querySelector("#meal-mode").value = record.mode;
    document.querySelector("#people-count").value = record.people_count ?? "";
    document.querySelector("#meal-location").value = record.location || "";
    document.querySelector("#meal-amount").value =
      record.meal_amount_cents === null || record.meal_amount_cents === undefined ? "" : (record.meal_amount_cents / 100).toFixed(2);
    fillTiming(record.timing);
    document.querySelector("#meal-note").value = record.note || "";
    (record.items || []).forEach(addItemRow);
  } else {
    document.querySelector("#meal-date").value = localDateValue();
  }
  editorPanel.hidden = false;
  editorPanel.scrollIntoView({ block: "start", behavior: "smooth" });
  document.querySelector("#meal-date").focus();
}

function closeMealEditor() {
  if (state.pendingCreate) return;
  editorPanel.hidden = true;
  mealError.hidden = true;
}

function lockMealEditor(locked) {
  Array.from(mealForm.querySelectorAll("input, select, textarea, button")).forEach(function (control) {
    if (control.id !== "save-meal") control.disabled = locked;
  });
  document.querySelector("#close-editor").disabled = locked;
  document.querySelector("#new-meal").disabled = locked;
  document.querySelector("#save-meal").textContent = locked ? "使用同一编号重试" : (mealForm.dataset.mode === "edit" ? "保存更正" : "保存记录");
}

function openPaymentEditor(record, payment) {
  editorPanel.hidden = true;
  paymentForm.reset();
  paymentError.hidden = true;
  paymentError.textContent = "";
  state.paymentOriginal = payment;
  document.querySelector("#payment-meal-id").value = record.id;
  document.querySelector("#payment-id").value = payment.payment_id;
  document.querySelector("#payment-version").value = record.expected_version;
  document.querySelector("#payment-kind").value = payment.kind;
  document.querySelector("#payment-scope").value = payment.scope;
  document.querySelector("#payment-amount").value =
    payment.amount_cents === null || payment.amount_cents === undefined ? "" : (payment.amount_cents / 100).toFixed(2);
  document.querySelector("#payment-amount-min").value =
    payment.amount_min_cents === null || payment.amount_min_cents === undefined ? "" : (payment.amount_min_cents / 100).toFixed(2);
  document.querySelector("#payment-amount-max").value =
    payment.amount_max_cents === null || payment.amount_max_cents === undefined ? "" : (payment.amount_max_cents / 100).toFixed(2);
  document.querySelector("#payment-source").value = payment.source || "";
  document.querySelector("#payment-note").value = payment.note || "";
  paymentEditorPanel.hidden = false;
  paymentEditorPanel.scrollIntoView({ block: "start", behavior: "smooth" });
  document.querySelector("#payment-amount").focus();
}

function closePaymentEditor() {
  paymentEditorPanel.hidden = true;
  paymentError.hidden = true;
  state.paymentOriginal = null;
}

function showConflict(message) {
  closeMealEditor();
  closePaymentEditor();
  loadHistory(true).then(function () {
    showPageFeedback(message, true);
  });
}

function mealPayload() {
  const mealAmountInput = document.querySelector("#meal-amount");
  const amountUnknown = document.querySelector("#meal-amount-unknown").checked;
  const mealAmount = yuanToCents(mealAmountInput);
  const timing = readTiming();
  if (timing.duration_min !== null && timing.duration_max !== null && timing.duration_min > timing.duration_max) {
    throw new Error("耗时下限不能高于上限。");
  }
  const payload = {
    date: document.querySelector("#meal-date").value,
    meal_type: document.querySelector("#meal-type").value,
    mode: document.querySelector("#meal-mode").value,
    items: readItems(),
    payments: readPayments(),
    timing: timing,
    location: emptyToNull(document.querySelector("#meal-location").value),
    note: emptyToNull(document.querySelector("#meal-note").value),
    people_count: numberOrNull(document.querySelector("#people-count"))
  };
  if (amountUnknown) payload.meal_amount_cents = null;
  else if (mealForm.dataset.mode === "create" && mealAmount !== null) payload.meal_amount_cents = mealAmount;
  else if (mealForm.dataset.mode === "edit" && mealAmountInput.dataset.touched === "true") payload.meal_amount_cents = mealAmount;
  return payload;
}

mealForm.addEventListener("submit", async function (event) {
  event.preventDefault();
  mealError.hidden = true;
  const saveButton = document.querySelector("#save-meal");
  saveButton.disabled = true;
  try {
    if (mealForm.dataset.mode === "create") {
      if (!state.pendingCreate) {
        const values = mealPayload();
        values.request_id = requestId();
        if (state.draft && state.draft.mode === values.mode) {
          values.recommendation_source = { source_type: state.draft.source_type, source_id: state.draft.source_id };
        }
        state.pendingCreate = values;
      }
      await callAction("meal_create", state.pendingCreate);
      state.pendingCreate = null;
      lockMealEditor(false);
      closeMealEditor();
      await loadHistory(true);
      showPageFeedback("已补记用餐记录。", false);
    } else {
      const values = mealPayload();
      delete values.payments;
      const mealId = document.querySelector("#meal-id").value;
      await callAction("meal_update", {
        request_id: requestId(),
        meal_id: mealId,
        expected_version: Number(document.querySelector("#meal-version").value),
        scope: "meal",
        changes: values
      });
      closeMealEditor();
      await loadHistory(true);
      showPageFeedback("记录已更正。", false);
    }
  } catch (error) {
    if (error.code === "VERSION_CONFLICT") {
      state.pendingCreate = null;
      lockMealEditor(false);
      showConflict("记录已被其他更正更新，当前修改未保存；历史已刷新，请重新打开最新记录再更正。");
    } else if (error.uncertain && mealForm.dataset.mode === "create") {
      lockMealEditor(true);
      mealError.textContent = "保存结果未确认。再次点击可使用同一请求编号重试；请勿离开此页面。";
      mealError.hidden = false;
    } else if (error.uncertain) {
      closeMealEditor();
      const refreshed = await loadHistory(true);
      showPageFeedback(refreshed ? "更正结果未确认，历史已刷新；请核对记录后再继续。" : "更正结果未确认，历史也暂时无法读取；请稍后重新加载记录。", true);
    } else {
      state.pendingCreate = null;
      lockMealEditor(false);
      mealError.textContent = "保存失败：" + error.message;
      mealError.hidden = false;
    }
  } finally {
    saveButton.disabled = false;
  }
});

function paymentChanges() {
  const amount = yuanToCents(document.querySelector("#payment-amount"));
  const min = yuanToCents(document.querySelector("#payment-amount-min"));
  const max = yuanToCents(document.querySelector("#payment-amount-max"));
  validateAmountChoice(amount, min, max);
  validatePaymentRange(min, max);
  const source = emptyToNull(document.querySelector("#payment-source").value);
  validatePaymentSource(source, amount, min);
  if (min !== null && max !== null && min > max) throw new Error("付款估算下限不能高于上限。");
  const original = state.paymentOriginal;
  const kind = document.querySelector("#payment-kind").value;
  const scope = document.querySelector("#payment-scope").value;
  const note = emptyToNull(document.querySelector("#payment-note").value);
  const changes = {};
  if (kind !== original.kind) changes.kind = kind;
  if (scope !== original.scope) changes.scope = scope;
  if (amount !== original.amount_cents || min !== original.amount_min_cents || max !== original.amount_max_cents || source !== original.source) {
    changes.amount_cents = amount;
    changes.amount_min_cents = min;
    changes.amount_max_cents = max;
    changes.source = source;
  }
  if (note !== emptyToNull(original.note || "")) changes.note = note;
  return changes;
}

paymentForm.addEventListener("submit", async function (event) {
  event.preventDefault();
  paymentError.hidden = true;
  const saveButton = document.querySelector("#save-payment");
  saveButton.disabled = true;
  try {
    const changes = paymentChanges();
    if (Object.keys(changes).length === 0) {
      closePaymentEditor();
      showPageFeedback("没有需要保存的付款更改。", false);
      return;
    }
    await callAction("meal_update", {
      request_id: requestId(),
      meal_id: document.querySelector("#payment-meal-id").value,
      expected_version: Number(document.querySelector("#payment-version").value),
      scope: "payment",
      payment_id: document.querySelector("#payment-id").value,
      changes: changes
    });
    closePaymentEditor();
    await loadHistory(true);
    showPageFeedback("这笔付款已更正。", false);
  } catch (error) {
    if (error.code === "VERSION_CONFLICT") {
      showConflict("记录已被其他更正更新，这笔更改未保存；历史已刷新，请重新打开最新记录再更正。");
    } else if (error.uncertain) {
      closePaymentEditor();
      const refreshed = await loadHistory(true);
      showPageFeedback(refreshed ? "付款更正结果未确认，历史已刷新；请核对这笔后再继续。" : "付款更正结果未确认，历史也暂时无法读取；请稍后重新加载记录。", true);
    } else {
      paymentError.textContent = "保存失败：" + error.message;
      paymentError.hidden = false;
    }
  } finally {
    saveButton.disabled = false;
  }
});

document.querySelector("#new-meal").addEventListener("click", function () {
  openMealEditor("create");
});
document.querySelector("#close-editor").addEventListener("click", closeMealEditor);
document.querySelector("#cancel-meal").addEventListener("click", closeMealEditor);
document.querySelector("#add-item").addEventListener("click", function () { addItemRow(); });
document.querySelector("#add-payment").addEventListener("click", function () { addPaymentRow(); });
document.querySelector("#meal-amount").addEventListener("input", function (event) {
  event.currentTarget.dataset.touched = "true";
});
document.querySelector("#meal-amount-unknown").addEventListener("change", function (event) {
  const input = document.querySelector("#meal-amount");
  if (event.currentTarget.checked) {
    input.dataset.beforeUnknown = input.value;
    input.value = "";
    input.disabled = true;
  } else {
    input.disabled = false;
    input.value = input.dataset.beforeUnknown || "";
  }
});
document.querySelector("#close-payment-editor").addEventListener("click", closePaymentEditor);
document.querySelector("#cancel-payment").addEventListener("click", closePaymentEditor);
filterForm.addEventListener("submit", function (event) {
  event.preventDefault();
  loadHistory(true);
});
document.querySelector("#clear-filters").addEventListener("click", function () {
  filterForm.reset();
  loadHistory(true);
});
loadMoreButton.addEventListener("click", function () {
  loadHistory(false);
});

loadHistory(true);

if (location.hash === "#new") {
  history.replaceState(null, "", "/records");
  openMealEditor("create");
  const savedDraft = sessionStorage.getItem("shixu-meal-draft");
  sessionStorage.removeItem("shixu-meal-draft");
  if (savedDraft) {
    const draft = JSON.parse(savedDraft);
    state.draft = draft;
    document.querySelector("#meal-type").value = draft.meal_type;
    document.querySelector("#meal-mode").value = draft.mode;
    document.querySelector("#people-count").value = draft.people_count ?? "";
    document.querySelector("#meal-location").value = draft.location ?? "";
    draft.dishes.forEach(name => addItemRow({ name }));
  }
}
