"use strict";
const $ = (id) => document.getElementById(id);
const phases = {
  stopped: "已停止",
  starting: "启动中",
  running: "运行中",
  stopping: "停止中",
  error: "需要处理",
};
let csrf = "",
  model = null,
  priority = [],
  settingsLoaded = false,
  busy = false,
  timer = null;
const esc = (s) =>
  String(s ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const pct = (n) => Math.round(Math.max(0, Math.min(1, Number(n) || 0)) * 100);
const clock = (s) => new Date(s).toLocaleTimeString("zh-CN", { hour12: false });
const date = (s) =>
  new Date(s).toLocaleString("zh-CN", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
function image(url) {
  try {
    const u = new URL(url);
    return u.protocol === "https:" &&
      ["static-cdn.jtvnw.net", "images.igdb.com"].includes(u.hostname)
      ? u.href
      : "";
  } catch {
    return "";
  }
}
function link(url) {
  try {
    const u = new URL(url);
    return u.protocol === "https:" ? u.href : "";
  } catch {
    return "";
  }
}
function notice(text) {
  $("notice").textContent = text;
  $("notice").hidden = !text;
}
async function api(path, method = "GET", body) {
  const response = await fetch("/api/" + path, {
    method,
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      ...(csrf ? { "X-CSRF-Token": csrf } : {}),
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  if (!response.ok) {
    const text = await response.text();
    if (response.status === 401 && path !== "login") showLogin();
    throw new Error(text || "请求失败");
  }
  return response.json();
}
function showLogin() {
  csrf = "";
  clearTimeout(timer);
  $("console").hidden = true;
  $("login").hidden = false;
}
async function entered(session) {
  csrf = session.csrf;
  settingsLoaded = false;
  $("login").hidden = true;
  $("console").hidden = false;
  $("password").value = "";
  await refresh();
}
$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const button = e.submitter;
  button.disabled = true;
  $("login-error").textContent = "";
  try {
    await entered(
      await api("login", "POST", {
        username: "admin",
        password: $("password").value,
      }),
    );
  } catch (err) {
    $("login-error").textContent = err.message;
  } finally {
    button.disabled = false;
  }
});
$("logout").addEventListener("click", async () => {
  try {
    await api("logout", "POST", {});
    showLogin();
  } catch (err) {
    notice(err.message);
  }
});
function page(name) {
  for (const element of document.querySelectorAll(".page"))
    element.hidden = element.id !== name;
  for (const button of document.querySelectorAll(".nav")) {
    const active = button.dataset.page === name;
    button.classList.toggle("active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  $("page-title").textContent = {
    overview: "运行总览",
    campaigns: "掉宝活动",
    settings: "游戏与设置",
    logs: "运行日志",
  }[name];
}
for (const button of document.querySelectorAll("[data-page]"))
  button.addEventListener("click", () => page(button.dataset.page));
for (const button of document.querySelectorAll("[data-open]"))
  button.addEventListener("click", () => page(button.dataset.open));
async function action(name, body) {
  if (busy) return;
  busy = true;
  for (const b of document.querySelectorAll("[data-action]")) b.disabled = true;
  notice("正在处理…");
  try {
    await api("actions/" + name, "POST", body ?? {});
    notice(
      name === "stop"
        ? "后台已停止，网页仍可访问"
        : "操作已提交，状态将自动更新",
    );
    await refresh();
  } catch (err) {
    notice(err.message);
  } finally {
    busy = false;
    for (const b of document.querySelectorAll("[data-action]"))
      b.disabled = false;
  }
}
for (const button of document.querySelectorAll("[data-action]"))
  button.addEventListener("click", (e) => {
    e.preventDefault();
    action(button.dataset.action);
  });
async function refresh() {
  clearTimeout(timer);
  if (!csrf) return;
  try {
    model = await api("state");
    $("connection").textContent = "控制台已连接";
    $("connection").className = "badge good";
    render();
  } catch (err) {
    if (csrf) {
      $("connection").textContent = "连接中断";
      $("connection").className = "badge bad";
      notice("无法读取最新状态：" + err.message);
    }
  } finally {
    if (csrf) timer = setTimeout(refresh, 2500);
  }
}
function render() {
  const m = model;
  $("phase").textContent = phases[m.phase] ?? m.phase;
  $("phase").dataset.phase = m.phase;
  $("runtime-status").textContent = m.status;
  $("uptime").textContent = m.started_at
    ? "最近启动 " + clock(m.started_at)
    : "尚未启动";
  $("stat-active").textContent = m.campaigns.filter(
    (c) => c.active && c.eligible,
  ).length;
  $("stat-claimed").textContent = m.campaigns.reduce(
    (sum, c) => sum + c.drops.filter((d) => d.claimed).length,
    0,
  );
  $("account-state").textContent = m.account.authenticated
    ? "已登录"
    : m.account.device
      ? "等待授权"
      : "未登录";
  $("account-id").textContent = m.account.authenticated
    ? "Twitch ID " + m.account.user_id
    : m.account.status;
  $("device-card").hidden = !m.account.device;
  if (m.account.device) $("device-code").textContent = m.account.device.code;
  $("runtime-error").hidden = !m.error;
  $("error-detail").textContent = m.error ?? "";
  $("watching-name").textContent = m.watching?.name ?? "暂无频道";
  const d = m.drop;
  $("current-drop").className = d ? "" : "empty";
  $("current-drop").innerHTML = d
    ? `<div class="current-game">${esc(m.watching?.game ?? "当前奖励")}</div><h3 class="current-title">${esc(d.rewards || d.name)}</h3><div class="meter"><progress max="100" value="${pct(d.progress)}" aria-label="掉宝进度"></progress></div><div class="reward-line"><span>${d.minutes} / ${d.required_minutes} 分钟${d.minutes !== d.confirmed_minutes ? " · 含估算进度" : ""}</span><strong>${pct(d.progress)}%</strong></div>`
    : "尚无掉宝任务。连接账号后，在“游戏与设置”中添加优先游戏。";
  $("recent-logs").innerHTML = m.logs.length
    ? m.logs
        .slice(-4)
        .reverse()
        .map(
          (l) =>
            `<p><small>${esc(clock(l.at))} · ${esc(l.level)}</small>${esc(l.message.slice(0, 180))}</p>`,
        )
        .join("")
    : '<p class="muted">暂无事件，启动后可查看运行过程。</p>';
  $("channel-list").className =
    "channel-list" + (m.channels.length ? "" : " empty");
  $("channel-list").innerHTML = m.channels.length
    ? m.channels
        .map(
          (c) =>
            `<div class="channel-row"><div><strong>${esc(c.name)}</strong><small>${esc(c.game ?? "未开播")} · ${c.online ? "在线" : "离线"} · ${c.viewers ?? 0} 人</small></div><button class="switch-channel" data-id="${esc(c.id)}" ${!c.online || !c.drops_enabled ? "disabled" : ""}>切换频道</button></div>`,
        )
        .join("")
    : "暂无频道。登录并选择游戏后会自动发现频道。";
  for (const button of document.querySelectorAll(".switch-channel"))
    button.addEventListener("click", () =>
      action("select-channel", { id: button.dataset.id }),
    );
  $("game-options").innerHTML = m.games
    .map((g) => `<option value="${esc(g)}"></option>`)
    .join("");
  if (!settingsLoaded) loadSettings();
  renderCampaigns();
  renderLogs();
}
function renderCampaigns() {
  if (!model) return;
  const q = $("campaign-search").value.toLowerCase(),
    filter = $("campaign-filter").value;
  const list = model.campaigns.filter(
    (c) =>
      (c.name + " " + c.game).toLowerCase().includes(q) &&
      (filter === "all" || c[filter]),
  );
  $("campaign-grid").innerHTML = list.length
    ? list
        .map((c) => {
          const art = image(c.image),
            url = link(c.link_url);
          return `<article class="campaign"><div class="campaign-head">${art ? `<img src="${esc(art)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : ""}<div><span class="badge">${c.expired ? "已结束" : c.upcoming ? "即将开始" : "进行中"}</span><h3>${esc(c.game)}</h3><small>${esc(c.name)}</small></div></div><div class="campaign-body"><div class="reward-line"><span>${c.linked ? "账号已关联" : "需要关联游戏账号"}</span><span>${esc(date(c.ends_at))} 截止</span></div>${!c.linked && url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">前往关联账号 ↗</a>` : ""}${c.drops
            .map((d) => {
              const reward = image(d.benefits[0]?.image);
              return `<div class="reward"><div class="reward-top">${reward ? `<img src="${esc(reward)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : ""}<div><strong>${esc(d.rewards || d.name)}</strong><small>${d.claimed ? "已领取" : d.claimable ? "等待核心自动领取" : "累计观看时长"}</small></div></div><div class="meter"><progress max="100" value="${pct(d.progress)}" aria-label="奖励进度"></progress></div><div class="reward-line"><span>${d.minutes} / ${d.required_minutes} 分钟${d.minutes !== d.confirmed_minutes ? " · 含估算" : ""}</span><span>${pct(d.progress)}%</span></div></div>`;
            })
            .join("")}</div></article>`;
        })
        .join("")
    : '<div class="panel empty">暂无匹配活动。首次使用请启动服务并完成 Twitch 授权。</div>';
}
$("campaign-search").addEventListener("input", renderCampaigns);
$("campaign-filter").addEventListener("change", renderCampaigns);
function loadSettings() {
  const s = model.settings;
  priority = [...s.priority];
  $("excluded").value = s.exclude.join("\n");
  $("priority-mode").value = s.priority_mode;
  $("quality").value = s.connection_quality;
  $("badges").checked = s.enable_badges_emotes;
  $("drop-check").checked = s.available_drops_check;
  $("language").innerHTML = model.languages
    .map((l) => `<option value="${esc(l)}">${esc(l)}</option>`)
    .join("");
  $("language").value = s.language;
  settingsLoaded = true;
  renderPriority();
}
function renderPriority() {
  $("priority-list").innerHTML = priority.length
    ? priority
        .map(
          (g, i) =>
            `<li><small>${i + 1}</small><span>${esc(g)}</span><button type="button" data-index="${i}" data-move="-1" aria-label="上移 ${esc(g)}" ${i === 0 ? "disabled" : ""}>↑</button><button type="button" data-index="${i}" data-move="1" aria-label="下移 ${esc(g)}" ${i === priority.length - 1 ? "disabled" : ""}>↓</button><button type="button" data-remove="${i}" aria-label="移除 ${esc(g)}">×</button></li>`,
        )
        .join("")
    : '<li class="muted">尚未选择游戏，不会擅自运行其他活动。</li>';
  for (const b of document.querySelectorAll("[data-move]"))
    b.addEventListener("click", () => {
      const i = Number(b.dataset.index),
        j = i + Number(b.dataset.move);
      [priority[i], priority[j]] = [priority[j], priority[i]];
      renderPriority();
    });
  for (const b of document.querySelectorAll("[data-remove]"))
    b.addEventListener("click", () => {
      priority.splice(Number(b.dataset.remove), 1);
      renderPriority();
    });
}
function addGame() {
  const value = $("game-input").value.trim();
  if (value && !priority.includes(value)) {
    priority.push(value);
    $("game-input").value = "";
    renderPriority();
  }
}
$("add-game").addEventListener("click", addGame);
$("game-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    addGame();
  }
});
$("settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const b = e.submitter;
  b.disabled = true;
  try {
    await api("settings", "PUT", {
      priority,
      exclude: $("excluded")
        .value.split("\n")
        .map((s) => s.trim())
        .filter(Boolean),
      priority_mode: $("priority-mode").value,
      connection_quality: Number($("quality").value),
      language: $("language").value,
      enable_badges_emotes: $("badges").checked,
      available_drops_check: $("drop-check").checked,
    });
    $("save-result").textContent = "已保存 · 重新加载后全部生效";
    await refresh();
  } catch (err) {
    notice(err.message);
  } finally {
    b.disabled = false;
  }
});
function renderLogs(force = false) {
  if (!model) return;
  // Do not jump back to the top while someone is reading older log entries.
  if (!force && $("log-list").scrollTop > 10) return;
  const filtered = model.logs.filter(
    (l) =>
      $("log-filter").value === "all" ||
      ["ERROR", "WARNING", "CRITICAL"].includes(l.level),
  );
  $("log-list").innerHTML = filtered.length
    ? filtered
        .slice()
        .reverse()
        .map(
          (l) =>
            `<div class="log-entry ${["ERROR", "CRITICAL"].includes(l.level) ? "error" : l.level === "WARNING" ? "warning" : ""}"><small>${esc(date(l.at))} · ${esc(l.level)}</small><pre>${esc(l.message)}</pre></div>`,
        )
        .join("")
    : '<div class="empty">当前没有匹配日志</div>';
}
$("log-filter").addEventListener("change", () => {
  $("log-list").scrollTop = 0;
  renderLogs(true);
});
$("log-list").addEventListener("scroll", () => {
  if ($("log-list").scrollTop === 0) renderLogs();
});
api("session")
  .then(entered)
  .catch(() => showLogin());
