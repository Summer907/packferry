# 当前实现与验收矩阵

日期：2026-10-13。此记录描述当前仓库和本次可复现检查，不替代历史技术验证，也不将历史 PoC PASS 当作本代码的生产验收。

## 已实现

- Shared Core：严格 Bundle v1 Schema、内容寻址、单生态、多目标去重、原始文件身份与 SHA-256 校验、引用检查、版本拒绝、JSON 重复键拒绝、ZIP 中央目录预检、PAX/GNU 扩展头限额、载体拒绝。
- Exporter：包/项目输入、标准 pylock v1、uv 静态元数据桥接及原制品映射、pip 兜底、静态构建依赖收集、npm/Arborist/pacote、可验证缓存、历史解析复用、独立状态与风险报告、CLI。
- 外网 UI 与桌面：Vue 本地资源、回环随机端口和本地会话、同源检查、原生文件选择/保存、关闭确认与协作取消、中断记录、单实例锁。默认桌面，保留浏览器与 CLI。
- Importer：Nexus 用户身份、内存凭据与短期会话、HTTPS/CSRF、Hosted 白名单、流式限额、预检/确认、持久任务与审计、单 Worker、文件读回核实、幂等查询、UNKNOWN、重启待授权、30 天清理、一致性备份与迁移。
- 分发：锁文件、CI Windows/Ubuntu 矩阵、Windows Exporter 便携目录、独立轻量 Importer CLI、Inno Setup/Linux 包构建配方、Compose/离线镜像与代理配置。

Manifest 的 `resolutions[].dependencies` 是环境内依赖边；`build_dependencies` 单独记录源码构建依赖关系。`packages[].dependencies` 是跨环境关系并集，不应直接当作任一环境的安装图。`tool_versions` 保存工具版本。

## 本次检查

| 检查 | 结果与边界 |
| --- | --- |
| Python 自动化 | 69 passed、1 skipped；跳过项是默认关闭的原生窗口测试。Ruff 通过。 |
| 前端 | Exporter 4 项、Importer 6 项 Vitest 通过；两套生产构建通过。 |
| Windows 原生窗口 | 显式启用原生测试后通过：WebView2 加载本地 Vue 并自动关闭隐藏窗口。源码运行，不等同干净机器冻结包验收。 |
| Windows 冻结程序 | PyInstaller onedir 构建成功；doctor 核实内置 Python 3.12.12、pip 25.1.1、uv 0.11.29、Node 26.4.0、npm 11.17.0。 |
| 冻结 npm 导出 | `is-number@7.0.0`：Windows/Linux 两目标、原始 tarball、解析/收集 COMPLETE；Bundle 通过校验。未运行该包或验证无公网安装。 |
| 冻结 Python 导出 | `six==1.17.0`：CPython 3.9/3.12 × Windows/glibc 2.31 Linux 四虚拟目标，解析/收集 COMPLETE；原始 Wheel 去重。未在四个原生解释器安装。 |
| Importer 整体链路 | 本地 HTTP Nexus 仿真 + Uvicorn + 真实客户端：认证、提交、确认、等待、报告；覆盖响应丢失读回、撤权、重启和并发幂等。仿真不是 Nexus 3.96.4 CE 生产实例。 |

## AC-01～12 对照

| ID | 当前证据 | 尚需正式验收 |
| --- | --- | --- |
| AC-01 | uv 合成依赖/冲突/静态 sdist、真实 Python 样本 | 更广泛项目与 Provider 性能 |
| AC-02 | 四目标标签、markers、去重，冻结程序真实纯 Python 样本 | 原生 ABI/扩展包运行 |
| AC-03 | 可验证文件缓存与历史解析复用测试 | 大缓存、长时间并发运行 |
| AC-04 | requirements/pyproject/pylock、npm lock v2/v3 输入 | 多真实项目锁文件矩阵 |
| AC-05 | Arborist peer/optional、平台审计、原始 tarball、脚本风险 | sharp/esbuild 等原生运行与外部资源行为 |
| AC-06 | Bundle 往返和恶意归档自动化 | 生产容量上限/磁盘压力 |
| AC-07 | 两生态 Publisher、Nexus HTTP 仿真、逐文件结果 | 真实 Nexus 3.96.4 CE 普通账号发布/读回 |
| AC-08 | 重复/冲突/UNKNOWN/幂等测试 | 真实 Nexus 仓库策略和权限矩阵 |
| AC-09 | 两套 Vue 构建、Web/API/CLI 共用中心任务 | 实际 HTTPS 浏览器完整导入 |
| AC-10 | 非交互 CLI HTTP 完整流程/JSON/退出码 | 实际 Agent 调用与长任务 |
| AC-11 | 重启待授权、撤权、响应丢失、单 Worker、备份/清理 | 生产断电、TLS、实际权限撤回与备份恢复 |
| AC-12 | 本次未执行真实无公网安装 | 必须在隔绝公网的客户端仅使用内网 Nexus 实测 |

## 保留的限制

- CPython x64 Windows/glibc Linux 是当前目标标签基线；PyPy、ARM、musl 和特殊 ABI 明确不作为已验证能力。宿主 Ubuntu 22.04/24.04 的桌面包尚未在 Linux 原生环境构建。
- 静态 Python Provider 有候选/包数/下载和构建依赖深度限额；未知或动态元数据报告 PARTIAL。收集源码及静态构建依赖不等于可离线构建，程序不执行构建。
- pylock 的 VCS、目录、通用 archive 源和其他锁文件方言明确不支持；npm Git/本地路径/workspace/直接 URL 不执行源准备，并给出失败或缺口。
- 内网真实 Nexus 地址和测试凭据未在本次提供；Docker、Linux 桌面/包和 Windows Inno 安装器未在对应环境完成验收。
- 浏览器自动化中有一轮 Edge 连接显示空白，未计作 UI 通过；静态资源、原生 WebView 页面和真实 Exporter API 流程分别检查通过。

## 上线前需要的条件

真实 Nexus 3.96.4 CE 测试实例、普通用户及仓库白名单、受信任 TLS、Windows 干净虚拟机、Ubuntu 22.04/24.04 桌面与 Docker 环境、禁止公网访问的安装客户端。上述条件齐备后按对应 AC 项运行并保存环境、命令、工具版本、文件哈希和结果；缺项继续保留 PENDING，不能宣布生产验收完成。

## 本机生成的交付文件

- `dist/PackFerry-0.1.0-windows-x64.zip`：Windows Exporter 便携目录，解压后运行 `packferry/packferry.exe`。
- `dist/PackFerry-ImporterCLI-0.1.0-windows-x64.zip`：独立内网 API 客户端，解压后使用 `packferry-import/packferry-import.exe`。
- `dist/SHA256SUMS.txt`：上述两个 ZIP 的 SHA-256。

`dist` 是本机构建产物，未纳入 Git；构建配方、源码、依赖锁和测试均在仓库。Windows WebView2 仍是安装前提。生产部署/发布及 Git 提交尚未执行。
