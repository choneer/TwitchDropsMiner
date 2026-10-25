"use strict";
const $ = (id) => document.getElementById(id);
const phases = {
  stopped: "已停止",
  starting: "启动中",
  running: "运行中",
  stopping: "停止中",
  error: "需要处理",
};
const expandedRewards = new Set();
let csrf = "",
  model = null,
  priority = [],
  settingsLoaded = false,
  busy = false,
  timer = null,
  campaignPage = 0,
  campaignMarkup = "",
  suggestionMarkup = "";
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
    (c) => c.active && !c.finished,
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
    && d.progress_known === false
    ? `<div class="current-game">${esc(m.watching?.game ?? "当前奖励")}</div><h3 class="current-title">${esc(d.rewards || d.name)}</h3><p class="muted">等待 Twitch 确认账号进度 · 需要观看 ${d.required_minutes} 分钟</p>`
    : d
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
  const options = m.games
    .map((g) => `<option value="${esc(g)}"></option>`)
    .join("");
  if ($("game-options").innerHTML !== options) $("game-options").innerHTML = options;
  renderGameSuggestions();
  if (!settingsLoaded) loadSettings();
  renderCampaigns();
  renderLogs();
}
function renderCampaigns() {
  if (!model) return;
  const q = $("campaign-search").value.toLowerCase(),
    filter = $("campaign-filter").value;
  const discovery = model.discovery;
  $("catalog-status").textContent = discovery
    ? `${discovery.message}${discovery.updated_at ? " · 更新于 " + date(discovery.updated_at) : ""}`
    : "活动数据尚未加载";
  $("catalog-status").className = "catalog-status" + (discovery?.status === "error" ? " warning" : "");
  const list = model.campaigns.filter((c) => CampaignView.matches(c, filter, q))
    .sort((a, b) => Number(b.active) - Number(a.active) || new Date(a.ends_at) - new Date(b.ends_at));
  const pageSize = Number($("campaign-page-size").value);
  const pages = Math.max(1, Math.ceil(list.length / pageSize));
  campaignPage = Math.min(campaignPage, pages - 1);
  $("campaign-count").textContent = `共 ${list.length} 个活动 · ${campaignPage + 1} / ${pages} 页`;
  $("campaign-prev").disabled = campaignPage === 0;
  $("campaign-next").disabled = campaignPage === pages - 1;
  const html = list.length
    ? list.slice(campaignPage * pageSize, (campaignPage + 1) * pageSize)
        .map((c) => {
          const art = image(c.image),
            url = link(c.link_url);
          const linkage = c.linked === null ? "账号关联状态待 Twitch 确认" : c.linked ? "账号已关联" : "需要关联游戏账号";
          return `<article class="campaign"><div class="campaign-head">${art ? `<img src="${esc(art)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : ""}<div><span class="badge ${c.finished ? "good" : ""}">${CampaignView.status(c)}</span><h3>${esc(c.game)}</h3><small>${esc(c.name)}</small></div></div><div class="campaign-body"><small>${c.catalog_only ? "公开活动目录 · 账号进度待同步" : "Twitch 账号数据"}</small><div class="reward-line"><span>${linkage}</span><span>${esc(date(c.ends_at))} 截止</span></div>${c.linked !== true && url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">前往关联账号 ↗</a>` : ""}${!c.finished && !c.expired && c.watchable ? `<button type="button" class="choose-game" data-game="${esc(c.game)}">加入优先游戏</button>` : ""}<div class="reward-scroll" data-reward-scroll="${esc(c.id)}" tabindex="0" role="region" aria-label="${esc(c.game)}的奖励列表">${c.drops
            .slice(0, expandedRewards.has(c.id) ? c.drops.length : 3)
            .map((d) => {
              const reward = image(d.benefits[0]?.image);
              if (d.required_subs > 0) return `<div class="reward"><div class="reward-top">${reward ? `<img src="${esc(reward)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : ""}<div><strong>${esc(d.rewards || d.name)}</strong><small>${d.claimed ? "已领取" : `需要订阅 ${d.required_subs} 次 · 无法通过挂机完成`}</small></div></div></div>`;
              const known = d.progress_known !== false;
              return `<div class="reward"><div class="reward-top">${reward ? `<img src="${esc(reward)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : ""}<div><strong>${esc(d.rewards || d.name)}</strong><small>${d.claimed ? "已领取" : d.claimable ? "等待核心自动领取" : known ? "累计观看时长" : "尚未取得账号进度"}</small></div></div>${known ? `<div class="meter"><progress max="100" value="${pct(d.progress)}" aria-label="奖励进度"></progress></div><div class="reward-line"><span>${d.minutes} / ${d.required_minutes} 分钟${d.minutes !== d.confirmed_minutes ? " · 含估算" : ""}</span><span>${pct(d.progress)}%</span></div>` : `<p class="muted">需要观看 ${d.required_minutes} 分钟 · 当前进度未知</p>`}</div>`;
            })
            .join("")}</div>${c.drops.length > 3 ? `<button type="button" class="reward-toggle" data-reward-toggle="${esc(c.id)}" aria-expanded="${expandedRewards.has(c.id)}">${expandedRewards.has(c.id) ? "收起奖励" : `查看更多 · 还有 ${c.drops.length - 3} 个奖励`}<span aria-hidden="true">⌄</span></button>` : ""}</div></article>`;
        })
        .join("")
    : `<div class="panel empty">${esc(CampaignView.empty(model.account.authenticated, discovery, model.campaigns.length > 0))}</div>`;
  if (campaignMarkup !== html) {
    const positions = new Map(Array.from(document.querySelectorAll("[data-reward-scroll]"),
      (element) => [element.dataset.rewardScroll, element.scrollTop]));
    $("campaign-grid").innerHTML = html;
    campaignMarkup = html;
    for (const element of document.querySelectorAll("[data-reward-scroll]"))
      element.scrollTop = positions.get(element.dataset.rewardScroll) ?? 0;
  }
}
$("campaign-grid").addEventListener("click", (event) => {
  const toggle = event.target.closest("[data-reward-toggle]");
  if (toggle) {
    const id = toggle.dataset.rewardToggle;
    if (expandedRewards.has(id)) expandedRewards.delete(id);
    else expandedRewards.add(id);
    renderCampaigns();
    Array.from(document.querySelectorAll("[data-reward-toggle]"))
      .find((button) => button.dataset.rewardToggle === id)?.focus({ preventScroll: true });
    return;
  }
  const button = event.target.closest("[data-game]");
  if (button) { addGame(button.dataset.game); page("settings"); notice("已加入待保存列表；点击保存设置，再重新加载以生效。"); }
});
for (const [id, event] of [["campaign-search", "input"], ["campaign-filter", "change"]])
  $(id).addEventListener(event, () => { campaignPage = 0; renderCampaigns(); });
$("campaign-prev").addEventListener("click", () => { campaignPage--; renderCampaigns(); });
$("campaign-next").addEventListener("click", () => { campaignPage++; renderCampaigns(); });
$("campaign-page-size").addEventListener("change", () => { campaignPage = 0; renderCampaigns(); });
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
  renderGameSuggestions();
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
function addGame(game) {
  const value = (typeof game === "string" ? game : $("game-input").value).trim();
  if (value && !priority.includes(value)) {
    priority.push(value);
    $("game-input").value = "";
    renderPriority();
    renderGameSuggestions();
  }
}
function renderGameSuggestions() {
  if (!model) return;
  const query = $("game-input").value.trim().toLowerCase();
  const games = model.games.filter((g) => g.toLowerCase().includes(query) && !priority.includes(g));
  $("game-hint").textContent = games.length
    ? `找到 ${games.length} 个有未完成活动的游戏，点击下方名称添加。`
    : query ? "当前活动目录没有匹配游戏。可手动添加完整名称，待活动开放后参与。"
      : "当前没有可添加的游戏；已完成活动不列入候选。目录加载失败时请刷新活动。";
  const html = games.slice(0, 8).map((g) => `<button type="button" data-game="${esc(g)}">${esc(g)}</button>`).join("");
  if (suggestionMarkup !== html) { $("game-suggestions").innerHTML = html; suggestionMarkup = html; }
}
$("game-input").addEventListener("input", renderGameSuggestions);
$("game-suggestions").addEventListener("click", (event) => {
  const button = event.target.closest("[data-game]");
  if (button) addGame(button.dataset.game);
});
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
