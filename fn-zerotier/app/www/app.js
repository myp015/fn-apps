const nodeIdEl = document.getElementById("nodeId");
const versionEl = document.getElementById("version");
const onlineStateEl = document.getElementById("onlineState");
const peerSummaryEl = document.getElementById("peerSummary");
const serviceEnabledEl = document.getElementById("serviceEnabled");
const networkCountEl = document.getElementById("networkCount");
const networkListEl = document.getElementById("networkList");
const networksEmptyEl = document.getElementById("networksEmpty");
const joinedMoonCountEl = document.getElementById("joinedMoonCount");
const joinedMoonListEl = document.getElementById("joinedMoonList");
const joinedMoonsEmptyEl = document.getElementById("joinedMoonsEmpty");
const createdMoonCountEl = document.getElementById("createdMoonCount");
const createdMoonListEl = document.getElementById("createdMoonList");
const createdMoonsEmptyEl = document.getElementById("createdMoonsEmpty");
const moonForm = document.getElementById("moonForm");
const moonJoinWorldIdInput = document.getElementById("moonWorldId");
const moonSeedInput = document.getElementById("moonSeed");
const moonCreateSeedEl = document.getElementById("moonCreateSeed");
const moonRootIdentityEl = document.getElementById("moonRootIdentity");
const moonCreateSupportEl = document.getElementById("moonCreateSupport");
const moonCreateForm = document.getElementById("moonCreateForm");
const moonCreateWorldIdInput = document.getElementById("moonCreateWorldIdInput");
const moonStableEndpointsInput = document.getElementById("moonStableEndpoints");
const moonCreateCancelBtn = document.getElementById("moonCreateCancelBtn");
const moonCreateBtn = document.getElementById("moonCreateBtn");
const peerListEl = document.getElementById("peerList");
const refreshBtn = document.getElementById("refreshBtn");
const joinForm = document.getElementById("joinForm");
const networkIdInput = document.getElementById("networkId");
const toastEl = document.getElementById("toast");

const NETWORK_STATUS_META = {
  OK: { className: "pill-ok", text: "正常" },
  ACCESS_DENIED: { className: "pill-warn", text: "访问被拒绝" },
  REQUESTING_CONFIGURATION: { className: "pill-warn", text: "等待配置" },
};

const MOON_TABLE_HEADER_HTML = `
  <div class="moon-created-header">
    <span>World ID</span>
    <span>Seed</span>
    <span>Root Identity</span>
    <span>Stable Endpoints</span>
    <span>状态</span>
    <span>操作</span>
  </div>
`;

let busy = false;
let toastTimer = null;
let latestStatusData = null;
let editingCreatedMoonId = "";

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

function escapeHtml(value) {
  return String(value || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
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

async function copyText(text, successMessage) {
  if (!text) {
    showToast("没有可复制的内容", "error");
    return;
  }

  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      const input = document.createElement("textarea");
      input.value = text;
      document.body.appendChild(input);
      input.select();
      document.execCommand("copy");
      document.body.removeChild(input);
    }
    showToast(successMessage, "success");
  } catch (error) {
    showToast("复制失败", "error");
  }
}

function toBool(value) {
  if (typeof value === "boolean") return value;
  if (typeof value === "number") return value !== 0;
  const normalized = String(value || "").trim().toLowerCase();
  return normalized === "1" || normalized === "true" || normalized === "yes" || normalized === "on";
}

function renderService(data) {
  const service = data.service || {};
  const info = data.info || {};
  const peerSummary = data.peerSummary || {};

  const serviceActive = toBool(service.active);
  const serviceEnabled = toBool(service.enabled);

  nodeIdEl.textContent = info.address || "-";
  versionEl.textContent = info.version || "-";
  onlineStateEl.textContent = serviceActive ? "运行中" : "未运行";
  peerSummaryEl.textContent = `${peerSummary.online || 0} / ${peerSummary.total || 0}`;
  serviceEnabledEl.textContent = serviceEnabled ? "已启用" : "未启用";
}

function networkStatusClass(status) {
  return NETWORK_STATUS_META[status]?.className || "pill-muted";
}

function networkStatusText(status) {
  return NETWORK_STATUS_META[status]?.text || status || "未知";
}

function checkedAttr(value) {
  return toBool(value) ? "checked" : "";
}

function isValidMoonWorldId(value) {
  return /^(?:[0-9a-fA-F]{10}|0{6}[0-9a-fA-F]{10})$/.test(String(value || "").trim());
}

function extractSeedFromIdentity(identity) {
  const normalized = String(identity || "").trim().toLowerCase();
  if (/^[0-9a-f]{10}$/.test(normalized)) {
    return normalized;
  }
  const match = normalized.match(/^([0-9a-f]{10}):/);
  return match ? match[1] : "";
}

function resetMoonJoinForm() {
  moonJoinWorldIdInput.value = "";
  moonSeedInput.value = "";
}

function resetMoonCreateForm() {
  editingCreatedMoonId = "";
  moonCreateBtn.textContent = "创建";
  moonCreateCancelBtn.hidden = true;
  moonCreateWorldIdInput.value = "";
  moonStableEndpointsInput.value = "";
}

function moonEndpoints(moon) {
  const roots = Array.isArray(moon?.roots) ? moon.roots : [];
  return roots.flatMap((root) => (Array.isArray(root?.stableEndpoints) ? root.stableEndpoints : [])).filter(Boolean);
}

function getCreatedMoonById(worldId) {
  const moons = Array.isArray(latestStatusData?.createdMoons) ? latestStatusData.createdMoons : [];
  return moons.find((moon) => moon.id === worldId) || null;
}

function getMoonRowData(moon) {
  const roots = Array.isArray(moon?.roots) ? moon.roots : [];
  const identity = roots.length ? roots[0].identity || "" : "";
  const seed = roots.length ? extractSeedFromIdentity(identity) : "";
  const endpoints = moonEndpoints(moon);
  return {
    identity: identity || "-",
    seed: seed || "-",
    endpointsText: endpoints.length ? endpoints.join(", ") : "-",
  };
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
        ? network.assignedAddresses.join(", ")
        : "尚未分配地址";
      return `
        <article class="network-table-row">
          <div class="network-table-col network-table-col-name">
            <strong>${name}</strong>
          </div>
          <div class="network-table-col network-table-col-id">
            <strong>${nwid}</strong>
          </div>
          <div class="network-table-col network-table-col-type">
            <strong>${network.type || "-"}</strong>
          </div>
          <div class="network-table-col network-table-col-device">
            <strong>${network.portDeviceName || network.dev || "-"}</strong>
          </div>
          <div class="network-table-col network-table-col-addresses">
            <strong>${addresses}</strong>
          </div>
          <div class="network-table-col network-table-col-status">
            <span class="pill ${networkStatusClass(network.status)}">${networkStatusText(network.status)}</span>
          </div>
          <div class="network-table-col network-table-col-settings">
            <div class="network-flags" data-network="${nwid}">
              <label><input type="checkbox" data-setting="allowManaged" ${checkedAttr(network.allowManaged)}> Managed</label>
              <label><input type="checkbox" data-setting="allowDNS" ${checkedAttr(network.allowDNS)}> DNS</label>
              <label><input type="checkbox" data-setting="allowDefault" ${checkedAttr(network.allowDefault)}> Default Route</label>
              <label><input type="checkbox" data-setting="allowGlobal" ${checkedAttr(network.allowGlobal)}> Global IP</label>
            </div>
          </div>
          <div class="network-table-col network-table-col-actions">
            <div class="network-actions">
              <button class="btn btn-danger" type="button" data-action="leave-network" data-network="${nwid}">退出</button>
            </div>
          </div>
        </article>
      `;
    })
    .join("");

  networkListEl.innerHTML = `
    <div class="network-table-header">
      <span>名称</span>
      <span>Network ID</span>
      <span>类型</span>
      <span>设备</span>
      <span>地址</span>
      <span>状态</span>
      <span>设置</span>
      <span>操作</span>
    </div>
    ${networkListEl.innerHTML}
  `;
}


function renderMoonTable(container, rowsHtml) {
  container.innerHTML = `${MOON_TABLE_HEADER_HTML}${rowsHtml}`;
}

function renderMoonSection(moons, countEl, emptyEl, listEl, countLabel, rowRenderer) {
  countEl.textContent = `${countLabel} ${moons.length}`;
  emptyEl.style.display = moons.length ? "none" : "block";

  if (!moons.length) {
    listEl.innerHTML = "";
    return;
  }

  renderMoonTable(listEl, moons.map(rowRenderer).join(""));
}

function renderJoinedMoonRow(moon) {
  const active = moon.active !== undefined ? toBool(moon.active) : true;
  const waiting = toBool(moon.waiting);
  const { identity, seed, endpointsText } = getMoonRowData(moon);
  const stateClass = !active ? "pill-muted" : waiting ? "pill-warn" : "pill-ok";
  const stateText = !active ? "已停止" : waiting ? "等待拉取" : "已生效";
  return `
    <article class="moon-created-row">
      <div class="moon-created-col moon-created-col-id">
          <strong>${moon.id || "-"}</strong>
      </div>
      <div class="moon-created-col moon-created-col-seed">
          <strong>${seed}</strong>
      </div>
      <div class="moon-created-col moon-created-col-identity">
        <strong>${identity}</strong>
      </div>
      <div class="moon-created-col moon-created-col-endpoints">
        <strong>${endpointsText}</strong>
      </div>
      <div class="moon-created-col moon-created-col-state">
        <span class="pill ${stateClass}">${stateText}</span>
      </div>
      <div class="moon-created-col moon-created-col-actions">
        <button class="btn btn-danger" type="button" data-action="leave-joined-moon" data-world-id="${moon.id || ""}">移除</button>
      </div>
    </article>
  `;
}

function renderCreatedMoonRow(moon) {
  const active = toBool(moon.active);
  const { identity, seed, endpointsText } = getMoonRowData(moon);
  const orbitCommand = moon.orbitCommand || (moon.id && seed !== "-" ? `zerotier-cli orbit ${moon.id} ${seed}` : "");
  const moonFileBase64 = moon.moonFileBase64 || "";
  const moonFileName = moon.moonFileName || `${String(moon.id || "moon")}.moon`;
  return `
    <article class="moon-created-row">
      <div class="moon-created-col moon-created-col-id">
          <strong>${moon.id || "-"}</strong>
      </div>
      <div class="moon-created-col moon-created-col-seed">
          <strong>${seed}</strong>
      </div>
      <div class="moon-created-col moon-created-col-identity">
        <strong>${identity}</strong>
      </div>
      <div class="moon-created-col moon-created-col-endpoints">
        <strong>${endpointsText}</strong>
      </div>
      <div class="moon-created-col moon-created-col-state">
          <span class="pill ${active ? "pill-ok" : "pill-muted"}">${active ? "已启动" : "已停止"}</span>
      </div>
      <div class="moon-created-col moon-created-col-actions">
        <button class="btn btn-secondary" type="button" data-action="copy-created-moon-orbit" data-orbit-command="${escapeHtml(orbitCommand)}" ${orbitCommand ? "" : "disabled"}>复制</button>
        <button class="btn btn-secondary" type="button" data-action="download-created-moon" data-moon-file-base64="${escapeHtml(moonFileBase64)}" data-moon-file-name="${escapeHtml(moonFileName)}" ${moonFileBase64 ? "" : "disabled"}>下载</button>
        <button class="btn btn-secondary" type="button" data-action="edit-created-moon" data-world-id="${moon.id || ""}">修改</button>
        <button class="btn ${active ? "btn-secondary" : "btn-primary"}" type="button" data-action="${active ? "stop-created-moon" : "start-created-moon"}" data-world-id="${moon.id || ""}">${active ? "停止" : "启动"}</button>
        <button class="btn btn-danger" type="button" data-action="remove-created-moon" data-world-id="${moon.id || ""}">移除</button>
      </div>
    </article>
  `;
}

function renderJoinedMoons(data) {
  const moons = Array.isArray(data.joinedMoons) ? data.joinedMoons : [];
  renderMoonSection(moons, joinedMoonCountEl, joinedMoonsEmptyEl, joinedMoonListEl, "加入", renderJoinedMoonRow);
}

function renderCreatedMoons(data) {
  const moons = Array.isArray(data.createdMoons) ? data.createdMoons : [];
  renderMoonSection(moons, createdMoonCountEl, createdMoonsEmptyEl, createdMoonListEl, "创建", renderCreatedMoonRow);
}

function renderMoonCreator(data) {
  const info = data.moonCreate || {};
  const supported = toBool(info.supported);
  const error = info.error || "";

  moonCreateSeedEl.textContent = info.seed || "-";
  moonRootIdentityEl.textContent = info.rootIdentity || error || "-";
  moonCreateSupportEl.textContent = supported ? (editingCreatedMoonId ? "编辑中" : "待创建") : "不可用";
  moonCreateSupportEl.className = supported
    ? `pill ${editingCreatedMoonId ? "pill-warn" : "pill-muted"}`
    : "pill pill-muted";
  moonCreateBtn.disabled = !supported;
  moonCreateWorldIdInput.disabled = !supported;
  moonStableEndpointsInput.disabled = !supported;

  if (!moonSeedInput.value && info.seed) {
    moonSeedInput.value = info.seed;
  }
  if (!moonJoinWorldIdInput.value && info.worldId) {
    moonJoinWorldIdInput.value = info.worldId;
  }
  if (!editingCreatedMoonId && !moonCreateWorldIdInput.value && info.worldId) {
    moonCreateWorldIdInput.value = info.worldId;
  }
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
  latestStatusData = data;
  renderService(data);
  renderMoonCreator(data);
  renderJoinedMoons(data);
  renderCreatedMoons(data);
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
    return data;
  } catch (error) {
    showToast(error.message || String(error), "error");
    return null;
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

moonForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const worldId = (moonJoinWorldIdInput.value || "").trim();
  const seed = (moonSeedInput.value || "").trim();
  if (!isValidMoonWorldId(worldId)) {
    showToast("World ID 必须是 10 位十六进制，或补零后的 16 位字符串", "error");
    moonJoinWorldIdInput.focus();
    return;
  }
  if (!/^[0-9a-fA-F]{10}$/.test(seed)) {
    showToast("Seed 必须是 10 位十六进制字符串", "error");
    moonSeedInput.focus();
    return;
  }
  const data = await postAction("moon_join", { worldId, seed }, `已加入 moon ${worldId}`);
  if (data) {
    resetMoonJoinForm();
  }
});

moonCreateCancelBtn.addEventListener("click", () => {
  resetMoonCreateForm();
});

moonCreateForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const worldId = (moonCreateWorldIdInput.value || "").trim();
  const stableEndpoints = (moonStableEndpointsInput.value || "").trim();
  if (!isValidMoonWorldId(worldId)) {
    showToast("World ID 必须是 10 位十六进制，或补零后的 16 位字符串", "error");
    moonCreateWorldIdInput.focus();
    return;
  }
  if (!stableEndpoints) {
    showToast("请至少填写一个 Stable Endpoint", "error");
    moonStableEndpointsInput.focus();
    return;
  }

  const action = editingCreatedMoonId ? "moon_update" : "moon_create";
  const payload = editingCreatedMoonId
    ? { oldWorldId: editingCreatedMoonId, worldId, stableEndpoints }
    : { worldId, stableEndpoints };
  const successMessage = editingCreatedMoonId ? `已修改 moon ${worldId}` : `已创建 moon ${worldId}`;
  const data = await postAction(action, payload, successMessage);
  if (data) {
    resetMoonCreateForm();
  }
});

document.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;

  const action = button.getAttribute("data-action");
  const network = button.getAttribute("data-network") || "";
  const worldId = button.getAttribute("data-world-id") || "";
  const orbitCommand = button.getAttribute("data-orbit-command") || "";
  const moonFileBase64 = button.getAttribute("data-moon-file-base64") || "";
  const moonFileName = button.getAttribute("data-moon-file-name") || "";
  if (action === "leave-network") {
    await postAction("leave", { network }, `已离开网络 ${network}`);
    return;
  }

  if (action === "edit-created-moon") {
    const moon = getCreatedMoonById(worldId);
    if (!moon) {
      showToast(`未找到已创建 moon ${worldId}`, "error");
      return;
    }
    editingCreatedMoonId = worldId;
    moonCreateBtn.textContent = "修改";
    moonCreateCancelBtn.hidden = false;
    moonCreateWorldIdInput.value = moon.id || "";
    moonStableEndpointsInput.value = moonEndpoints(moon).join("\n");
    moonCreateSupportEl.textContent = "编辑中";
    moonCreateSupportEl.className = "pill pill-warn";
    moonCreateWorldIdInput.focus();
    return;
  }

  if (action === "copy-created-moon-orbit") {
    await copyText(orbitCommand, "Orbit 命令已复制");
    return;
  }

  if (action === "download-created-moon") {
    if (!moonFileBase64) {
      showToast("当前没有可下载的 .moon 文件", "error");
      return;
    }
    const link = document.createElement("a");
    link.href = `data:application/octet-stream;base64,${moonFileBase64}`;
    link.download = moonFileName || "moon.moon";
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    return;
  }

  if (action === "start-created-moon") {
    await postAction("moon_start", { worldId }, `已启动 moon ${worldId}`);
    return;
  }

  if (action === "stop-created-moon") {
    await postAction("moon_stop", { worldId }, `已停止 moon ${worldId}`);
    if (editingCreatedMoonId && editingCreatedMoonId === worldId) {
      resetMoonCreateForm();
    }
    return;
  }

  if (action === "remove-created-moon") {
    await postAction("moon_remove", { worldId }, `已移除 moon ${worldId}`);
    if (editingCreatedMoonId && editingCreatedMoonId === worldId) {
      resetMoonCreateForm();
    }
    return;
  }

  if (action === "leave-joined-moon") {
    await postAction("moon_leave", { worldId }, `已移除 moon ${worldId}`);
    return;
  }
});

document.addEventListener("change", async (event) => {
  const checkbox = event.target.closest("input[data-setting]");
  if (!checkbox) return;
  const panel = checkbox.closest(".network-table-row");
  if (!panel) return;
  const network = panel.querySelector(".network-flags")?.getAttribute("data-network") || "";
  const setting = checkbox.getAttribute("data-setting") || "";
  if (!network) return;
  if (!setting) return;
  await postAction("network_set", { network, settings: { [setting]: checkbox.checked } }, `网络 ${network} 设置已保存`);
});

refreshStatus();
