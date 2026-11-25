"use strict";

(() => {
  const reminderPanel = document.querySelector("#reminder-panel");
  const reminderForm = document.querySelector("#reminder-form");
  const reminderRules = document.querySelector("#reminder-rules");
  const reminderMessage = document.querySelector("#reminder-message");
  const pendingReminders = document.querySelector("#pending-reminders");
  const notificationButton = document.querySelector("#allow-notifications");
  const logPanel = document.querySelector("#local-log-panel");
  const logStatus = document.querySelector("#local-log-status");
  const logLines = document.querySelector("#local-log-lines");
  const restoreLog = document.querySelector("#restore-local-log");
  const weekdays = ["一", "二", "三", "四", "五", "六", "日"];
  let refreshing = false;
  let previousPending = "";

  function localButton(label, onClick) {
    const button = document.createElement("button");
    button.className = "button button-quiet";
    button.type = "button";
    button.textContent = label;
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        await onClick();
      } catch (error) {
        reminderPanel.open = true;
        reminderMessage.textContent = error.message;
      } finally {
        button.disabled = false;
      }
    });
    return button;
  }

  async function renderRules() {
    const { items } = await callAction("meal_reminder_list", {});
    reminderRules.replaceChildren();
    if (!items.length) {
      const li = document.createElement("li");
      li.textContent = "暂无提醒。";
      reminderRules.append(li);
    }
    for (const rule of items) {
      const row = document.createElement("li");
      const days = rule.weekdays.length === 7 ? "每天" : `周${rule.weekdays.map(day => weekdays[day]).join("、")}`;
      const description = [mealTypes[rule.meal_type], rule.at, days, rule.note, rule.enabled ? "" : "已停用"];
      row.append(description.filter(Boolean).join(" · "), localButton(rule.enabled ? "停用" : "启用", async () => {
        await callAction("meal_reminder_set", {
          request_id: requestId(), rule_id: rule.rule_id, expected_version: rule.version,
          meal_type: rule.meal_type, at: rule.at, weekdays: rule.weekdays, note: rule.note, enabled: !rule.enabled
        });
        await renderRules();
        await refreshPending();
      }));
      reminderRules.append(row);
    }
  }

  reminderPanel.addEventListener("toggle", () => {
    if (reminderPanel.open) renderRules().catch(error => { reminderMessage.textContent = error.message; });
  });
  reminderForm.addEventListener("submit", async event => {
    event.preventDefault();
    const submit = reminderForm.querySelector("button[type=submit]");
    const data = new FormData(reminderForm);
    submit.disabled = true;
    try {
      await callAction("meal_reminder_set", {
        request_id: requestId(), meal_type: data.get("meal_type"), at: data.get("at"),
        weekdays: data.get("days").split(",").map(Number), note: data.get("note").trim() || null
      });
      reminderMessage.textContent = "提醒已保存。";
      await renderRules();
    } catch (error) {
      reminderMessage.textContent = error.message;
    } finally {
      submit.disabled = false;
    }
  });

  function updateNotificationButton() {
    const permission = "Notification" in window ? Notification.permission : "unavailable";
    notificationButton.disabled = permission !== "default";
    notificationButton.textContent = ({
      default: "开启浏览器通知", granted: "浏览器通知已开启",
      denied: "浏览器已阻止通知", unavailable: "浏览器通知不可用"
    })[permission];
  }
  notificationButton.addEventListener("click", async () => {
    try {
      await Notification.requestPermission();
      await refreshPending();
    } catch (error) {
      reminderMessage.textContent = error.message;
    }
    updateNotificationButton();
  });

  async function refreshPending() {
    if (refreshing) return;
    refreshing = true;
    try {
      let pending;
      try {
        pending = await callAction("meal_reminder_pending", {});
      } catch (error) {
        if (error.code !== "invalid_session") throw error;
        await fetch("/", { cache: "no-store" });
        pending = await callAction("meal_reminder_pending", {});
      }
      const items = pending.items;
      const fingerprint = items.map(item => item.event_id).join(",");
      if (fingerprint !== previousPending) {
        pendingReminders.replaceChildren();
        pendingReminders.hidden = items.length === 0;
        for (const item of items) {
          const row = document.createElement("div");
          const time = new Date(item.scheduled_at).toLocaleString([], { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
          row.append(`${time} ${mealTypes[item.meal_type]}提醒${item.note ? ` · ${item.note}` : ""}`, localButton("知道了", async () => {
            await callAction("meal_reminder_ack", { request_id: requestId(), event_id: item.event_id });
            await refreshPending();
          }));
          pendingReminders.append(row);
        }
        previousPending = fingerprint;
      }
      if ("Notification" in window && Notification.permission === "granted") {
        for (const item of items) {
          if (item.claimed_at) continue;
          const { claimed } = await callAction("meal_reminder_claim", { request_id: requestId(), event_id: item.event_id });
          if (claimed) new Notification(`食序 · ${mealTypes[item.meal_type]}提醒`, { body: item.note || "", tag: item.event_id });
        }
      }
    } catch (error) {
      if (reminderPanel.open) reminderMessage.textContent = error.message;
    } finally {
      refreshing = false;
    }
  }

  async function refreshLog() {
    try {
      const status = await callAction("local_panel_status", {});
      logStatus.textContent = `服务运行中 · 本机端口 ${status.port}`;
      logLines.textContent = status.lines.join("\n") || "暂无日志。";
    } catch (error) {
      logStatus.textContent = error.message;
    }
  }
  function setLogVisibility(visible) {
    logPanel.hidden = !visible;
    restoreLog.hidden = visible;
    localStorage.setItem("shixu-log-hidden", visible ? "0" : "1");
  }
  logPanel.hidden = localStorage.getItem("shixu-log-hidden") === "1";
  restoreLog.hidden = !logPanel.hidden;
  logPanel.open = localStorage.getItem("shixu-log-open") === "1";
  logPanel.addEventListener("toggle", () => {
    localStorage.setItem("shixu-log-open", logPanel.open ? "1" : "0");
    if (logPanel.open && !logPanel.hidden) refreshLog();
  });
  logPanel.querySelector("summary").addEventListener("contextmenu", event => {
    event.preventDefault();
    setLogVisibility(false);
  });
  document.querySelector("#hide-local-log").addEventListener("click", () => setLogVisibility(false));
  document.querySelector("#refresh-local-log").addEventListener("click", refreshLog);
  restoreLog.addEventListener("click", () => {
    setLogVisibility(true);
    if (logPanel.open) refreshLog();
    else logPanel.open = true;
  });

  updateNotificationButton();
  refreshPending();
  setInterval(refreshPending, 20000);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") refreshPending();
  });
})();
