# PackFerry 构建与运维

本文说明当前源码的构建入口。发行包与离线 Nexus 验收需分别在目标 Windows 11、Ubuntu 22.04/24.04 和内网测试环境执行；仅构建成功不能视为已完成 AC-12。

## 外网端构建

构建机需有 Python 3.12、uv 0.11.29、Node 26.4.0/npm 11.17.0。两套前端分别在 `frontend/exporter` 与 `frontend/importer` 运行 `npm ci && npm test && npm run build`。产物放在各自 `dist/`，没有 CDN 依赖。

Windows：准备与目标系统架构一致的 Node/npm 完整运行时目录（含 `node.exe`、`npm.cmd`、`node_modules/npm`）及带 pip 的独立 Python 3.12 目录，执行 `powershell -File deploy/exporter/build.ps1 -NodeHome <目录> -PythonHome <目录>`。Linux：准备含 `bin/node`、`bin/npm` 的 Node 目录与兼容目标 glibc、含 `bin/python3` 和 pip 的自包含 Python 3.12 目录，设置 `PACKFERRY_NODE_HOME` 和 `PACKFERRY_PYTHON_DIR` 后运行 `bash deploy/exporter/build.sh`。脚本只复制固定的 Node/npm，不带构建机其他全局 npm 包；PyInstaller 使用 `onedir`，输出 `dist/packferry/`。需在各自 OS 上构建并在无预装 Python/Node 的干净目标机测试；Linux 以受支持的最低 glibc 系统构建/验证。

冻结程序无参数启动桌面界面，命令行参数转入 `export`、`web`、`doctor`。发布前运行 `dist/packferry/packferry doctor`，核对内置 uv、Node/npm、Python 和缓存目录，再执行桌面完整导出。构建时 `runtime-sources.sha256` 记录关键运行时输入哈希。Windows 用 Inno Setup 编译 `deploy/exporter/packferry.iss`，生成每用户安装程序，再用 `hash-release.ps1` 记录安装器 SHA-256；Linux 对同一目录运行 `deploy/exporter/archive-portable.sh` 生成便携 tar.gz，或运行 `deploy/exporter/build-deb.sh` 生成 `.deb`，脚本同时写校验文件。这些是构建配方，需在目标 OS 上实际执行并验收；仓库没有已签名发行包。

## 内网镜像与启动

在可联网构建机安装 Docker，运行 `bash deploy/importer/build-image.sh`。脚本先构建前端，再按 `uv.lock` 构建应用镜像，输出镜像归档及 SHA-256 文件。将归档、校验文件、`deploy/importer/compose.yaml`、`.env`、HTTPS 代理配置经合规渠道送入内网。内网核对哈希后执行 `docker load -i packferry-importer-0.1.0.tar.gz`，无需从公网拉取镜像。

复制 `deploy/importer/.env.example` 为 `.env`，配置真实的 HTTPS Nexus URL 和 Hosted 仓库白名单。`.env` 不保存用户密码。创建 `data/`，授予容器运行用户写权限。执行 `docker compose up -d`；单实例运行一个 API 和一个 Worker，SQLite 数据、Bundle 均在 `data/`。`docker compose ps` 与 `/health/ready` 用于检查进程。

Compose 仅把应用 HTTP 端口映射到宿主机 `127.0.0.1:8000`，使用同机 Caddy/Nginx 终止用户 HTTPS；`Caddyfile.example` 给出证书与反向代理示例。先确认容器内看到的反向代理源 IP，将其填入 `PACKFERRY_TRUSTED_PROXY_IPS`（示例值仅适用于对应 Docker 网络）；不能设为 `*`。部署后从客户端检查 HTTPS 证书及 Host/Origin 行为，确认直连 HTTP 无法从网络访问。Importer 默认拒绝不安全 HTTP；`PACKFERRY_ALLOW_INSECURE_HTTP=1` 仅供隔离测试，生产环境禁止设置。反向代理应传递正确 `X-Forwarded-Proto`、覆盖客户端提供的同名头，端口不得对非代理网络开放。Nexus 本身也必须是可信 HTTPS。

## 日常检查与恢复

- 使用用户本人 Nexus 账号登录。用户只能看自己的任务；维护者白名单可以查看全局审计。浏览器退出会撤销该用户后台凭据，进行中的任务可能进入 `NEEDS_AUTH`，由原任务所有者重新授权。
- 上传 Bundle 后先看预检，再确认发布。上传/确认请求超时，先用操作查询和任务列表核实，不能直接生成新幂等键重新发送。文件结果中 `SKIPPED` 代表远端同字节，`CONFLICT` 和 `UNKNOWN` 需人工核实；不得盲目重传。
- 查询 `docker compose logs --tail=200 importer`、`/health/live`、`/health/ready` 和任务报告。日志轮转由 Compose 限制为 10 MB × 5。
- 数据库在 `data/importer.sqlite3`。备份时用 SQLite Backup API 创建一致性副本，连同配置和仍在保留期/待恢复的 `data/bundles/` 一同备份。建议每日备份数据库，至少保留最近 7 份；不要把 30 天 Bundle 清理策略误用到待授权任务或审计记录。
- 升级前暂停新任务并等待当前文件结束，备份数据库、配置及必需 Bundle，再加载新镜像。启动后核对 `/health/ready`、任务状态与文件报告；回滚程序时使用对应版本的数据库备份。
- 单 Bundle 默认上限 5 GiB；宿主机、反向代理和存储需有足够磁盘空间。达到容量或无法访问配置的 Hosted 仓库时应停止接收任务并修复环境。

## 验收记录

前端自动化校验覆盖本地桌面令牌交换、导出请求字段、结构化错误、内网 CSRF 与幂等键。发行验收仍需逐平台记录：桌面启动与窗口退出、固定工具发现、中文/空格路径、WebView2/Qt 缺失诊断、Bundle 往返；内网普通用户认证与权限、重复同字节/冲突、重启恢复、发布响应丢失；完全断网客户端只使用 Nexus 安装代表包并记录任何公网请求。未跑的场景在验收表中标记“未验证”。
