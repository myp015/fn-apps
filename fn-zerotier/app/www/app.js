const serviceBadge = document.getElementById("serviceBadge");
const nodeIdEl = document.getElementById("nodeId");
const versionEl = document.getElementById("version");
const onlineStateEl = document.getElementById("onlineState");
const peerSummaryEl = document.getElementById("peerSummary");
const servicePortEl = document.getElementById("servicePort");
const serviceEnabledEl = document.getElementById("serviceEnabled");
const assignedAddressesEl = document.getElementById("assignedAddresses");
const networkCountEl = document.getElementById("networkCount");
const networkListEl = document.getElementById("networkList");
const networksEmptyEl = document.getElementById("networksEmpty");
const peerListEl = document.getElementById("peerList");
const refreshBtn = document.getElementById("refreshBtn");
const joinForm = document.getElementById("joinForm");
const networkIdInput = document.getElementById("networkId");
const toastEl = document.getElementById("toast");

let busy = false;
let toastTimer = null;

function showToast(text, tone = "info") {
  if (!toastEl) return;
  toastEl.textContent = String(text || "");
  toastEl.dataset.tone = tone;
  toastEl.classList.add("show");
  if (toastTimer) window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => {
    toastEl.classList.remove("show");
  }, 3200);
}

async function requestJson(url, options = {}) {
  const response = await fetch(url, {
    cache: "no-store",
    ...options,
  });
  const text = await response.text();
  let data = {};
  try {
    data = text ? JSON.parse(text) : {};
  } catch (error) {
    throw new Error(text || "返回结果不是合法 JSON");
  }
  if (!response.ok || data.ok === false) {
    throw new Error(data.error || text || "请求失败");
  }
  return data;
}

function setBusy(nextBusy) {
  busy = !!nextBusy;
  refreshBtn.disabled = busy;
  for (const button of document.querySelectorAll("button")) {
    if (button === refreshBtn) continue;
    button.disabled = busy;
  }
}

function renderService(data) {
  const service = data.service || {};
  const info = data.info || {};
  const networks = Array.isArray(data.networks) ? data.networks : [];
  const peerSummary = data.peerSummary || {};
  const online = String(info.online || "").toUpperCase();

  serviceBadge.textContent = service.active ? "运行中" : service.installed ? "已停止" : "未安装";
  serviceBadge.className = `badge ${service.active ? "badge-ok" : service.installed ? "badge-warn" : "badge-muted"}`;

  nodeIdEl.textContent = info.address || "-";
  versionEl.textContent = info.version || "-";
  onlineStateEl.textContent = online || "-";
  peerSummaryEl.textContent = `${peerSummary.online || 0} / ${peerSummary.total || 0}`;
  servicePortEl.textContent = info.port || "-";
  serviceEnabledEl.textContent = service.enabled ? "已启用" : "未启用";

  const addresses = networks
    .flatMap((item) => (Array.isArray(item.assignedAddresses) ? item.assignedAddresses : []))
    .filter(Boolean);
  assignedAddressesEl.textContent = addresses.length ? addresses.join(", ") : "-";
}

function networkStatusClass(status) {
  if (status === "OK") return "pill-ok";
  if (status === "REQUESTING_CONFIGURATION" || status === "ACCESS_DENIED") return "pill-warn";
  return "pill-muted";
}

function checkedAttr(value) {
  return value ? "checked" : "";
}

function formatPeerPathEntry(entry) {
  if (!entry) return "-";
  if (typeof entry === "string") return entry;
  if (typeof entry !== "object") return String(entry);

  const parts = [];
  if (entry.address) parts.push(entry.address);
  if (entry.ip) parts.push(entry.ip);
  if (entry.port) parts.push(String(entry.port));
  if (entry.preferred === true) parts.push("preferred");
  if (entry.trustedPathId) parts.push(`tpid:${entry.trustedPathId}`);
  if (entry.lastSend) parts.push(`tx:${entry.lastSend}`);
  if (entry.lastReceive) parts.push(`rx:${entry.lastReceive}`);

  return parts.length ? parts.join(" ") : JSON.stringify(entry);
}

function formatPeerPath(peer) {
  if (Array.isArray(peer.paths) && peer.paths.length) {
    return peer.paths.map(formatPeerPathEntry).join(" | ");
  }
  if (peer.path) {
    return formatPeerPathEntry(peer.path);
  }
  return "-";
}

function renderNetworks(data) {
  const networks = Array.isArray(data.networks) ? data.networks : [];
  networkCountEl.textContent = `${networks.length} 个网络`;
  networksEmptyEl.style.display = networks.length ? "none" : "block";

  if (!networks.length) {
    networkListEl.innerHTML = "";
    return;
  }

  networkListEl.innerHTML = networks
    .map((network) => {
      const name = network.name || "未命名网络";
      const nwid = network.nwid || "-";
      const addresses = Array.isArray(network.assignedAddresses) && network.assignedAddresses.length
        ? network.assignedAddresses.join("<br>")
        : "尚未分配地址";
      return `
        <article class="network-card">
          <div class="network-top">
            <div>
              <h3>${name}</h3>
              <p class="network-id">${nwid}</p>
            </div>
            <span class="pill ${networkStatusClass(network.status)}">${network.status || "UNKNOWN"}</span>
          </div>
          <div class="network-body">
            <div class="network-meta">
              <div><span>类型</span><strong>${network.type || "-"}</strong></div>
              <div><span>设备</span><strong>${network.portDeviceName || network.dev || "-"}</strong></div>
              <div><span>地址</span><strong>${addresses}</strong></div>
            </div>
            <div class="network-flags" data-network="${nwid}">
              <label><input type="checkbox" data-setting="allowManaged" ${checkedAttr(network.allowManaged)}> Managed</label>
              <label><input type="checkbox" data-setting="allowDNS" ${checkedAttr(network.allowDNS)}> DNS</label>
              <label><input type="checkbox" data-setting="allowDefault" ${checkedAttr(network.allowDefault)}> Default Route</label>
              <label><input type="checkbox" data-setting="allowGlobal" ${checkedAttr(network.allowGlobal)}> Global IP</label>
            </div>
            <div class="network-actions">
              <button class="btn btn-secondary" type="button" data-action="save-network" data-network="${nwid}">保存设置</button>
              <button class="btn btn-danger" type="button" data-action="leave-network" data-network="${nwid}">退出网络</button>
            </div>
          </div>
        </article>
      `;
    })
    .join("");
}

function renderPeers(data) {
  const peers = Array.isArray(data.peers) ? data.peers : [];
  if (!peers.length) {
    peerListEl.innerHTML = '<div class="empty-state">还没有可展示的 Peer 数据。</div>';
    return;
  }

  peerListEl.innerHTML = peers
    .map((peer) => {
      const online = typeof peer.latency === "number" && peer.latency >= 0;
      const latency = online ? `${peer.latency} ms` : "-";
      const path = formatPeerPath(peer);
      return `
        <div class="peer-item">
          <span class="peer-cell">
            <span class="peer-key">ztaddr</span>
            <span class="peer-field peer-addr">${peer.address || "-"}</span>
          </span>
          <span class="peer-cell">
            <span class="peer-key">ver</span>
            <span class="peer-field">${peer.version || "-"}</span>
          </span>
          <span class="peer-cell">
            <span class="peer-key">role</span>
            <span class="peer-field">${peer.role || "-"}</span>
          </span>
          <span class="peer-cell">
            <span class="peer-key">lat</span>
            <span class="peer-field">
              <span class="pill ${online ? "pill-ok" : "pill-muted"}">${latency}</span>
            </span>
          </span>
          <span class="peer-cell">
            <span class="peer-key">link</span>
            <span class="peer-field">${peer.link || "-"}</span>
          </span>
          <span class="peer-cell">
            <span class="peer-key">lastTX</span>
            <span class="peer-field">${peer.lastTX || "-"}</span>
          </span>
          <span class="peer-cell">
            <span class="peer-key">lastRX</span>
            <span class="peer-field">${peer.lastRX || "-"}</span>
          </span>
          <span class="peer-cell peer-cell-path">
            <span class="peer-key">path</span>
            <span class="peer-field peer-path">${path}</span>
          </span>
        </div>
      `;
    })
    .join("");
}

function renderAll(data) {
  renderService(data);
  renderNetworks(data);
  renderPeers(data);
}

async function refreshStatus(showMessage = false) {
  try {
    const data = await requestJson("../www/api.cgi?action=status");
    renderAll(data);
    if (showMessage) showToast("状态已刷新", "success");
  } catch (error) {
    showToast(error.message || String(error), "error");
  }
}

async function postAction(action, payload, successMessage) {
  setBusy(true);
  try {
    const data = await requestJson(`../www/api.cgi?action=${encodeURIComponent(action)}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify(payload || {}),
    });
    renderAll(data);
    showToast(successMessage || data.message || "操作成功", "success");
  } catch (error) {
    showToast(error.message || String(error), "error");
  } finally {
    setBusy(false);
  }
}

refreshBtn.addEventListener("click", () => refreshStatus(true));

joinForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const network = (networkIdInput.value || "").trim();
  if (!/^[0-9a-fA-F]{16}$/.test(network)) {
    showToast("Network ID 必须是 16 位十六进制字符串", "error");
    networkIdInput.focus();
    return;
  }
  await postAction("join", { network }, `已发起加入网络 ${network}`);
  networkIdInput.value = "";
});

document.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;

  const action = button.getAttribute("data-action");
  const network = button.getAttribute("data-network") || "";
  if (action === "leave-network") {
    await postAction("leave", { network }, `已离开网络 ${network}`);
    return;
  }

  if (action === "save-network") {
    const panel = button.closest(".network-card");
    const settings = {};
    for (const checkbox of panel.querySelectorAll("input[data-setting]")) {
      settings[checkbox.dataset.setting] = checkbox.checked;
    }
    await postAction("network_set", { network, settings }, `网络 ${network} 设置已保存`);
  }
});

refreshStatus();
