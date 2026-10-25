# Twitch Drops Miner Docker（独立 fork）

这是 choneer fork 的容器支持，不是 DevilXD 官方支持的功能。保留原版
Tkinter 界面，通过 noVNC 在浏览器操作；不修改掉宝逻辑，不下载直播视频。
首版支持 linux/amd64（fnOS x86），不宣称已验证 ARM。

## fnOS 部署

将仓库放在 `/vol1/1000/docker/twitchdropsminer/`，进入其中的 `docker/`。
复制 `.env.example` 为 `.env`，将 BIND_IP 改为 NAS 当前局域网 IP。
DATA_PATH 指向存储池 2；不要与其他实例共用目录。

```sh
cp .env.example .env
umask 077
openssl rand -hex 24 > web-password.secret
# 妥善保存这个文件里的密码，不要提交或发送给别人。
docker compose up -d --build
```

发布镜像可用后，使用 `docker compose pull && docker compose up -d --no-build`。
浏览器打开 `http://NAS-IP:5800`，HTTP 认证用户名固定为 `admin`，
密码为 `web-password.secret` 中的单行内容（16–72 个可打印 ASCII 字符）。随后在 GUI 中完成 Twitch 设备授权，
选择中文、游戏优先级，按 Reload。游戏账号关联需在 Twitch 官方页面完成。
默认没有设置游戏，不会擅自选择活动。关闭浏览器不影响容器运行。

只开放 8080 的映射端口；VNC/WebSocket 内部端口仅监听容器回环地址。
默认映射到宿主机 127.0.0.1，必须显式配置 BIND_IP 才允许局域网访问。
HTTP Basic 认证不加密传输：只用于可信局域网；远程访问使用 VPN 或带 HTTPS
的反向代理，绝不要直接向公网开放。不要使用 host 网络模式。

## 数据、备份及更新

所有 cookies、settings、cache 和锁文件都在 `/data`；源码和语言资源不在数据卷，
更新镜像不会被旧代码覆盖。未指定 TDM_DATA_DIR 的桌面版路径保持原样。
程序以 UID/GID 1000 运行；入口仅调整数据根目录及 cache 目录归属，不递归
修改已有文件。迁移旧数据前请停止旧程序，并确保文件对 UID 1000 可写。
目录权限为 0700；cookies 等同账号凭据，不要共享、上传或加入版本控制。
首次设置语言可直接在原版 Settings 页面中选择简体中文。

更新前先 `docker compose stop`，备份 DATA_PATH，拉取镜像或源码后重建。
降级前也先停止容器，必要时恢复备份。不会自动从上游拉代码或修改运行环境。
日志使用 Docker 的 10MB × 3 轮转，不默认开启程序无限增长的 `--log`。

## 恢复与限制

Docker 开机自启；supervisor 自动重启异常退出的子进程，正常关闭程序不会
立即重启。需重新启动时执行 `docker compose restart`。
停止容器预留 150 秒，让 GUI 接收 SIGTERM、保存状态并退出。
健康检查覆盖六个进程和 HTTP 认证边界，不向 Twitch 发出请求。
**原程序遇到错误可能保留窗口等待用户，进程仍在，健康检查不会把这种业务
错误判为异常，也不会因此自动重试登录。** 检查 GUI，避免盲目重启触发风控。
不要用同一账号同时在其他位置观看直播或运行第二个 miner。

这是非官方 Twitch 自动化；API 改动可能令程序失效，不保证零账号风险。
本地离线测试只证明容器界面、认证和持久化，不代表真实掉宝成功。
