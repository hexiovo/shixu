"use strict";

const mealTypes = { breakfast: "早餐", lunch: "午餐", dinner: "晚餐", snack: "加餐", other: "其他" };
const modes = { eat_out: "外食", delivery: "外卖", home_cooking: "在家做饭" };
const shixuTimeZone = "Asia/Shanghai";

function localDateValue(now = new Date()) {
  return new Intl.DateTimeFormat("sv-SE", { timeZone: shixuTimeZone }).format(now);
}

function requestId() {
  return crypto.randomUUID();
}

async function callAction(action, payload) {
  let response;
  try {
    response = await fetch("/api/action", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, payload })
    });
  } catch (error) {
    error.uncertain = true;
    throw error;
  }
  let body;
  try {
    body = await response.json();
  } catch (error) {
    error.uncertain = true;
    throw error;
  }
  if (!response.ok || !body.ok) {
    const error = new Error(body.error.message);
    error.code = body.error.code;
    error.details = body.error.details;
    throw error;
  }
  return body.result;
}
