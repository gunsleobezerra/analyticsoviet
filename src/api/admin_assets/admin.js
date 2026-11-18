"use strict";

/* Panel shell. It talks only to /admin/api on this origin, and echoes the CSRF
   cookie in the header of every write, which is what the server checks. */

const CSRF_COOKIE = "admin_csrf";
const CSRF_HEADER = "X-CSRF-Token";

const statusNode = document.getElementById("status");
const loginForm = document.getElementById("login-form");
const panel = document.getElementById("panel");
const whoNode = document.getElementById("who");
const pendingNode = document.getElementById("pending");
const passwordForm = document.getElementById("password-form");
const logoutButton = document.getElementById("logout");

function cookieValue(name) {
  const entries = document.cookie ? document.cookie.split("; ") : [];
  for (const entry of entries) {
    const separator = entry.indexOf("=");
    if (separator > 0 && entry.slice(0, separator) === name) {
      return decodeURIComponent(entry.slice(separator + 1));
    }
  }
  return "";
}

function setStatus(message, isError) {
  statusNode.textContent = message;
  statusNode.dataset.state = isError ? "error" : "info";
}

async function call(path, options) {
  const settings = Object.assign({method: "GET"}, options || {});
  const headers = Object.assign({Accept: "application/json"}, settings.headers || {});
  if (settings.body !== undefined) {
    headers["Content-Type"] = "application/json";
  }
  if (settings.method !== "GET") {
    headers[CSRF_HEADER] = cookieValue(CSRF_COOKIE);
  }
  const response = await fetch(path, {
    method: settings.method,
    headers: headers,
    body: settings.body,
    credentials: "same-origin",
  });
  let payload = null;
  try {
    payload = await response.json();
  } catch (error) {
    payload = null;
  }
  return {status: response.status, payload: payload};
}

function failureText(result, fallback) {
  if (result.payload && result.payload.error && result.payload.error.message) {
    return result.payload.error.message;
  }
  return fallback;
}

function showLogin(message) {
  panel.hidden = true;
  loginForm.hidden = false;
  if (message) {
    setStatus(message, true);
  }
  document.getElementById("username").focus();
}

function showPanel(session) {
  loginForm.hidden = true;
  panel.hidden = false;
  whoNode.textContent = session.username;
  pendingNode.hidden = !session.must_change_password;
  setStatus(session.must_change_password ? "Troca de senha pendente." : "Sessão ativa.", false);
}

async function loadSession() {
  const result = await call("/admin/api/session");
  if (result.status === 200 && result.payload) {
    showPanel(result.payload);
    return;
  }
  showLogin("");
}

loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const body = JSON.stringify({
    username: document.getElementById("username").value,
    password: document.getElementById("password").value,
  });
  const result = await call("/admin/api/login", {method: "POST", body: body});
  if (result.status === 200) {
    document.getElementById("password").value = "";
    await loadSession();
    return;
  }
  showLogin(failureText(result, "Não foi possível entrar."));
});

passwordForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const body = JSON.stringify({
    current_password: document.getElementById("current-password").value,
    new_password: document.getElementById("new-password").value,
  });
  const result = await call("/admin/api/password", {method: "POST", body: body});
  document.getElementById("current-password").value = "";
  document.getElementById("new-password").value = "";
  if (result.status === 200) {
    showLogin("Senha alterada. Entre de novo com a nova senha.");
    return;
  }
  setStatus(failureText(result, "Não foi possível trocar a senha."), true);
});

logoutButton.addEventListener("click", async () => {
  await call("/admin/api/logout", {method: "POST"});
  showLogin("Sessão encerrada.");
});

loadSession();
