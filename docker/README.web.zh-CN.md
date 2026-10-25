# Drop Harbor：原生网页图形界面

这是独立 fork 的 Web 版，不是网页远程桌面。无 VNC、Xvfb、Tkinter、任务栏、
浏览器模拟或桌面通知服务。掉宝核心沿用上游算法，界面和服务生命周期独立实现。
桌面版入口 `main.py` 仍保留；服务入口为仓库根目录 `web_main.py`。

## 使用

镜像 `ghcr.io/choneer/twitchdropsminer:web`（当前验证 linux/amd64）。
在应用专属目录保存 `compose.web.yml`、`.env` 和 `web-password.secret`。
复制 `.env.example` 为 `.env`，设置 `BIND_IP`、`WEB_PORT=4444` 及独立 DATA_PATH。
使用 `openssl rand -hex 24 > web-password.secret` 生成密码，文件权限应为 0600。

```sh
docker compose -f compose.web.yml up -d --build
```

浏览器访问 `http://NAS-IP:4444`，输入控制台密码。固定管理员为 admin。
首次安装默认停止，不会主动联系 Twitch；点击“启动 / 连接 Twitch”，
界面会显示设备码。自己打开 `https://www.twitch.tv/activate` 完成授权。
本工具不收集 Twitch 密码，不绕过验证码、不自动处理账号风控。

游戏设置中添加完整游戏名，排序后保存并重新加载。默认只运行优先级列表，
不会自动挂所有活动。活动页显示奖励图片、账号关联状态、截止时间及进度；
估算时长会明确标记，不当作 Twitch 已确认进度。核心仍自动领取满足条件的奖励。
支持手动选择频道，仍受核心的资格/优先级判断约束。

## 状态和恢复

总览将网页连通、核心运行、账号授权和业务错误分开显示。HTTP `/healthz`
仅表示 Web 服务存活，不表示授权成功或掉宝有进展。异常在界面、Docker stdout
和 `/data/web-logs/application.log` 中脱敏记录；页面保留 500 条，文件按 2MB
轮转，保留当前文件及 3 个备份。日志不记录激活码或访问令牌。

显式启动后保存开机运行意图；显式停止后不会在容器重启时自行启动。
核心错误后不自动无限重试登录，需要查看日志并手动重新加载。
停止容器预留 150 秒以关闭连接并保存配置。数据目录有跨进程锁，防止共用。
所有配置沿用上游 settings.json 的序列化格式，cookies.jar 可从桌面版迁移。
服务自己的启动意图保存在 web-state.json 中，不修改上游格式。

## 安全与部署边界

密码必须 16–72 个可打印 ASCII 字符。应用进程以 UID/GID 1000 运行，目录
默认 0700，初始 root 进程只准备目录和只读密码副本，随后清除附加组并降权。
已有文件不递归 chown，迁移时需确保 UID 1000 可读写。
登录会话仅存服务器内存、12 小时过期，Cookie 使用 HttpOnly 和 SameSite=Strict。
写操作要求会话 CSRF Token，同源校验；登录有失败频率限制。
密码和 Cookie 不进入浏览器本地存储。注销仅注销控制台，不清除 Twitch 账号。

HTTP 不加密，仅限可信 LAN；公网访问必须走 VPN 或 HTTPS。HTTPS 反代时设置
`TDM_SECURE_COOKIES=1`，并设置准确 `TDM_PUBLIC_ORIGIN=https://your-domain`。
服务不盲目信任 X-Forwarded-*。不要改成 host 网络或公开到公网。

## 从现有 VNC 版迁移 / 回退

1. 停止旧容器，确认无第二个进程继续使用相同 cookies/settings。
2. 完整备份原数据目录和网页密码；复制到 Web 版独立目录，校正文件归属。
3. 用 Web 镜像启动，删除旧 nginx/VNC 配置挂载；端口映射仍可使用 4444:8080。
4. 网页登录后主动启动，确认账号、活动、游戏排序和真实进度。
5. 旧容器保持停止，保留旧镜像和备份。回退时先停 Web，恢复升级前备份，再启动旧版。

不要让新旧实例同时操作同一数据或同一 Twitch 账号。当前开发不会擅自替换
生产容器。离线测试通过不等于已验证你的账号能在 Twitch 获得实际奖励。
