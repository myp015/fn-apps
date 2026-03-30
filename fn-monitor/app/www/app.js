const fields = [
  "HandlePowerKey",
  "HandlePowerKeyLongPress",
  "HandleRebootKey",
  "HandleRebootKeyLongPress",
  "HandleSuspendKey",
  "HandleSuspendKeyLongPress",
  "HandleHibernateKey",
  "HandleHibernateKeyLongPress",
  "HandleLidSwitch",
  "HandleLidSwitchExternalPower",
  "HandleLidSwitchDocked",
];

const msgEl = document.getElementById("msg");
const rawEl = document.getElementById("raw");
const monitorListEl = document.getElementById("monitorList");
const screenRefreshBtn = document.getElementById("screenRefresh");
const saveBtn = document.getElementById("save");
const refreshBtn = document.getElementById("refresh");
const applyRestartEl = document.getElementById("applyRestart");

let msgTimer = null;
let monitorBusy = false;

function showMsg(text, timeout = 3000) {
  if (!msgEl) return;
  if (msgTimer) {
    window.clearTimeout(msgTimer);
    msgTimer = null;
  }
  msgEl.textContent = String(text || "");
  msgEl.classList.add("show");
  if (timeout > 0) {
    msgTimer = window.setTimeout(() => {
      msgEl.classList.remove("show");
      msgTimer = null;
    }, timeout);
  }
}

function escapeHtml(value) {
  return String(value || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

async function requestJson(url, options = {}) {
  const response = await fetch(url, { cache: "no-store", ...options });
  const text = await response.text();
  let json = null;
  try {
    json = text ? JSON.parse(text) : {};
  } catch (error) {
    json = null;
  }
  if (!response.ok || !json) {
    throw new Error(text || "请求失败");
  }
  if (json.ok === false) {
    throw new Error(json.error || json.message || "请求失败");
  }
  return json;
}

function ensureAria() {
  for (const id of fields) {
    const el = document.getElementById(id);
    if (!el || el.getAttribute("aria-label")) continue;
    const label = document.querySelector(`label[for="${id}"]`);
    if (label && label.textContent) {
      el.setAttribute("aria-label", label.textContent.trim());
    }
  }
}

function setMonitorBusy(busy) {
  monitorBusy = !!busy;
  if (screenRefreshBtn) screenRefreshBtn.disabled = monitorBusy;
  if (!monitorListEl) return;
  for (const btn of monitorListEl.querySelectorAll("button[data-action]")) {
    if (btn.getAttribute("data-action") === "refresh") {
      btn.disabled = monitorBusy;
      continue;
    }
    if (monitorBusy) {
      btn.disabled = true;
    } else if (btn.dataset.disabledByControl === "true") {
      btn.disabled = true;
    }
  }
}

function monitorMetaText(connector) {
  const parts = [
    "连接=" + (connector.status || "unknown"),
    "enabled=" + (connector.enabled || "unknown"),
  ];

  return parts.join(" · ");
}

function renderMonitorList(connectors) {
  if (!monitorListEl) return;

  const list = Array.isArray(connectors) ? connectors : [];
  if (!list.length) {
    monitorListEl.innerHTML = '<div class="monitor-empty">未检测到显示器连接器</div>';
    return;
  }

  const html = list
    .map((connector) => {
      const connectorName = connector.sys_name || connector.name || "";
      const title = connector.name || connector.sys_name || "unknown";
      const state = connector.state || "unknown";
      const controllable = !!connector.controllable;
      const reason = connector.control_reason || "该显示器当前不可控";
      const stateClass =
        state === "on" ? "state-on" : state === "off" ? "state-off" : "state-unknown";
      const disabledAttr = controllable ? "" : 'disabled aria-disabled="true" data-disabled-by-control="true"';
      const disabledTitle = controllable ? "" : ` title="${escapeHtml(reason)}"`;

      return `
        <div class="monitor-item">
          <div class="monitor-main">
            <span class="state-pill ${stateClass}">${escapeHtml(state)}</span>
            <div class="monitor-title">${escapeHtml(title)}</div>
            <div class="monitor-meta">${escapeHtml(monitorMetaText(connector))}</div>
          </div>
          <div class="monitor-side">
            <div class="monitor-actions">
              <button type="button" data-action="on" data-connector="${escapeHtml(connectorName)}" ${disabledAttr}${disabledTitle}>开启</button>
              <button type="button" data-action="off" data-connector="${escapeHtml(connectorName)}" ${disabledAttr}${disabledTitle}>关闭</button>
              <button type="button" data-action="refresh" data-connector="${escapeHtml(connectorName)}">刷新</button>
            </div>
          </div>
        </div>
      `;
    })
    .join("");

  monitorListEl.innerHTML = html;
  setMonitorBusy(monitorBusy);
}

async function readConfig() {
  showMsg("读取中...", 1200);
  try {
    const result = await requestJson("../www/api.cgi?action=read");
    const parsed = result.parsed || {};
    for (const key of fields) {
      const el = document.getElementById(key);
      if (el) el.value = parsed[key] || "";
    }
    if (rawEl) rawEl.textContent = result.content || "";
    showMsg("已加载", 1000);
  } catch (error) {
    showMsg("读取失败: " + (error.message || error), 4000);
  }
}

async function saveConfig(event) {
  if (event && typeof event.preventDefault === "function") event.preventDefault();
  if (saveBtn) saveBtn.disabled = true;

  const changes = {};
  for (const key of fields) {
    const el = document.getElementById(key);
    if (!el) continue;
    changes[key] = (el.value || "").trim();
  }

  showMsg("保存中...", 2000);
  try {
    await requestJson("../www/api.cgi?action=write", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        changes,
        apply: !!(applyRestartEl && applyRestartEl.checked),
      }),
    });
    showMsg("保存成功", 1800);
    await readConfig();
  } catch (error) {
    showMsg("保存失败: " + (error.message || error), 4500);
  } finally {
    if (saveBtn) saveBtn.disabled = false;
  }
}

async function loadScreenStatus(connector = "") {
  try {
    const result = await requestJson(
      "../www/api.cgi?action=screen_status&connector=" + encodeURIComponent(connector || "")
    );
    renderMonitorList(result.connectors || []);
  } catch (error) {
    if (monitorListEl) {
      monitorListEl.innerHTML =
        '<div class="monitor-empty">状态读取失败，请检查 DRM / modetest / 权限</div>';
    }
    showMsg("状态读取失败: " + (error.message || error), 4000);
  }
}

async function screenAction(state, connector) {
  setMonitorBusy(true);
  showMsg("正在设置屏幕...", 2000);
  try {
    const result = await requestJson(
      "../www/api.cgi?action=screen&state=" +
        encodeURIComponent(state) +
        "&connector=" +
        encodeURIComponent(connector || "")
    );

    renderMonitorList(result.connectors || []);
    showMsg(`已执行 ${state} (${connector || "default"})`, 2200);
    await loadScreenStatus(connector);
  } catch (error) {
    showMsg("操作失败: " + (error.message || error), 4500);
  } finally {
    setMonitorBusy(false);
  }
}

function bindEvents() {
  if (screenRefreshBtn) {
    screenRefreshBtn.addEventListener("click", () => loadScreenStatus(""));
  }

  if (monitorListEl) {
    monitorListEl.addEventListener("click", (event) => {
      const btn =
        event.target && event.target.closest
          ? event.target.closest("button[data-action][data-connector]")
          : null;
      if (!btn || btn.disabled) return;

      const action = btn.getAttribute("data-action");
      const connector = btn.getAttribute("data-connector") || "";
      if (action === "refresh") {
        loadScreenStatus(connector);
        return;
      }
      if (action === "on" || action === "off") {
        screenAction(action, connector);
      }
    });
  }

  if (saveBtn) saveBtn.addEventListener("click", saveConfig);
  if (refreshBtn) {
    refreshBtn.addEventListener("click", (event) => {
      if (event && typeof event.preventDefault === "function") event.preventDefault();
      readConfig();
    });
  }
}

ensureAria();
bindEvents();
readConfig();
loadScreenStatus();
