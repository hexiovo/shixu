"use strict";

(() => {
  const $ = selector => document.querySelector(selector);
  const dateText = new Intl.DateTimeFormat("zh-CN", { timeZone: shixuTimeZone, month: "long", day: "numeric", weekday: "long" });
  const clockText = new Intl.DateTimeFormat("zh-CN", { timeZone: shixuTimeZone, hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
  const compareForm = $("#compare-form");
  const statusLabels = { eligible: "预计可行", needs_verification: "待核实", excluded: "已排除" };
  const preferenceLabels = { liked: "收藏", want_to_try: "待尝", temporarily_tired: "暂时吃腻" };
  const sourceLabels = { observed: "实测", estimate: "估算", manual: "手工记录", actual: "实际", unknown: "未知" };
  const targetLabels = { meal_start: "开饭", finished: "吃完", returned: "返回" };
  const paymentLabels = { meal: "餐费", delivery_fee: "配送", packaging_fee: "包装", discount: "优惠", milk_tea: "奶茶", purchase: "采购", other: "其他" };
  const stageNames = { outbound: "去程", queue: "排队", wait: "等餐", order_to_ready: "送达或自取", shopping: "采购", prep: "备菜", cook: "烹饪", meal: "用餐", return: "返程" };
  const stageKeys = {
    eat_out: ["outbound", "queue", "wait", "meal", "return"],
    delivery: ["order_to_ready", "meal", "return"],
    home_cooking: ["shopping", "prep", "cook", "meal", "return"]
  };
  const ui = { page: null, compareSeq: 0, showAll: false, candidate: null, store: null, mutationIds: new WeakMap() };

  function el(tag, className, ...children) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    node.append(...children);
    return node;
  }
  function link(label, href) {
    const anchor = el("a", "text-action", label);
    anchor.href = href;
    return anchor;
  }
  function notice(text, error = false) {
    const output = $("#page-status");
    output.textContent = text;
    output.hidden = !text;
    output.classList.toggle("error", error);
  }
  function row(label, detail, value) {
    const name = el("div", "", el("strong", "", label));
    if (detail) name.append(el("small", "", detail));
    return el("div", "data-row", name, value ?? "");
  }
  function fill(node, entries, empty) {
    node.replaceChildren(...(entries.length ? entries : [el("p", "empty", empty)]));
  }
  const input = (form, name) => form.elements.namedItem(name);
  const field = (data, name) => (data.get(name) ?? "").trim();
  const parts = text => text.split(/[\n,，、]/).map(value => value.trim()).filter(Boolean);
  const yuan = amount => amount == null ? "未知" : `¥${(amount / 100).toFixed(2)}`;
  const cents = text => Math.round(Number(text) * 100);
  function range(value, format) {
    if (!value) return "未知";
    return value.min === value.max ? format(value.min) : `${format(value.min)}–${format(value.max)}`;
  }
  function timeRange(value) {
    return value ? range({ min: value.earliest, max: value.latest }, value => clockText.format(new Date(value))) : "未知";
  }
  function chosenSlot() {
    const hour = Number(clockText.format(new Date()).split(":")[0]);
    return hour < 10 ? "breakfast" : hour < 15 ? "lunch" : "dinner";
  }
  function dataWindow(from, to = from) {
    return { date_from: from, date_to: to };
  }
  async function mutate(control, action, fields) {
    const key = JSON.stringify([action, fields]);
    const previous = ui.mutationIds.get(control);
    const request_id = previous?.key === key ? previous.id : requestId();
    ui.mutationIds.set(control, { key, id: request_id });
    try {
      const result = await callAction(action, { request_id, ...fields });
      ui.mutationIds.delete(control);
      return result;
    } catch (error) {
      if (!error.uncertain) ui.mutationIds.delete(control);
      throw error;
    }
  }
  function errorText(error) {
    if (error.uncertain) return "结果未确认。可用相同内容重试。";
    if (error.code === "VERSION_CONFLICT") return "数据已有新版本，请刷新后重试。";
    return error.message;
  }
  function button(label, handler, kind = "quiet") {
    const control = el("button", `button button-${kind}`, label);
    control.type = "button";
    control.addEventListener("click", async () => {
      control.disabled = true;
      notice("");
      try { await handler(control); } catch (error) { notice(errorText(error), true); }
      finally { control.disabled = false; }
    });
    return control;
  }
  function handleForm(selector, handler) {
    const form = $(selector);
    form.addEventListener("submit", async event => {
      event.preventDefault();
      const submit = form.querySelector("button[type=submit]");
      submit.disabled = true;
      notice("");
      try { await handler(new FormData(form), form); } catch (error) { notice(errorText(error), true); }
      finally { submit.disabled = false; }
    });
  }

  function recommendationArgs(data, now) {
    const target = field(data, "time_target");
    const result = {
      now: now.toISOString(), meal_type: field(data, "meal_type"), time_target: target,
      budget_basis: field(data, "budget_basis"), use_kitchen_stock: data.has("stock"), ranking: "score", limit: 200
    };
    for (const name of ["mode", "location", "return_location", "occasion", "price_region"]) {
      if (field(data, name)) result[name] = field(data, name);
    }
    if (field(data, "people_count")) result.people_count = Number(field(data, "people_count"));
    if (field(data, "budget")) result.budget_cents = cents(field(data, "budget"));
    if (field(data, "utensils")) result.available_utensils = parts(field(data, "utensils"));
    if (field(data, "deadline")) {
      result.deadline = `${localDateValue(now)}T${field(data, "deadline")}:00+08:00`;
      if (new Date(result.deadline) < now) throw new Error("截止时间已经过去，请改为今天稍晚的时间。");
    }
    return result;
  }
  function translateMissing(reason) {
    if (stageNames[reason]) return `缺少${stageNames[reason]}时间`;
    const exact = {
      "time constraint exceeded": "预计超出截止时间", "payment budget exceeded": "预计付款超出预算",
      "consumption budget exceeded": "食用成本超出预算", "total payment cost": "缺少付款金额",
      "total consumption cost": "缺少食用成本", "user marked as avoid": "已标为避免"
    };
    if (exact[reason]) return exact[reason];
    for (const [prefix, label] of [
      ["contains restricted allergen: ", "包含受限过敏原"], ["ingredients need verification: ", "须核实配料"],
      ["allergen needs verification: ", "须核实过敏原"], ["contains temporary avoidance: ", "包含临时忌口食材"],
      ["occasion not among recorded options: ", "记录的场合不含"], ["occasion suitability unrecorded: ", "未记录是否适合"],
      ["available ingredient: ", "缺少或须核实食材"], ["missing utensil: ", "缺少或须核实厨具"]
    ]) {
      if (reason.startsWith(prefix)) return `${label}：${reason.slice(prefix.length)}`;
    }
    return reason;
  }
  function preferenceButton(item, preference, enabled, callback) {
    const title = preferenceLabels[preference];
    return button(enabled ? `取消${title}` : title, async control => {
      await mutate(control, "recommendation_preference_set", {
        source_type: item.source_type, source_id: item.id, preference, enabled: !enabled
      });
      await callback();
      notice(enabled ? `已取消${title}。` : `已标记${title}。`);
    });
  }
  function draftMeal(item, compare) {
    sessionStorage.setItem("shixu-meal-draft", JSON.stringify({
      meal_type: compare.meal_type, mode: item.mode, dishes: item.dishes,
      people_count: compare.people_count, location: compare.location,
      source_type: item.source_type, source_id: item.id
    }));
    location.assign("/records#new");
  }
  function renderOffer(item, result, railLimit, isBest) {
    const time = item.time;
    const cost = item.cost;
    const reading = result.time_target === "returned" ? time.return_at : result.time_target === "finished" ? time.finish_at : time.start_at;
    const offer = el("article", `offer${isBest ? " best" : ""}`);
    offer.dataset.status = item.status;
    const rail = el("div", "time-rail");
    if (time.minutes) {
      const bar = el("progress", "");
      bar.max = railLimit;
      bar.value = time.minutes.max;
      bar.setAttribute("aria-label", `预计最多 ${time.minutes.max} 分钟`);
      rail.append(bar);
    }
    if (result.deadline) {
      const elapsed = Math.max(0, (new Date(result.deadline) - new Date(result.now)) / 60000);
      const marker = el("span", "deadline-mark");
      marker.style.left = `${100 * elapsed / railLimit}%`;
      marker.setAttribute("aria-hidden", "true");
      rail.append(marker);
    }
    const name = el("div", "offer-name", el("small", "", modes[item.mode]), el("strong", "", item.name));
    const dishes = item.dishes.filter(dish => dish !== item.name);
    if (dishes.length) name.append(el("small", "", dishes.join("、")));
    const timeline = el("div", "time-lane", el("span", "", timeRange(reading)), rail);
    const score = el("div", "score", `${item.recommendation_score.percent}%`, el("small", "", isBest ? "最高推荐" : statusLabels[item.status]));
    const amount = el("div", "offer-value", el("span", "amount", yuan(cost.cents?.min)));
    if (cost.cents && cost.cents.max !== cost.cents.min) amount.append("–", el("wbr"), el("span", "amount", yuan(cost.cents.max)));
    offer.append(el("div", "offer-main", name, timeline, amount, score));
    const details = el("details", "offer-details", el("summary", "", "依据与操作"));
    details.append(
      el("p", "", `开饭 ${timeRange(time.start_at)} · 吃完 ${timeRange(time.finish_at)} · 返回 ${timeRange(time.return_at)}`),
      el("p", "", `时间依据 ${sourceLabels[time.source] ?? time.source} · 本次付款 ${range(cost.payment.cents, yuan)} · 食用成本 ${range(cost.consumption.cents, yuan)}`)
    );
    const components = item.recommendation_score.components;
    if (components.time !== undefined) details.append(el("p", "", `时间 ${components.time}/40 · 费用 ${components.cost}/30 · 喜好 ${components.preference}/20 · 近期 ${components.variety}/10`));
    if (item.occasion.environment || item.occasion.recorded_occasions.length) {
      details.append(el("p", "", [item.occasion.environment, item.occasion.recorded_occasions.join("、")].filter(Boolean).join(" · ")));
    }
    const priceSources = [...new Set((item.recipe_price_estimate?.items ?? []).filter(line => line.price).map(line => `${line.price.source}（${line.price.recorded_on}）`))];
    if (priceSources.length) details.append(el("p", "", `菜价依据 ${priceSources.join("、")}`));
    if (item.missing.length) details.append(el("p", "", [...new Set(item.missing.map(translateMissing))].join("；")));
    const history = item.recommendation_context.history;
    if (history.recent_meal_count) details.append(el("p", "", `近期已关联 ${history.recent_meal_count} 餐，最近一次 ${history.meals[0]?.calendar_date ?? ""}`));
    const actions = el("div", "offer-actions");
    if (item.status !== "excluded" && !isBest) actions.append(button("用这个方案记餐", () => draftMeal(item, result), "primary"));
    for (const key of ["liked", "want_to_try"]) actions.append(preferenceButton(item, key, Boolean(history.preference[key]), reloadCompare));
    details.append(actions);
    if (isBest) {
      const choice = button("用这个方案记餐", () => draftMeal(item, result));
      choice.classList.add("primary-choice");
      offer.append(el("div", "offer-footer", details, choice));
    } else offer.append(details);
    return offer;
  }
  function renderCompare(result) {
    const output = $("#offer-list");
    const heading = $("#recommend-summary");
    const eligible = result.items.filter(item => item.status === "eligible");
    const best = eligible[0]?.recommendation_score.percent;
    heading.textContent = !result.total ? "还没有可比较的候选。" :
      eligible.length ? `${mealTypes[result.meal_type]}有 ${eligible.length} 个预计可行，最高推荐度 ${best}%。` :
        "当前没有能确认符合条件的方案。可展开候选看缺项。";
    if (!result.items.length) {
      output.replaceChildren(el("p", "empty", "先", link("添加一组候选", "#favorites"), "，或", link("保存一份菜谱", "#recipes"), "。"));
      return;
    }
    const firstByMode = new Set();
    for (const item of result.items) {
      if (![...firstByMode].some(first => first.mode === item.mode)) firstByMode.add(item);
    }
    const shown = ui.showAll ? result.items : result.items.filter(item => firstByMode.has(item));
    const elapsed = result.deadline ? Math.max(0, (new Date(result.deadline) - new Date(result.now)) / 60000) : 0;
    const max = Math.max(1, elapsed, ...result.items.map(item => item.time.minutes?.max ?? 0));
    const table = el("div", "offer-table", el("div", "offer-head",
      el("span", "", "方案"), el("span", "", `预计${targetLabels[result.time_target]}`),
      el("span", "", result.budget_basis === "payment" ? "本次付款" : "食用成本"), el("span", "", "推荐度")
    ), ...shown.map(item => renderOffer(item, result, max, item.status === "eligible" && item.recommendation_score.percent === best)));
    const timing = result.deadline ? `从 ${clockText.format(new Date(result.now))} 算起，竖线为 ${clockText.format(new Date(result.deadline))} 截止。` :
      `从 ${clockText.format(new Date(result.now))} 算起，横条按预计分钟上限比较。`;
    output.replaceChildren(table, el("p", "section-note", timing));
    if (shown.length < result.items.length || ui.showAll) {
      output.append(button(ui.showAll ? "只看每种方式首选" : `展开全部 ${result.items.length} 项`, () => {
        ui.showAll = !ui.showAll;
        renderCompare(result);
      }));
    }
    if (result.total > result.items.length) output.append(el("p", "section-note", `显示前 ${result.items.length} 项，共 ${result.total} 项。可按方式缩小范围。`));
  }
  async function reloadCompare(data = new FormData(compareForm)) {
    const sequence = ++ui.compareSeq;
    try {
      const result = await callAction("recommend", recommendationArgs(data, new Date()));
      if (sequence !== ui.compareSeq) return;
      renderCompare(result);
    } catch (error) {
      if (sequence !== ui.compareSeq) return;
      $("#recommend-summary").textContent = errorText(error);
      $("#offer-list").replaceChildren();
    }
  }

  function showNextReminder(rules, now) {
    const day = localDateValue(now);
    const start = new Date(`${day}T00:00:00+08:00`).getTime();
    let next;
    for (let offset = 0; offset <= 7; offset++) {
      const date = new Date(start + offset * 86400000);
      const iso = localDateValue(date);
      const weekday = (new Date(`${iso}T00:00:00Z`).getUTCDay() + 6) % 7;
      for (const rule of rules) {
        const at = new Date(`${iso}T${rule.at}:00+08:00`);
        if (rule.enabled && rule.weekdays.includes(weekday) && at > now && (!next || at < next.at)) next = { rule, at };
      }
    }
    const output = $("#next-reminder");
    output.className = next ? "upcoming" : "empty";
    output.replaceChildren(...(next ? [
      el("strong", "", clockText.format(next.at)),
      el("p", "muted", `${localDateValue(next.at) === day ? "今天" : dateText.format(next.at)} · ${mealTypes[next.rule.meal_type]}${next.rule.note ? ` · ${next.rule.note}` : ""}`)
    ] : ["还没有启用的饭点提醒。"]));
  }
  function mealRow(meal) {
    const dishes = meal.dishes ?? meal.items.map(item => item.name);
    const time = meal.timing?.ended_at ? clockText.format(new Date(meal.timing.ended_at)) : "";
    const store = meal.store ? [meal.store.name, meal.store.branch].filter(Boolean).join(" · ") : "";
    const meta = [time, mealTypes[meal.meal_type], modes[meal.mode], store].filter(Boolean).join(" · ");
    const value = meal.meal_amount_cents == null ? "" : yuan(meal.meal_amount_cents);
    const content = row(dishes.join("、") || "未记录菜品", meta, value);
    if (meal.payments?.length) {
      const list = el("ul", "section-note");
      meal.payments.forEach(payment => list.append(el("li", "", `${paymentLabels[payment.kind] ?? payment.kind} ${payment.amount_cents != null
        ? yuan(payment.amount_cents)
        : range(payment.amount_min_cents == null ? null : { min: payment.amount_min_cents, max: payment.amount_max_cents }, yuan)}`)));
      content.firstChild.append(list);
    }
    return content;
  }
  function figures(values) {
    return el("dl", "figures", ...values.map(([label, value]) => el("div", "", el("dt", "", label), el("dd", "", String(value)))));
  }
  function mealCounts(stats) {
    return [...Object.entries(modes).map(([key, label]) => [label, `${stats.mode_counts[key]} 餐`]), ["奶茶", `${stats.milk_tea_purchases.count} 笔`]];
  }
  function addDays(day, n) {
    return localDateValue(new Date(new Date(`${day}T00:00:00+08:00`).getTime() + n * 86400000));
  }
  async function loadToday() {
    const date = localDateValue();
    const weekday = (new Date(`${date}T00:00:00Z`).getUTCDay() + 6) % 7;
    const jobs = [
      ["#offer-list", reloadCompare()],
      ["#meals-peek", callAction("meal_list", { ...dataWindow(date), limit: 6 }).then(({ items }) =>
        fill($("#meals-peek"), items.map(mealRow), "今天还没有用餐记录。"))],
      ["#stock-peek", Promise.all([callAction("inventory_batch_list", { as_of: date }), callAction("leftover_list", { as_of: date, status: "available" })]).then(([stock, leftovers]) => {
        const batches = stock.items.filter(item => !item.expired && Number(item.quantity) > 0).map(item => row(item.ingredient, item.location, `${item.quantity} ${item.unit}`));
        const dishes = leftovers.items.filter(item => !item.expired && item.servings > 0).map(item => row(item.name, "剩菜", `${item.servings} 份`));
        fill($("#stock-peek"), batches.concat(dishes).slice(0, 5), "暂无可用食材或剩菜。");
      })],
      ["#plans-peek", Promise.all([callAction("menu_plan_list", dataWindow(date)), callAction("recipe_list", { limit: 200 })]).then(([plans, recipes]) => {
        const names = new Map(recipes.items.map(recipe => [recipe.recipe_id, recipe.name]));
        const groups = new Map();
        for (const plan of plans.items) {
          if (!groups.has(plan.meal_type)) groups.set(plan.meal_type, []);
          groups.get(plan.meal_type).push(`${names.get(plan.recipe_id) ?? "已保存菜谱"}（${plan.servings} 份）`);
        }
        fill($("#plans-peek"), [...groups].map(([slot, dishes]) => row(dishes.join("、"), mealTypes[slot])), "今天还没安排菜单。");
      })],
      ["#week-peek", callAction("meal_stats", dataWindow(addDays(date, -weekday), date)).then(result =>
        $("#week-peek").replaceChildren(figures(mealCounts(result))))],
      ["#next-reminder", callAction("meal_reminder_list", {}).then(({ items }) => showNextReminder(items, new Date()))]
    ];
    const done = await Promise.allSettled(jobs.map(([, job]) => job));
    done.forEach((outcome, index) => {
      if (outcome.status === "rejected") fill($(jobs[index][0]), [], errorText(outcome.reason));
    });
  }

  function dishNames(details) {
    const dishes = Array.isArray(details.dishes) ? details.dishes : [details.dish];
    return dishes.map(item => typeof item === "string" ? item : item?.name).filter(Boolean);
  }
  function candidateFields() {
    const form = $("#candidate-form");
    const mode = input(form, "mode").value;
    const stages = ui.candidate?.mode === mode ? ui.candidate.details.time_profile?.stages : {};
    $("#candidate-stages").replaceChildren(...stageKeys[mode].map(key => {
      const control = el("input");
      control.name = `stage_${key}`;
      control.type = "number";
      control.min = "0";
      control.max = "1440";
      const saved = stages?.[key];
      if (saved?.min_minutes != null && saved.min_minutes === saved.max_minutes) control.value = saved.min_minutes;
      else if (saved) control.placeholder = "沿用已有区间";
      return el("label", "", el("span", "", stageNames[key]), control);
    }));
  }
  function editCandidate(candidate) {
    ui.candidate = candidate;
    const form = $("#candidate-form");
    form.reset();
    input(form, "payment").placeholder = "";
    $("#candidate-title").textContent = candidate ? "编辑候选" : "添加候选";
    $("#candidate-cancel").hidden = !candidate;
    if (candidate) {
      input(form, "mode").value = candidate.mode;
      input(form, "name").value = candidate.name;
      input(form, "dishes").value = dishNames(candidate.details).join("\n");
      input(form, "store_name").placeholder = candidate.details.store_id ? "沿用已有店铺" : "";
      const profile = candidate.details.cost_profile;
      if (profile?.payment?.min_cents != null && profile.payment.min_cents === profile.payment.max_cents) {
        input(form, "payment").placeholder = `当前 ${yuan(profile.payment.min_cents)}`;
      }
      input(form, "name").focus();
    } else {
      input(form, "store_name").placeholder = "";
      input(form, "payment").placeholder = "";
    }
    candidateFields();
  }
  async function loadFavorites() {
    const [candidates, recipes, prefs] = await Promise.all([
      callAction("candidate_list", { limit: 200 }), callAction("recipe_list", { limit: 200 }),
      callAction("recommendation_preference_list", { limit: 200 })
    ]);
    const entries = [
      ...candidates.items.map(item => ({ ...item, id: item.candidate_id, source_type: "candidate" })),
      ...recipes.items.map(item => ({ ...item, id: item.recipe_id, source_type: "recipe", mode: "home_cooking" }))
    ];
    const active = new Map();
    for (const item of prefs.items.filter(item => item.active)) {
      const key = `${item.source_type}:${item.source_id}`;
      if (!active.has(key)) active.set(key, new Set());
      active.get(key).add(item.preference);
    }
    const filter = $("#favorites-filter").value;
    const rows = [];
    for (const item of entries) {
      const flags = active.get(`${item.source_type}:${item.id}`) ?? new Set();
      if (item.preference === "like") flags.add("liked");
      if (filter && !flags.has(filter)) continue;
      const note = [modes[item.mode], dishNames(item.details).join("、"), ...[...flags].map(flag => preferenceLabels[flag])];
      const controls = el("div", "offer-actions");
      if (item.source_type === "candidate") controls.append(button("编辑", () => editCandidate(item)));
      for (const key of Object.keys(preferenceLabels)) {
        if (key === "liked" && item.preference === "like" && !active.get(`${item.source_type}:${item.id}`)?.has("liked")) {
          controls.append(button("取消收藏", async control => {
            await mutate(control, "candidate_upsert", {
              candidate_id: item.id, expected_version: item.version, mode: item.mode,
              name: item.name, details: item.details, preference: "neutral"
            });
            await loadFavorites();
            notice("已取消收藏。");
          }));
        } else controls.append(preferenceButton(item, key, flags.has(key), loadFavorites));
      }
      rows.push(row(item.name, note.filter(Boolean).join(" · "), controls));
    }
    fill($("#favorite-list"), rows, filter ? "没有匹配的记录。" : "还没有候选或菜谱。");
  }
  handleForm("#candidate-form", async (data, form) => {
    const old = ui.candidate;
    const mode = field(data, "mode");
    const savedDishes = Array.isArray(old?.details.dishes) ? old.details.dishes : [];
    const dishes = field(data, "dishes").split("\n").map(dish => dish.trim()).filter(Boolean);
    const details = {
      ...(old?.details ?? {}),
      dishes: dishes.map(dish => savedDishes.find(saved => (typeof saved === "string" ? saved : saved?.name) === dish) ?? dish)
    };
    const stageChanges = {};
    for (const key of stageKeys[mode]) {
      const value = field(data, `stage_${key}`);
      if (value === "") continue;
      const minutes = Number(value);
      const saved = old?.mode === mode ? details.time_profile?.stages?.[key] : null;
      if (saved?.min_minutes === minutes && saved.max_minutes === minutes) continue;
      stageChanges[key] = { min_minutes: minutes, max_minutes: minutes };
    }
    if (old && old.mode !== mode) delete details.time_profile;
    if (Object.keys(stageChanges).length) {
      details.time_profile = { ...details.time_profile, source: "estimate", stages: { ...details.time_profile?.stages, ...stageChanges } };
    }
    if (field(data, "payment") !== "") {
      const value = cents(field(data, "payment"));
      details.cost_profile = {
        ...details.cost_profile, payment: { min_cents: value, max_cents: value, source: "estimate" },
        payment_coverage_people_count: Number(field(data, "people_count") || details.cost_profile?.payment_coverage_people_count || 1)
      };
    }
    if (field(data, "store_name")) {
      const constraint = { name: field(data, "store_name") };
      if (field(data, "store_branch")) constraint.branch = field(data, "store_branch");
      const stores = await callAction("store_search", constraint);
      const ids = [...new Set(stores.items.map(item => item.canonical_store.store_id))];
      if (ids.length !== 1) throw new Error(ids.length ? "有多家同名店。请填写分店。" : "没查到已有店铺。请核对店名。");
      details.store_id = ids[0];
    }
    await mutate(form, "candidate_upsert", {
      ...(old ? { candidate_id: old.candidate_id, expected_version: old.version } : {}),
      mode, name: field(data, "name"), preference: old?.preference ?? "neutral", details
    });
    editCandidate(null);
    await loadFavorites();
    notice("候选已保存。");
  });

  function recipeIngredients(ingredients) {
    return ingredients?.map(item => typeof item === "string" ? item :
      [item.name, item.scaled_quantity ?? item.quantity ?? "未记录用量", item.unit].filter(Boolean).join(" ")) ?? [];
  }
  function previewRecipe(preview, args) {
    const output = $("#recipe-preview");
    const recipe = preview.details;
    const details = [recipe.servings ? `${recipe.servings} 份` : "份数未记录", preview.source_kind === "ai_generated" ? "模型生成" : "导入预览"];
    const actions = button("确认保存菜谱", async control => {
      await mutate(control, "recipe_import_commit", { ...args, preview_signature: preview.preview_signature, confirm: true });
      output.replaceChildren();
      await loadRecipes();
      notice("菜谱已保存。");
    }, "primary");
    output.replaceChildren(row(preview.name, details.join(" · ")),
      el("ul", "", ...recipeIngredients(recipe.ingredient_amounts).map(value => el("li", "", value))),
      el("ol", "", ...recipe.steps.map(step => el("li", "", [step.title, step.instruction].filter(Boolean).join(" · ")))),
      actions
    );
  }
  async function loadRecipes() {
    const [recipes, providers] = await Promise.all([callAction("recipe_list", { limit: 200 }), callAction("recipe_provider_status", {})]);
    const nodes = recipes.items.map(item => {
      const title = el("summary", "", item.name);
      const details = el("details", "data-row", title);
      const ingredients = recipeIngredients(item.details.ingredient_amounts ?? item.details.ingredients);
      const steps = (Array.isArray(item.details.steps) ? item.details.steps : []).map(step =>
        typeof step === "string" ? step : [step.title, step.instruction].filter(Boolean).join(" · "));
      const feedback = item.latest_feedback?.reason;
      if (feedback) details.append(el("p", "section-note", `上次反馈 · ${feedback}`));
      if (ingredients.length) details.append(el("ul", "", ...ingredients.map(value => el("li", "", value))));
      if (steps.length) details.append(el("ol", "", ...steps.map(value => el("li", "", value))));
      const servings = el("input");
      servings.type = "number";
      servings.min = "1";
      servings.value = item.details.servings ?? 2;
      const output = el("p", "section-note");
      const scale = button("换算", async () => {
        if (!servings.reportValidity() || !servings.value) return;
        const result = await callAction("recipe_scale", { recipe_id: item.recipe_id, target_servings: Number(servings.value) });
        output.textContent = (result.ingredients ?? []).map(value =>
          `${value.name} ${value.scaled_quantity ?? "未记录用量"}${value.scaled_quantity != null ? ` ${value.unit}` : ""}`
        ).join("、") || "还没有可换算的食材。";
      });
      details.append(el("div", "tool-form", el("label", "", el("span", "", "换算份数"), servings), scale), output);
      return details;
    });
    fill($("#recipe-list"), nodes, "还没有保存的菜谱。");
    const options = input($("#recipe-provider-form"), "provider");
    options.replaceChildren();
    if (providers.baidu.ready) options.add(new Option("百度菜谱搜索", "baidu"));
    if (providers.model.ready) options.add(new Option("模型生成草稿", "model"));
    $("#provider-area").hidden = options.options.length === 0;
  }
  handleForm("#recipe-import", async data => {
    const args = { source_kind: field(data, "source_kind"), source_text: field(data, "source_text") };
    if (field(data, "source_url")) args.source_url = field(data, "source_url");
    previewRecipe(await callAction("recipe_import_preview", args), args);
  });
  handleForm("#recipe-provider-form", async data => {
    const query = field(data, "query");
    if (field(data, "provider") === "model") {
      const model = await callAction("recipe_model_preview", { prompt: query });
      previewRecipe(model.preview, model.import_arguments);
      return;
    }
    const { items } = await callAction("recipe_baidu_search", { query });
    fill($("#provider-output"), items.map(item => {
      const resultLink = link(item.title ?? "查看来源", item.url);
      resultLink.target = "_blank";
      resultLink.rel = "noopener noreferrer";
      return el("div", "data-row", resultLink, el("small", "", item.snippet ?? ""));
    }), "没有找到带链接的菜谱。");
  });

  async function loadKitchen() {
    const day = localDateValue();
    const [stock, leftovers, plans, recipes] = await Promise.all([
      callAction("inventory_batch_list", { as_of: day }), callAction("leftover_list", { as_of: day }),
      callAction("menu_plan_list", { date_from: day, limit: 100 }), callAction("recipe_list", { limit: 200 })
    ]);
    fill($("#inventory-list"), stock.items.map(item => row(item.ingredient,
      [item.location, item.expires_on ? `到期 ${item.expires_on}` : "", item.expired ? "已过期" : ""].filter(Boolean).join(" · "),
      `${item.quantity} ${item.unit}`
    )), "还没有库存批次。");
    const leftoverStates = { available: "可用", used_up: "已吃完", discarded: "已丢弃" };
    fill($("#leftover-list"), leftovers.items.map(item =>
      row(item.name, [item.expired ? "已过期" : leftoverStates[item.status], item.use_by].filter(Boolean).join(" · "), `${item.servings} 份`)
    ), "还没有剩菜记录。");
    const recipeInput = input($("#plan-form"), "recipe_id");
    const selected = recipeInput.value;
    recipeInput.replaceChildren(new Option("选择菜谱", ""), ...recipes.items.map(item => new Option(item.name, item.recipe_id)));
    if (recipes.items.some(item => item.recipe_id === selected)) recipeInput.value = selected;
    $("#plan-form button[type=submit]").disabled = recipes.items.length === 0;
    const names = new Map(recipes.items.map(item => [item.recipe_id, item.name]));
    const checked = new Set([...document.querySelectorAll("#plan-list input:checked")].map(control => control.value));
    fill($("#plan-list"), plans.items.map(item => {
      const choice = el("input");
      choice.type = "checkbox";
      choice.value = item.plan_id;
      choice.checked = checked.has(item.plan_id);
      return el("label", "data-row check-line", choice, el("span", "", `${item.planned_date} · ${mealTypes[item.meal_type]} · ${names.get(item.recipe_id) ?? "已保存菜谱"} · ${item.servings} 份`));
    }), "未来还没有安排。");
    $("#build-shopping").disabled = plans.items.length === 0;
    const listId = localStorage.getItem("shixu-last-shopping");
    if (listId) {
      try { showShopping(await callAction("shopping_list_get", { shopping_list_id: listId })); }
      catch (error) {
        if (error.code !== "NOT_FOUND") throw error;
        localStorage.removeItem("shixu-last-shopping");
        showShopping(null);
      }
    } else showShopping(null);
  }
  function stockExpiryFields(data) {
    const days = field(data, "shelf_life_days");
    return {
      location: field(data, "location") || null,
      expires_on: field(data, "expires_on") || null,
      ...(days ? { shelf_life_days: Number(days) } : {})
    };
  }
  function shelfLifeLookup(ingredientName, locationField, output) {
    let sequence = 0;
    const refresh = async () => {
      const current = ++sequence;
      const ingredient = ingredientName();
      output.hidden = true;
      if (!ingredient) return;
      try {
        const result = await callAction("inventory_shelf_life_get", { ingredient, location: locationField.value.trim() || null });
        if (current !== sequence) return;
        output.textContent = result.shelf_life_days == null
          ? "这个存放位置还没有保质天数，可填写本批到期日或保存天数。"
          : `该位置已保存 ${result.shelf_life_days} 天。今天入库时，留空会用 ${result.expires_on}。`;
      } catch (error) {
        if (current !== sequence) return;
        output.textContent = errorText(error);
      }
      output.hidden = false;
    };
    locationField.addEventListener("change", refresh);
    return refresh;
  }
  function shoppingExpiry(ingredient) {
    const location = el("input");
    location.name = "location";
    const date = el("input");
    date.name = "expires_on";
    date.type = "date";
    const days = el("input");
    days.name = "shelf_life_days";
    days.type = "number";
    days.min = "0";
    days.step = "1";
    const form = el("form", "tool-form", el("label", "", el("span", "", "位置"), location),
      el("label", "", el("span", "", "本批到期日"), date),
      el("label", "", el("span", "", "保质天数（保存供下次用）"), days));
    form.addEventListener("submit", event => event.preventDefault());
    const hint = el("p", "section-note");
    hint.setAttribute("aria-live", "polite");
    hint.hidden = true;
    const details = el("details", "", el("summary", "", "存放位置和保质期"), form, hint);
    const refresh = shelfLifeLookup(() => ingredient, location, hint);
    details.addEventListener("toggle", () => { if (details.open) refresh(); });
    return { form, details };
  }
  function showShopping(list) {
    const output = $("#shopping-list");
    if (!list) return fill(output, [], "从菜单计划选择菜谱，生成购物清单。");
    const items = list.items.filter(item => !item.quantity_known || Number(item.quantity_to_buy) > 0 || item.status === "bought");
    fill(output, items.map(item => {
      const bought = item.status === "bought";
      const hasPurchases = item.purchases.length > 0;
      const needsAmount = !bought && !item.quantity_known && !hasPurchases;
      const quantity = el("input");
      const unit = el("input");
      if (needsAmount) {
        quantity.type = "number";
        quantity.min = "0.001";
        quantity.step = "any";
        quantity.required = unit.required = true;
        unit.value = item.unit ?? "";
      }
      const covered = hasPurchases && !Number(item.remaining_quantity);
      const expiry = !bought && !covered ? shoppingExpiry(item.ingredient) : null;
      const label = bought ? "重新打开" : covered ? "标记已买" : "已买入库";
      const control = button(label, async control => {
        const amount = {};
        if (needsAmount) {
          unit.value = unit.value.trim();
          if (!quantity.reportValidity() || !unit.reportValidity()) return;
          Object.assign(amount, { quantity: quantity.value, unit: unit.value });
        }
        if (expiry && !expiry.form.reportValidity()) return;
        let result;
        try {
          result = await mutate(control, "shopping_list_mark_bought", {
            shopping_list_id: list.shopping_list_id, item_id: item.item_id, expected_version: item.version, bought: !bought,
            ...amount, ...(expiry ? stockExpiryFields(new FormData(expiry.form)) : {})
          });
        } catch (error) {
          if (expiry && error.code === "SHELF_LIFE_REQUIRED") expiry.details.open = true;
          throw error;
        }
        await loadKitchen();
        if (result.inventory_updated) notice("采购已入库。");
      });
      const details = [item.quantity_known ? `剩余 ${item.remaining_quantity} ${item.unit ?? ""}` : "清单用量待确认"];
      if (item.quantity_purchased !== "0") details.push(`实买 ${item.quantity_purchased} ${item.unit}`);
      else if (hasPurchases) details.push(`实买 ${item.purchases.map(purchase => `${purchase.quantity} ${purchase.unit}`).join("、")}`);
      if (bought) details.push("已买");
      const amountFields = needsAmount ? el("div", "tool-form",
        el("label", "", el("span", "", "实买数量"), quantity),
        el("label", "", el("span", "", "实买单位"), unit)
      ) : null;
      const actions = expiry ? el("div", "purchase-fields", ...(amountFields ? [amountFields] : []), expiry.details, control) : control;
      return row(item.ingredient, details.join(" · "), actions);
    }), "库存已覆盖已知需求。");
  }
  const inventoryForm = $("#inventory-form");
  const ingredientField = input(inventoryForm, "ingredient");
  const refreshInventoryExpiry = shelfLifeLookup(() => ingredientField.value.trim(), input(inventoryForm, "location"), $("#inventory-shelf-life"));
  ingredientField.addEventListener("change", refreshInventoryExpiry);
  handleForm("#inventory-form", async (data, form) => {
    const item = Object.fromEntries(["ingredient", "quantity", "unit"].map(name => [name, field(data, name)]));
    await mutate(form, "inventory_batch_upsert", { ...item, ...stockExpiryFields(data), source: "网页入库" });
    form.reset();
    refreshInventoryExpiry();
    await loadKitchen();
    notice("库存已登记。");
  });
  handleForm("#leftover-form", async (data, form) => {
    await mutate(form, "leftover_upsert", {
      name: field(data, "name"), servings: Number(field(data, "servings")), prepared_on: field(data, "prepared_on"),
      use_by: field(data, "use_by") || null, status: "available"
    });
    input(form, "name").value = "";
    await loadKitchen();
    notice("剩菜已登记。");
  });
  handleForm("#plan-form", async (data, form) => {
    await mutate(form, "menu_plan_upsert", {
      planned_date: field(data, "planned_date"), meal_type: field(data, "meal_type"),
      recipe_id: field(data, "recipe_id"), servings: Number(field(data, "servings"))
    });
    await loadKitchen();
    notice("已加入菜单。同一餐可以继续添加其他菜谱。");
  });
  $("#build-shopping").addEventListener("click", async event => {
    const control = event.currentTarget;
    try {
      const plan_ids = [...document.querySelectorAll("#plan-list input:checked")].map(item => item.value);
      if (!plan_ids.length) throw new Error("先勾选要采购的菜谱计划。");
      control.disabled = true;
      const list = await mutate(control, "shopping_list_build", { plan_ids });
      localStorage.setItem("shixu-last-shopping", list.shopping_list_id);
      showShopping(await callAction("shopping_list_get", { shopping_list_id: list.shopping_list_id }));
      notice("购物清单已生成。");
    } catch (error) { notice(errorText(error), true); }
    finally { control.disabled = false; }
  });

  function totalValue(value) {
    const estimate = value.total_range_cents;
    return yuan(value.total_cents) + (value.total_cents == null && estimate
      ? ` · 估算 ${range({ min: estimate.min_cents, max: estimate.max_cents }, yuan)}` : "");
  }
  async function loadReport(data = new FormData($("#report-form"))) {
    const report = await callAction("meal_report", { period: field(data, "period"), anchor: field(data, "anchor") });
    const frequency = report.frequency;
    const last = frequency.last_meal;
    const period = el("p", "report-period", `${report.date_from} 至 ${report.date_to} · ${report.total_meals} 餐${last ? ` · 最近一餐 ${last.date}` : ""}`);
    const budget = report.budget.budget?.budget_cents;
    const spending = figures([
      ["整期付款", totalValue(report.spending.payment)], ["食用成本", totalValue(report.spending.consumption_cost)],
      ["预算", budget == null ? "未设" : yuan(budget)], ["剩余预算", budget == null ? "—" : yuan(report.budget.balance.remaining_cents)]
    ]);
    const pair = el("div", "overview-pair");
    for (const [label, entries] of [["常吃的菜", frequency.common_dishes], ["常去的店", frequency.common_stores]]) {
      const rows = el("div", "rows");
      fill(rows, entries.map(item => row([item.name, item.branch].filter(Boolean).join(" · "), item.last_meal?.date, `${item.meal_count} 餐`)), "本周期暂无记录。");
      pair.append(el("section", "report-block", el("h2", "", label), rows));
    }
    const insights = el("section", "report-block", el("h2", "", "近期饮食"));
    const tags = Object.entries(report.diet.tag_counts).map(([name, count]) => `${name} ${count} 餐`);
    if (tags.length) insights.append(el("p", "section-note", tags.join(" · ")));
    fill(insights.appendChild(el("div", "rows")), report.diet.suggestions.map(item => row(item.message, item.basis)), "本周期暂无饮食建议。");
    const meals = el("div", "rows");
    fill(meals, report.meals.map(item => row(item.dishes.join("、") || "未记录菜品", `${item.date} · ${mealTypes[item.meal_type]} · ${modes[item.mode]}`)), "本周期还没有用餐记录。");
    $("#report-output").replaceChildren(period, figures(mealCounts(frequency)), spending, pair, insights,
      el("section", "report-block", el("h2", "", report.total_meals > 30 ? "最近 30 餐" : "周期餐次"), meals));
  }
  handleForm("#report-form", loadReport);

  handleForm("#store-find", async data => {
    $("#store-profile").hidden = true;
    const { items } = await callAction("store_search", { name: field(data, "name") });
    fill($("#store-results"), items.map(item => row(item.name, item.branch, button("编辑场合", async () => {
      ui.store = await callAction("store_profile_get", { store_id: item.store_id });
      const form = $("#store-profile");
      input(form, "environment").value = ui.store.profile?.environment ?? "";
      input(form, "occasions").value = ui.store.profile?.occasions.join("、") ?? "";
      input(form, "note").value = ui.store.profile?.note ?? "";
      form.hidden = false;
    }))), "没查到同名店铺。");
  });
  handleForm("#store-profile", async (data, form) => {
    ui.store = await mutate(form, "store_profile_set", {
      store_id: ui.store.store.store_id, ...(ui.store.profile ? { expected_version: ui.store.profile.version } : {}),
      environment: field(data, "environment") || null, occasions: parts(field(data, "occasions")), note: field(data, "note") || null
    });
    notice("店铺场合已保存。");
  });
  async function loadPrices() {
    const { items } = await callAction("price_snapshot_list", { limit: 12 });
    fill($("#price-list"), items.map(item => row(item.ingredient,
      `${item.region} · ${item.source} · ${item.recorded_on}`, `${yuan(item.price_cents)} / ${item.package_quantity} ${item.unit}`
    )), "还没有菜价快照。");
  }
  handleForm("#price-form", async (data, form) => {
    const args = Object.fromEntries(["ingredient", "package_quantity", "unit", "region", "recorded_on", "source"].map(name => [name, field(data, name)]));
    await mutate(form, "price_snapshot_add", { ...args, price_cents: cents(field(data, "price")) });
    await loadPrices();
    notice("价格快照已保存。");
  });

  const loaders = { today: loadToday, recipes: loadRecipes, kitchen: loadKitchen, favorites: loadFavorites, reports: loadReport, settings: loadPrices };
  async function navigate() {
    const section = location.hash.slice(1);
    const next = Object.hasOwn(loaders, section) ? section : "today";
    if (next === ui.page) return;
    ui.page = next;
    document.querySelectorAll("[data-panel]").forEach(panel => { panel.hidden = panel.dataset.panel !== next; });
    document.querySelectorAll("[data-view]").forEach(link => {
      if (link.dataset.view === next) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
    notice("");
    try { await loaders[next](); } catch (error) { if (ui.page === next) notice(errorText(error), true); }
  }

  const now = new Date();
  const header = $("#today-date");
  header.dateTime = localDateValue(now);
  header.textContent = dateText.format(now);
  input(compareForm, "meal_type").value = chosenSlot();
  input($("#report-form"), "anchor").value = localDateValue(now);
  input($("#plan-form"), "planned_date").value = localDateValue(now);
  input($("#leftover-form"), "prepared_on").value = localDateValue(now);
  input($("#price-form"), "recorded_on").value = localDateValue(now);
  input($("#candidate-form"), "mode").addEventListener("change", candidateFields);
  $("#candidate-cancel").addEventListener("click", () => editCandidate(null));
  $("#favorites-filter").addEventListener("change", () => loadFavorites().catch(error => notice(errorText(error), true)));
  handleForm("#compare-form", reloadCompare);
  candidateFields();
  window.addEventListener("hashchange", navigate);
  window.addEventListener("pageshow", event => {
    if (event.persisted && ui.page === "today") loadToday();
  });
  navigate();
})();
