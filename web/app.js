"use strict";

const $ = id => document.getElementById(id);
const fmtSpeed = b => b == null ? "" : b >= 1048576 ? (b / 1048576).toFixed(1) + " MB/s"
  : b >= 1024 ? (b / 1024).toFixed(0) + " KB/s" : b + " B/s";
const fmtUptime = s => s >= 86400 ? Math.floor(s / 86400) + "d " + Math.floor(s % 86400 / 3600) + "h"
  : s >= 3600 ? Math.floor(s / 3600) + "h " + Math.floor(s % 3600 / 60) + "m" : Math.floor(s / 60) + "m";
let currentUser = null;
let pollers = [];

async function j(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) {
    let d = "";
    try {
      d = (await r.json()).detail || r.status;
    } catch {
      d = r.status;
    }
    if (r.status === 401 && !url.endsWith("/api/auth/login")) showLogin();
    throw new Error(d);
  }
  return r.json();
}

function appendLine(container, label, value) {
  if (container.childNodes.length) container.appendChild(document.createElement("br"));
  container.appendChild(document.createTextNode(String(label) + String(value ?? "")));
}

function appendSafeLink(container, label, rawUrl) {
  try {
    const displayUrl = String(rawUrl ?? "");
    const url = new URL(displayUrl, window.location.href);
    if (url.protocol !== "http:" && url.protocol !== "https:") return;
    if (container.childNodes.length) container.appendChild(document.createElement("br"));
    container.appendChild(document.createTextNode(String(label) + ": "));
    const link = document.createElement("a");
    link.href = url.href;
    link.textContent = displayUrl;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    container.appendChild(link);
  } catch {
    // Invalid and non-web URLs are intentionally not rendered as links.
  }
}

async function refreshStatus() {
  try {
    const s = await j("/api/status");
    const g = s.gateway;
    $("subline").textContent = `gateway v${g.version} · up ${fmtUptime(g.uptime_s)}` +
      (currentUser && currentUser.is_admin
        ? ` · movies: ${g.movies_dir} · tv: ${g.tv_dir}` +
          (g.sort_delete_torrent ? " · torrent deleted after sort" : "") : "");
    $("save-chip").textContent = currentUser && currentUser.is_admin
      ? "downloads → " + g.downloads_dir : "managed by Jellyfin Gateway";

    const jf = s.jellyfin;
    $("jf-dot").className = "dot " + (jf.online ? "ok" : "");
    $("jf-state").textContent = jf.online ? "Online" : "Offline";
    const jfKv = $("jf-kv");
    jfKv.replaceChildren();
    appendLine(jfKv, "url: ", jf.url);
    if (jf.online) {
      if (jf.version) appendLine(jfKv, "", `${jf.server_name || "Jellyfin"} · v${jf.version}`);
      if (jf.item_count != null) appendLine(jfKv, "", `${jf.item_count} library items`);
      if (jf.lan_url) appendSafeLink(jfKv, "local", jf.lan_url);
      if (jf.tailscale_url) appendSafeLink(jfKv, "tailscale", jf.tailscale_url);
    } else {
      appendLine(jfKv, "", jf.error || "not reachable");
    }

    const q = s.qbittorrent;
    const qUnsupported = q.online && q.supported === false;
    $("qb-dot").className = "dot " +
      (qUnsupported ? "warn" : (q.online ? "ok" : ""));
    $("qb-state").textContent = qUnsupported ? "Unsupported" :
      (q.online ? "Online" : "Offline");
    let kv = "url: " + (q.url ?? "");
    if (q.online) {
      kv += "\nv" + (q.version || "?") + " · " + (q.connection || "");
      if (qUnsupported) {
        kv += "\n⚠ " + (q.error ||
          "qBittorrent 5.x is required (v" + (q.version || "?") + " detected)");
        kv += "\nAdding torrents is disabled until qBittorrent is updated to 5.x";
      } else {
        kv += "\n↓ " + fmtSpeed(q.dl_speed) + "  ↑ " + fmtSpeed(q.up_speed) +
              "  · DHT " + (q.dht_nodes ?? 0);
        const t = q.torrents || {};
        let torrentLine = t.total + " torrents · " + (t.downloading || 0) +
              " downloading · " + (t.seeding || 0) + " seeding · " +
              (t.stopped || 0) + " stopped";
        if (t.other) torrentLine += " · " + t.other + " other";
        kv += "\n" + torrentLine;
        if (t.pending_sort) kv += "\n⚠ " + t.pending_sort + " waiting to be sorted";
      }
    } else {
      kv += "\n" + (q.error || "not reachable");
    }
    $("qb-kv").textContent = kv;

    const binding = q.vpn_binding;
    const bindingWarning = $("qb-vpn-warning");
    const bindingUnknown = binding?.checked === false;
    const bindingUnsafe = binding?.checked === true && !binding.safe;
    const showBindingWarning = Boolean(currentUser?.is_admin && q.online &&
      binding && (bindingUnknown || bindingUnsafe));
    bindingWarning.style.display = showBindingWarning ? "flex" : "none";
    if (showBindingWarning) {
      const message = bindingUnknown
        ? "Could not verify qBittorrent's VPN interface binding."
        : (binding.interface
          ? `qBittorrent is bound to ${binding.interface}, not the active VPN.`
          : "qBittorrent can use any network interface.");
      const action = binding.vpn_interface
        ? ` Bind it to ${binding.vpn_interface}.`
        : " Connect the VPN first, then bind it.";
      $("qb-vpn-warning-text").textContent = message + action;
      $("qb-bind-vpn").disabled = !binding.vpn_interface;
      $("qb-bind-vpn").textContent = binding.vpn_interface
        ? `Bind to ${binding.vpn_interface}` : "Bind to VPN";
    }

    const v = s.nordvpn;
    $("vp-dot").className = "dot " + (v.connected ? "ok" : (v.available ? "warn" : ""));
    $("vp-state").textContent = v.connected ? "Connected" : (v.available ? "Disconnected" : "Not installed");
    kv = v.connected ? (v.server || "connected") : (v.error || "not connected");
    if (v.account && v.account.active != null) {
      kv += "\nsubscription: " + (v.account.active ? "active" : "inactive") +
            (v.account.expiry ? " · until " + v.account.expiry : "");
    }
    if (v.tunnel_iface) kv += "\ntunnel: " + v.tunnel_iface;
    $("vp-kv").textContent = kv;

    const gu = s.guard;
    let guardText;
    if (gu.enabled && gu.compatible === false) {
      // the kill switch cannot function on this qBittorrent; do not present
      // the (preserved) ownership state as a currently working guard
      guardText =
        "guard: unavailable — compatible qBittorrent 5.x not confirmed";
    } else {
      guardText = gu.enabled
        ? ("guard: " + (gu.paused ? "⏸ torrents paused (no tunnel)" : (gu.tunnel ? "ok (" + gu.tunnel + ")" : "waiting for tunnel")))
        : "guard: off";
    }
    // Admins see exactly which interfaces the configured globs match, so a
    // too-broad pattern visibly produces e.g. "lo, eth0, wlan0" instead of
    // silently treating the first match as the VPN (ISSUE 10).
    if (Array.isArray(gu.matched_ifaces)) {
      guardText += "  [matches: " +
        (gu.matched_ifaces.length ? gu.matched_ifaces.join(", ") : "none") + "]";
    }
    $("guard-line").textContent = guardText;
    $("vp-connect").disabled = !v.available || v.connected;
    $("vp-disconnect").disabled = !v.available || !v.connected;
  } catch (e) {
    console.error(e);
  }
}

function tdText(value) {
  const td = document.createElement("td");
  td.textContent = value == null ? "" : String(value);
  return td;
}

async function refreshTorrents() {
  try {
    const torrents = await j("/api/torrents");
    const tbody = $("tbody");
    tbody.replaceChildren();

    if (!torrents.length) {
      const tr = document.createElement("tr");
      const td = tdText("no torrents");
      td.colSpan = 7;
      td.className = "dim";
      tr.appendChild(td);
      tbody.appendChild(tr);
      return;
    }

    for (const t of torrents) {
      const tr = document.createElement("tr");

      const nameTd = tdText(t.name || "…");
      nameTd.title = String(t.name || "");
      tr.appendChild(nameTd);

      const categoryTd = document.createElement("td");
      if (t.category) {
        const chip = document.createElement("span");
        chip.classList.add("chip");
        if (t.category === "tv") chip.classList.add("tv");
        else if (t.category === "movies") chip.classList.add("movie");
        chip.textContent = String(t.category);
        categoryTd.appendChild(chip);
      } else {
        categoryTd.textContent = "—";
        categoryTd.className = "dim";
      }
      tr.appendChild(categoryTd);

      const numericProgress = Number(t.progress) || 0;
      const percent = Math.max(0, Math.min(100, Math.round(numericProgress * 100)));
      const done = numericProgress >= 0.999;
      const progressTd = document.createElement("td");
      const bar = document.createElement("div");
      bar.className = "bar";
      const fill = document.createElement("i");
      if (done) fill.classList.add("done");
      fill.style.width = percent + "%";
      bar.appendChild(fill);
      progressTd.appendChild(bar);
      tr.appendChild(progressTd);

      tr.appendChild(tdText(fmtSpeed(done ? t.upspeed : t.dlspeed) + (done ? " ↑" : " ↓")));
      tr.appendChild(tdText(t.state || ""));

      const sortTd = document.createElement("td");
      const status = document.createElement("span");
      status.classList.add("sortchip");
      if (t.sort_status === "sorted" && t.cleanup_status === "pending") {
        status.classList.add("wait");
        status.textContent = "⚠ cleanup pending";
        status.title = "Files are in the library; removing the torrent from qBittorrent "
                       + "failed and will be retried automatically.";
      } else if (t.sort_status === "sorted") {
        status.classList.add("ok");
        status.textContent = "✓ sorted";
      } else if (t.sort_status === "failed") {
        status.classList.add("bad");
        status.textContent = "✗ failed";
        status.title = String(t.error || "");
      } else if (t.sort_status === "incomplete") {
        status.classList.add("wait");
        status.textContent = "⚠ sort incomplete";
        status.title = "The sort was interrupted (disk error, restart, ...) and will "
                       + "be retried automatically. Files already placed are kept — "
                       + "a retry skips them and never re-moves or re-dedupes them. "
                       + "“Sort now” retries immediately.";
      } else if (done && (t.category === "movies" || t.category === "tv")) {
        status.classList.add("wait");
        status.textContent = "… sorting";
      } else {
        status.classList.add("dim");
        status.textContent = "—";
      }
      sortTd.appendChild(status);
      tr.appendChild(sortTd);

      const actionTd = document.createElement("td");
      if (currentUser?.is_admin && done &&
          (t.category === "movies" || t.category === "tv") && t.sort_status !== "sorted") {
        const button = document.createElement("button");
        button.className = "ghost sort-action";
        button.type = "button";
        button.textContent = "Sort now";
        button.addEventListener("click", () => sortNow(String(t.hash || "")));
        actionTd.appendChild(button);
      }
      tr.appendChild(actionTd);
      tbody.appendChild(tr);
    }
  } catch (e) {
    console.error(e);
  }
}

async function refreshFeed() {
  try {
    const events = await j("/api/sortlog");
    const feed = $("feed");
    feed.replaceChildren();
    if (!events.length) {
      const empty = document.createElement("div");
      empty.textContent = "nothing yet";
      feed.appendChild(empty);
      return;
    }
    for (const event of events) {
      const row = document.createElement("div");
      if (event.level === "warn") row.classList.add("warn");
      else if (event.level === "error") row.classList.add("err");
      const timeEl = document.createElement("time");
      const date = new Date(Number(event.ts) * 1000);
      timeEl.textContent = date.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
      row.appendChild(timeEl);
      row.appendChild(document.createTextNode(String(event.msg || "")));
      feed.appendChild(row);
    }
  } catch (e) {
    console.error(e);
  }
}

async function sortNow(hash) {
  try {
    const r = await j(`/api/torrents/${encodeURIComponent(hash)}/sort`, {method: "POST"});
    if (r.ok && r.cleanup_status === "pending") {
      alert("Sorted successfully; qBittorrent cleanup is pending and will be retried.");
    } else if (r.ok) {
      alert("Sorted: " + r.dest);
    } else {
      alert("Sort failed: " + (r.error || r.status));
    }
    refreshTorrents();
    refreshFeed();
  } catch (e) {
    alert("Error: " + e.message);
  }
}

$("add-btn").addEventListener("click", async () => {
  const btn = $("add-btn");
  const msg = $("add-msg");
  btn.disabled = true;
  msg.className = "msg";
  msg.textContent = "";
  try {
    const fd = new FormData();
    const magnet = $("magnet").value.trim();
    if (magnet) fd.set("magnet", magnet);
    const file = $("tfile").files[0];
    if (file) fd.set("torrent_file", file);
    if (!magnet && !file) throw new Error("paste a magnet or choose a .torrent file");
    fd.set("kind", $("kind").value);
    const r = await j("/api/add", {method: "POST", body: fd});
    if (r.started === false) {
      throw new Error("Torrent was added but qBittorrent did not start it.");
    }
    msg.className = "msg ok";
    const parsed = r.parsed || {};
    msg.textContent = `Added ${r.kind === "tv" ? "TV" : "movie"}${r.note ? " (" + r.note + ")" : ""} → ${parsed.title || "naming after metadata"}` +
      (r.save_path ? " → " + r.save_path : "");
    $("magnet").value = "";
    $("tfile").value = "";
    refreshTorrents();
    refreshFeed();
  } catch (e) {
    msg.className = "msg err";
    msg.textContent = e.message;
  } finally {
    btn.disabled = false;
  }
});

$("vp-connect").addEventListener("click", async () => {
  await j("/api/vpn", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({action: "connect"})});
  setTimeout(refreshStatus, 2500);
});
$("vp-disconnect").addEventListener("click", async () => {
  await j("/api/vpn", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({action: "disconnect"})});
  setTimeout(refreshStatus, 2500);
});
$("qb-bind-vpn").addEventListener("click", async () => {
  const btn = $("qb-bind-vpn");
  btn.disabled = true;
  try {
    const result = await j("/api/qbittorrent/bind-vpn", {method: "POST"});
    alert(`qBittorrent is now restricted to ${result.vpn_binding.vpn_interface}.`);
    await Promise.all([refreshStatus(), refreshFeed()]);
  } catch (e) {
    alert("Could not bind qBittorrent: " + e.message);
    await refreshStatus();
  }
});
$("jf-refresh").addEventListener("click", async () => {
  try {
    await j("/api/refresh-jellyfin", {method: "POST"});
    alert("Jellyfin library refresh requested");
  } catch (e) {
    alert("Error: " + e.message);
  }
});

const CFG_FIELDS = {
  GATEWAY_HOST: "text", GATEWAY_PORT: "num", HIDE_SETTINGS: "chk",
  QB_URL: "text", QB_USER: "text", QB_PASS: "pass",
  JELLYFIN_URL: "text", JELLYFIN_API_KEY: "pass",
  MOVIES_DIR: "text", TV_DIR: "text", DOWNLOAD_DIR: "text",
  WATCH_POLL: "num", SORT_DELETE_TORRENT: "chk", NOTIFY_JELLYFIN: "chk",
  VPN_GUARD: "chk", VPN_GUARD_POLL: "num", VPN_GUARD_IFACES: "text",
};
let cfgBaseline = {};

function applyHideSettings() {
  const el = $("cfg-HIDE_SETTINGS");
  $("settings-card").style.display = el && el.checked ? "none" : "";
}

async function loadConfig() {
  try {
    const config = await j("/api/config");
    cfgBaseline = {};
    for (const [key, meta] of Object.entries(config)) {
      const el = $("cfg-" + key);
      if (!el) continue;
      const kind = CFG_FIELDS[key];
      if (kind === "chk") {
        el.checked = Boolean(meta.value);
        cfgBaseline[key] = el.checked;
      } else if (meta.value == null) {
        el.value = "";
        el.placeholder = meta.set ? "•••• (set — leave empty to keep)" : "not set";
        cfgBaseline[key] = "";
      } else {
        el.value = meta.value;
        cfgBaseline[key] = String(meta.value);
      }
    }
    applyHideSettings();
  } catch (e) {
    console.error(e);
  }
}

$("save-cfg").addEventListener("click", async () => {
  const btn = $("save-cfg");
  const msg = $("cfg-msg");
  btn.disabled = true;
  msg.className = "msg";
  msg.textContent = "";
  try {
    const payload = {};
    for (const key of Object.keys(CFG_FIELDS)) {
      const el = $("cfg-" + key);
      const kind = CFG_FIELDS[key];
      let current;
      if (kind === "chk") {
        current = el.checked;
      } else {
        const raw = el.value.trim();
        current = kind === "num" ? (raw === "" ? null : Number(raw)) : raw;
      }
      const baseline = cfgBaseline[key];
      const changed = kind === "chk" ? current !== baseline
        : String(current === null ? "" : current) !== String(baseline);
      if (!changed) continue;
      if (kind === "pass" && current === "") continue;
      if (kind === "num" && (current === null || !Number.isFinite(current) || current <= 0)) {
        throw new Error(key + ": must be a number > 0");
      }
      payload[key] = current;
    }
    if (!Object.keys(payload).length) {
      msg.textContent = "no changes to save";
      return;
    }
    const r = await j("/api/config", {method: "POST", headers: {"Content-Type": "application/json"},
                                      body: JSON.stringify(payload)});
    msg.className = "msg ok";
    let text = "Saved: " + (r.applied.length ? r.applied.join(", ") : "(no changes)");
    if (r.restart.length) {
      text += " — restart needed for " + r.restart.join(", ") + " (systemctl --user restart jellyfin-gateway)";
    }
    if (r.kept.length) text += " · kept: " + r.kept.join(", ");
    msg.textContent = text;
    await loadConfig();
    refreshStatus();
  } catch (e) {
    msg.className = "msg err";
    msg.textContent = e.message;
  } finally {
    btn.disabled = false;
  }
});

let browseTarget = null;

function openBrowse(key) {
  browseTarget = key;
  const el = $("cfg-" + key);
  $("browse-overlay").style.display = "flex";
  browseGo(el && el.value.trim());
  $("browse-path").focus();
}

function closeBrowse() {
  $("browse-overlay").style.display = "none";
  browseTarget = null;
}

function browseItem(text, path, className = "") {
  const item = document.createElement("div");
  if (className) item.className = className;
  item.textContent = String(text);
  if (path != null) {
    item.dataset.path = String(path);
    item.title = String(path);
  }
  return item;
}

async function browseGo(path) {
  const target = (path === undefined ? $("browse-path").value : String(path)).trim() || "/";
  const list = $("browse-list");
  list.replaceChildren(browseItem("loading…", null, "empty"));
  try {
    const data = await j("/api/browse?path=" + encodeURIComponent(target));
    $("browse-path").value = data.path;

    const quick = $("browse-quick");
    quick.replaceChildren();
    for (const pathValue of data.quick || []) {
      const chip = document.createElement("span");
      chip.className = "chip";
      chip.dataset.path = String(pathValue);
      chip.textContent = String(pathValue);
      quick.appendChild(chip);
    }

    list.replaceChildren();
    if (data.parent) list.appendChild(browseItem("↑ " + data.parent, data.parent, "up"));
    if (data.dirs && data.dirs.length) {
      for (const dir of data.dirs) list.appendChild(browseItem("📁 " + dir.name, dir.path));
    } else {
      list.appendChild(browseItem("empty folder", null, "empty"));
    }
  } catch (e) {
    list.replaceChildren(browseItem(e.message, null, "err"));
  }
}

for (const button of document.querySelectorAll(".browse-btn[data-target]")) {
  button.addEventListener("click", () => openBrowse(button.dataset.target));
}
$("browse-go").addEventListener("click", () => browseGo());
$("browse-cancel").addEventListener("click", closeBrowse);
$("browse-list").addEventListener("click", event => {
  const dir = event.target.closest("div[data-path]");
  if (dir) browseGo(dir.dataset.path);
});
$("browse-quick").addEventListener("click", event => {
  const chip = event.target.closest(".chip[data-path]");
  if (chip) browseGo(chip.dataset.path);
});
$("browse-path").addEventListener("keydown", event => {
  if (event.key === "Enter") browseGo();
});
$("browse-use").addEventListener("click", () => {
  if (!browseTarget) return;
  const el = $("cfg-" + browseTarget);
  if (el) el.value = $("browse-path").value;
  closeBrowse();
});
$("browse-overlay").addEventListener("click", event => {
  if (event.target === $("browse-overlay")) closeBrowse();
});

function stopPolling() {
  pollers.forEach(clearInterval);
  pollers = [];
}

function applyRole() {
  document.querySelectorAll(".admin-only").forEach(el => {
    el.style.display = currentUser && currentUser.is_admin ? "" : "none";
  });
  if (!currentUser || !currentUser.is_admin) $("browse-overlay").style.display = "none";
  if (currentUser) {
    $("identity-name").textContent = currentUser.username;
    $("identity-role").textContent = currentUser.is_admin ? "Administrator" : "Jellyfin user";
  }
}

function showLogin(message = "") {
  currentUser = null;
  stopPolling();
  $("dashboard").style.display = "none";
  $("identity").style.display = "none";
  $("login-view").style.display = "grid";
  $("subline").textContent = "Sign in with Jellyfin";
  if (message) {
    $("login-msg").className = "msg err";
    $("login-msg").textContent = message;
  }
}

async function refreshIdentity() {
  try {
    const user = await j("/api/auth/me");
    const becameAdmin = currentUser && !currentUser.is_admin && user.is_admin;
    const changed = !currentUser || user.is_admin !== currentUser.is_admin || user.username !== currentUser.username;
    currentUser = user;
    if (changed) {
      applyRole();
      if (becameAdmin) loadConfig();
    }
  } catch {
    // j() handles expired sessions by returning the UI to the login view.
  }
}

async function showDashboard(user) {
  currentUser = user;
  stopPolling();
  $("login-view").style.display = "none";
  $("dashboard").style.display = "block";
  $("identity").style.display = "flex";
  applyRole();
  await Promise.all([refreshStatus(), refreshTorrents(), refreshFeed(),
                     user.is_admin ? loadConfig() : Promise.resolve()]);
  pollers.push(setInterval(refreshStatus, 5000));
  pollers.push(setInterval(refreshTorrents, 3000));
  pollers.push(setInterval(refreshFeed, 5000));
  pollers.push(setInterval(refreshIdentity, 30000));
}

async function login() {
  const btn = $("login-btn");
  const msg = $("login-msg");
  btn.disabled = true;
  msg.className = "msg";
  msg.textContent = "";
  try {
    const user = await j("/api/auth/login", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({username: $("login-username").value, password: $("login-password").value})});
    $("login-password").value = "";
    await showDashboard(user);
  } catch (e) {
    msg.className = "msg err";
    msg.textContent = e.message;
  } finally {
    btn.disabled = false;
  }
}

$("login-btn").addEventListener("click", login);
$("login-password").addEventListener("keydown", event => {
  if (event.key === "Enter") login();
});
$("login-username").addEventListener("keydown", event => {
  if (event.key === "Enter") $("login-password").focus();
});
$("logout-btn").addEventListener("click", async () => {
  try {
    await j("/api/auth/logout", {method: "POST"});
  } catch {
    // The local session is cleared from the UI even if logout fails remotely.
  }
  $("login-password").value = "";
  showLogin();
});

(async () => {
  try {
    await showDashboard(await j("/api/auth/me"));
  } catch {
    showLogin();
  }
})();
