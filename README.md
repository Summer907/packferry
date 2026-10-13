# PackFerry

隔离网络下的 PyPI/npm 离线包准备和 Nexus Hosted 导入。公网端默认是 pywebview 桌面应用，也支持本地浏览器和 CLI；内网端是 Linux 中心服务，浏览器与远程 CLI 共用任务、授权及审计。

Bundle 是唯一跨区契约：单 ZIP、单生态、原始制品、Manifest、SHA-256 和缺口报告。文件通过现有合规渠道人工传入；程序不跨区联网，不回传内网库存，不执行第三方构建/安装脚本。

## 开发启动

需要 Python 3.12、uv 0.11.29、Node 26.4.0/npm 11.17.0。Windows 桌面需 WebView2 Runtime；Ubuntu 22.04/24.04 桌面使用 PySide6/Qt。

```powershell
uv sync --locked --extra desktop
npm --prefix frontend/exporter ci
npm --prefix frontend/exporter run build
npm --prefix frontend/importer ci
npm --prefix frontend/importer run build
uv run --extra desktop packferry desktop
```

浏览器入口：`uv run packferry web`。它自动启动认证的随机回环端口，并通过 URL fragment 建立本地会话。

```powershell
uv run packferry doctor --json
uv run packferry export --ecosystem pypi --package "urllib3[socks]==2.8.0" --output packages.zip --json
uv run packferry export --ecosystem npm --package "is-number@7.0.0" --output npm-packages.zip --json
```

Exporter 默认重新求解；项目优先锁文件。`--environment` 使用 `id:runtime:version:platform:abi[:libc]`。解析、文件收集、离线安装验证分别报告，下载完成不等于离线可安装。

## 内网服务

在外网构建 Importer 镜像、前端和依赖，再经合规渠道送入内网。`deploy/importer/compose.yaml` 使用本地镜像和持久化目录；生产必须配置 HTTPS Nexus、Hosted 白名单和可信代理 IP。Nexus 用户密码只驻留内存，重启后原任务所有者重新授权。

远程客户端提供 `login / submit / preview / confirm / status / wait / report / authorize`，只调用中心 API。密码通过隐藏输入或 stdin 提供，不写入命令行和 Bundle。

## 验证与交付

```powershell
uv run pytest
uv run ruff check apps packages clients tests
npm --prefix frontend/exporter test
npm --prefix frontend/importer test
```

原生窗口测试须在有桌面会话的环境显式启用：设置 `PACKFERRY_NATIVE_SMOKE=1` 后运行 `tests/test_desktop.py`。真实 Nexus 3.96.4 CE、Ubuntu 原生桌面、干净机器分发和完全无公网安装是独立验收项，不能用模拟测试替代。

- [操作、分发与恢复](docs/operations.md)
- 独立远程 CLI 可用 `deploy/importer-client/build.ps1`（Windows）或 `build.sh`（Linux）构建；只携带 HTTP 客户端依赖。
- [Importer 配置与备份](apps/importer/README.md)
- [当前实现与验收矩阵](docs/implementation-status.md)
- [需求](docs/requirements.md)、[架构](docs/architecture.md)、[历史技术验证](docs/technical-validation.md)
