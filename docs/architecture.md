# Offline Package Sync — 系统架构与技术设计

> v0.3 精简实施版｜2026-10-12｜基于 `architecture(2).md` v0.2 调整模块职责，减少对包管理器能力的重复实现；**保留全部已确认决策及需求语义**。需求以 `requirements.md` 为准，PoC 结论以 `technical-validation.md` 为准。Nexus 后续仅针对 **3.96.4 CE** 验证。
>
> **状态规则**：`已确认` 仅经用户批准方可变更；`设计基线` 可依据验证优化；`待 PoC` 不能视作已实现。本文 API、表结构、CLI、Schema 均为拟议契约，需分阶段定稿。若技术验证与需求冲突，先记录 ADR，未经确认不修改需求。

## 1. 架构原则、边界与拓扑

**目标**：公网侧按 PyPI/npm 包、项目和目标环境解析依赖并收集原始发行文件；生成可校验 ZIP Bundle，经既有合规渠道人工传入内网；内网中心服务以真实 Nexus 用户身份批量发布到 Hosted，提供 Web/CLI、审计和故障恢复。

**硬约束**：

- 单向隔离：外网和内网不跨区联网、不共享数据库/缓存/文件系统；内网库存和数据不回流。Importer 不访问公共 Registry，不负责重新求解。
- 支持 PyPI + npm；外网 Windows/Linux 桌面单机；内网 Linux 无头中心服务，Windows/Linux 浏览器和远程 CLI；约 15 人使用。
- Bundle v1 **单 ZIP、单生态**（`pypi` 或 `npm`），允许多个目标环境及制品去重；不携带原始项目依赖文件、Nexus 地址、内网凭据或导入状态。
- 不执行不可信第三方安装/构建脚本；无法完整求解/收集时显式报告，不伪称完整或已验证离线可安装。
- **复用优先**：uv、pip、npm 负责版本求解和现有协议语义。自研仅实现适配、跨环境合并、原始文件交付、Bundle、Nexus 业务核实、用户任务与审计；不自行实现 Python/npm 版本求解器或通用 Registry 协议。

```text
公网 Windows/Linux（Exporter 单机）
  Vue Web → localhost FastAPI ┐
  Exporter CLI ───────────────┴→ ExportService
       → PythonAdapter(uv + pip + packaging)
         / NpmAdapter(npm CLI；必要时 Arborist/pacote)
       → 原生工具缓存 + Artifact Store(原始发行文件/SHA-256)
       → BundleBuilder → ZIP
                ══ 既有合规渠道，人工单向传入 ══
内网 Linux（Importer）
  Web / 远程 CLI / Agent → HTTPS → FastAPI → SQLite + Bundle Store
                                         → 单进程持久 Worker
                                         → PyPI/npm Publisher → Nexus 3.96.4 CE Hosted
```

**信任边界**：外部 ZIP、Registry 元数据、原始制品、Nexus 响应均是不可信输入。SHA-256 只证明字节一致，不能证明来源真实。预检是快照，正式发布前必须再次检查 Nexus。

## 2. 技术栈、目录与最小自研边界

| 层 | 选型 | 职责边界 |
| --- | --- | --- |
| 前端 | Vue 3 + Vite + TypeScript（两套独立 Web UI，已确认） | 外网准备/导出，内网上传/审计；共享组件可选，业务状态分离 |
| 后端 | Python 3.12、FastAPI、Pydantic v2/JSON Schema、HTTPX | API、服务编排、数据校验；与目标 Python 版本独立 |
| Python 生态 | uv CLI + pip + `packaging`；Simple Registry API 兜底 | **uv 求解**；pip 优先完成适配目标的 Wheel 下载/选择；`packaging` 做兼容性检查与兜底，不重写 pip/uv 算法 |
| npm 生态 | 固定版本 Node/npm CLI；必要时 `@npmcli/arborist` / `pacote` | **npm 求解**；不足时调用其官方底层库读取依赖树/原始 tarball，不手写 peer/optional 求解 |
| 原始制品 | Artifact Store（SHA-256 寻址）+ uv/npm 原生缓存 | 原生缓存服务求解/下载；Artifact Store 仅保证可复用的**原始发行字节**及来源索引 |
| 持久化 | SQLite WAL + SQLAlchemy 2.x + Alembic、本地文件 | 任务、文件结果、审计、幂等与恢复；无 Redis/Celery |
| CLI/测试 | Typer；pytest / Vitest | 两端结构化输出、稳定退出码与自动化验收 |
| 部署 | Exporter 便携/安装；Importer Docker Compose 单应用容器 | 导出端离线打包运行时；导入端 API+Worker 同进程 |

**约束**：锁定 Python、uv、pip、Node/npm 及前后端依赖版本，任务记录 Resolver 版本；工具升级须回归相关 PoC。Arborist/pacote、pylock、pip 选择方案为**优先验证方向，非已经确认的替代实现**。

目录保留 Monorepo 和职责边界，不要求为每个名词创建独立类或包：

```text
offline-package-sync/
  apps/exporter/{api,cli,services,adapters,storage}/
  apps/importer/{api,services,workers,publishers,persistence,security}/
  packages/common/{bundle,models,hashes}/
  clients/importer_cli/
  frontend/{exporter,importer,shared}/
  tests/  deploy/{exporter,importer}/
  requirements.md  architecture.md  technical-validation.md
```

- `common` 只放纯逻辑 Schema、哈希与 Bundle 校验，不依赖框架/DB/Resolver/Nexus；Exporter 与 Importer 独立依赖它，互不调用。
- Exporter Web/CLI 共用 `ExportService`；Importer Web/CLI 只通过中心 HTTP API。Importer CLI 不直连 Nexus。
- 自研核心限于 `ExportService`、生态 Adapter、`ArtifactStore`、`BundleBuilder/Validator`、`ImportService`、Worker、生态 `Publisher`。恢复调度优先放在 Worker/Service，不额外建复杂框架。

## 3. Exporter：求解、下载与风险报告

**运行模型**：本地 Web 使用 `127.0.0.1` FastAPI（内置 Vue 静态文件），后台异步处理任务；须验证 Host/Origin 并使用随机本地会话/启动令牌，避免恶意网页调用。CLI 直接调用同一 `ExportService`，不依赖常驻 Web。缓存跨进程写入需锁/原子替换。

**统一语义**：

- 输入单包/多包与约束，或 Python `requirements.txt`/`pyproject.toml`、npm `package.json`/`package-lock.json`。保留 extras、markers、peer、overrides 等语义，禁用/诊断危险特殊指令。
- **普通包默认重新解析当前满足约束的版本**，不因新版缺 Wheel 而静默降级；可选复用历史或锁定版本。**项目默认优先受支持 lockfile**，可手动重新解析；无锁文件按声明求解并记录结果。
- 多 Python 目标环境分别求解，再合并字节相同的制品。npm 默认生产+开发依赖，可选仅生产（必须按依赖可达关系裁剪，不能将 lockfile 全量视为生产依赖）。
- 每个环境分别记录解析/文件收集状态、依赖关系、风险及受影响节点；下载成功不代表离线安装成功。私有 URL、鉴权信息、Git/直链/本地路径/workspace 必须识别并脱敏，严禁写入 Manifest 的凭据字段。

### 3.1 PythonAdapter

1. uv 是**唯一主要求解引擎**。按目标解释器、Python 版本、ABI、平台独立求解并取得固定版本结果；普通包使用全新临时输出或 `--upgrade` 等可验证机制，防止无意沿用历史锁定版本。
2. **优先 PoC：PEP 751 `pylock.toml` 作为机器可读中间结果**。仅在测试证明其输出含所需版本、环境、文件与可靠依赖边后使用；缺字段时使用可靠 Core Metadata / Simple API 补充并标记来源，禁止推断完整闭包。
3. 制品选择优先调用 pip/uv 的现成能力。对已固定版本，优先使用 `pip download` 的目标版本/实现/平台/ABI 参数与 `--no-deps` 收集兼容 Wheel；其表达不了的目标环境或候选排序才用 `packaging` + Simple API 兜底。**不可直接用宿主机 `sys_tags()` 推断目标兼容性**。每个已选版本/环境默认只选一个最优兼容 Wheel；跨环境按 SHA-256 去重。
4. 无 Wheel 时尽力收集 sdist、静态元数据与可识别的构建依赖。不运行第三方构建脚本；动态元数据或构建要求未知时记录 `PARTIAL/UNRESOLVED`。uv `--no-build` 可能排除高版本 sdist-only 候选，**`PY-02.C` 未通过前不得将正常退出视作完整求解**。
5. 目标范围：重点 CPython 3.9/3.12 × Windows x86_64 / Ubuntu 20.04 x86_64（glibc 2.31），配置范围 Python 3.9～3.14。应接受兼容的更低 manylinux 基线；PyPy、musl、ARM、特殊 ABI/libc 只能按 PoC 限定支持。

`pip download` 跨平台模式有 `--only-binary=:all:` 或 `--no-deps` 限制；sdist 回退是独立路径，不能假设 pip 一步下载完整跨平台源码/构建闭包。

### 3.2 NpmAdapter

1. 固定 Node/npm 版本；普通包在隔离临时项目中求解，项目优先支持 `package-lock.json` v2/v3；可用 `npm install --package-lock-only --ignore-scripts` 等命令，实际生命周期行为必须验证。不得直接对不可信源码目录执行可能触发脚本的打包命令。
2. npm CLI **负责版本/peer/optional 求解**。当 CLI 输出不足以表达安装实例路径、依赖边与生产/开发/optional 可达关系时，再使用 `@npmcli/arborist` 读取其树；保留每个安装路径实例，不将 `name@version` 当唯一依赖节点。
3. 仅收集 Registry **原始 tarball**，优先复用 npm/pacote 获取字节、`integrity`、现有缓存；另算 SHA-256 存入 Artifact Store。不重新打包第三方 npm 包。跨 Windows/Linux x64 分析 `os/cpu/libc`、peer、optional；锁文件丢失跨平台 optional 时重新解析/对比并报告缺口。
4. 记录安装脚本、原生模块、额外下载及 Git/直链/本地路径/workspace 等特殊源风险。重点 PoC：`sharp`、`esbuild`、peer 冲突、锁文件中公网 `resolved` 地址在内网 Registry 的行为，以及完全无公网的 `npm ci`。

### 3.3 Artifact Store 与状态

- uv/npm 原生缓存按工具自身机制运行；**不将原生缓存视为可重建 Bundle 的权威原始文件库**。
- 自研 Artifact Store 只做原始文件 SHA-256 寻址、逻辑包/版本/文件名/来源索引、下载前可验证复用、临时文件校验后原子替换，不重新实现 HTTP 缓存协议。导出前重新检查文件存在性和 SHA-256。
- 解析失败、下载失败均需报告**包、环境、原因、影响范围**；允许带明确缺口的 Bundle，但不能静默遗漏。

## 4. Bundle v1：唯一跨区数据契约

**约束**：ZIP 内只能有一个生态；允许多个环境、同名不同版本；按物理 SHA-256 只存一份内容，不丢失不同逻辑制品的发布身份。ZIP 总 SHA-256 由 Importer 接收后计算用于审计（与 Bundle ID 区分）；`format_version=1`，不支持的版本明确拒绝。

```text
manifest.json
artifacts/sha256/<前2位>/<完整64位SHA256>
reports/summary.json
```

**Manifest v1（字段与 JSON Schema 在 Phase 1 定稿）**：

| 对象 | 核心字段 |
| --- | --- |
| `bundle` | `format_version, bundle_id, created_at, ecosystem, generator_version` |
| `input` | `mode, normalized_constraints, resolution_policy, lock_policy, original_lock_fingerprint?` |
| `environments[]` | `environment_id, runtime, version, platform, abi/libc` |
| `packages[]` | `node_id, name, version, source, dependencies[]`；关系未知需标示 |
| `artifacts[]` | `artifact_id, filename, type, archive_path, size, sha256, source, integrity?` |
| `resolutions[]` | `environment_id, nodes[], selected_artifacts[], status, warnings[]` |
| `warnings[]` | `code, severity, affected_environment_ids, affected_node_ids, message` |
| `verification` | `resolution_status, collection_status, offline_install_status` |

`reports/summary.json` 只从 Manifest 派生；不携带原始 `requirements.txt`/`pyproject.toml`/`package.json`/`package-lock.json`、敏感地址、密码、内网配置或会话信息。已下载、存在缺口、已验证离线可安装必须可区分；导入状态属于 Importer，独立于上述三个状态。

**安全校验**：对外部 ZIP 流式限制总大小、条目数、单文件/总解压量、压缩比；拒绝绝对路径、`..`、编码/反斜杠穿越、symlink/特殊条目、重复和大小写冲突、未声明/缺失文件、悬空引用；先严格 Schema 和业务引用，再校验长度、SHA-256。只读检查 Wheel/sdist/npm 包内部身份及必要元数据，限制读取大小与解析深度；不执行归档脚本。缺口可解释的 Bundle 可通过结构校验，但不等于离线可安装。

## 5. Importer：身份、任务与 Nexus 发布

**执行模型**：单 Uvicorn 进程 + 内置持久 Worker；SQLite 为恢复依据，内存仅负责调度与短期 Nexus 凭据。建议初始为**单任务、单文件串行**。操作意图、任务/文件状态及审计先写 DB，网络 I/O 放事务外；响应后更新结果。Worker 在启动后扫描中断状态，重新核实并恢复，不另建分布式队列或独立 Recovery 框架。

### 5.1 状态与幂等

- 任务：`RECEIVED → VALIDATING → READY → QUEUED → RUNNING`；终态 `COMPLETED / PARTIAL / FAILED / EXPIRED`；特殊 `NEEDS_AUTH / INTERRUPTED`。READY 必须由用户预览确认；未知结果不得记为 COMPLETED。
- 文件：`PENDING → CHECKING → UPLOADING → VERIFYING → UPLOADED`；另有 `SKIPPED`（**同内容已核实**）、`CONFLICT`（异内容）、`FAILED`（确定失败）、`UNKNOWN`（结果不明）。401/403/5xx/查询失败均不得当成远端不存在；HTTP 409 不代表内容一致。
- 同文件发布前、发生 TOCTOU/冲突/超时/响应丢失后均应再查询核实。`UNKNOWN` 先对账，失败时不盲目重发。永久性文件级错误记录并继续独立文件；明确暂时性 429/503 等最多**额外自动重试 2 次**；整体鉴权失效、错仓、空间不足等暂停/终止并保留现场。
- 重启时保留已经核实的 UPLOADED/SKIPPED；其他上传中的文件先对账。仅原任务所有者可重新授权；内存凭据消失转 `NEEDS_AUTH`，不能无凭据继续发布。
- `Idempotency-Key` 绑定操作者、请求指纹和一次逻辑提交/确认：同 key 同指纹返回原结果，异指纹冲突。超时需查询操作结果，**不得自动重复 POST**；上传中断和操作查询契约在 Phase 3 定稿。
- **同一 ZIP 可被不同人/不同仓库/不同时间主动多次提交**，均产生独立任务和不可变审计；Bundle ID、ZIP 哈希与任务 ID 不混用。MVP 不做终态任务复杂文件级原地重试，可新建任务并按实际远端内容去重。

### 5.2 Publisher：只封装必要差异

按生态各实现：

`validate_repository → find_artifact → verify_existing → publish → reconcile_unknown`

- 底层发布尽量使用 Twine、npm CLI 或经过 PoC 的 Nexus/Registry API，**不自行重新实现完整 PyPI/npm Registry 协议**；明确调用方身份、目标仓库及异常分类。
- 发布前根据**逻辑制品身份 + 原始文件字节**核实远端。同身份同 SHA-256 可 `SKIPPED`，不一致 `CONFLICT`，无法可靠读取/确认则 `UNKNOWN`；不能只凭 `name@version`、HTTP 200/409 或 Nexus metadata 判断内容一致。
- npm 需证明上传保持原始 tarball、不会执行不可信 lifecycle scripts。Wheel、sdist、npm tarball 的上传/下载、重复/冲突及普通用户权限以 **Nexus 3.96.4 CE PoC** 为准；已通过部分样本不代表适配器整体完成。

### 5.3 身份、权限与传输

- Nexus **本地用户名+密码**，无个人 User Token；约 15 人按 Nexus 原生权限操作，MVP 不另建账号体系/审批。Importer 登录后在内存短期复用用户凭据，Worker **代表真实任务所有者**访问 Nexus；不使用共享高权发布账号。
- Web：安全 Cookie（HttpOnly/Secure/SameSite）、Origin/CSRF；CLI：短期 Bearer 访问 Importer API。Worker 凭据不入 DB；重启、失效后原 owner 重授权。Importer 会话 Token **不能当作 Nexus 发布密码**。
- CLI 密码经隐藏输入、stdin 或受控凭据文件等安全渠道提供；禁止命令行明文密码、明文持久保存、写入日志/Bundle/审计。登录会话和已授权后台任务生命周期分别管理，关闭浏览器不自动终止任务。
- 登录校验必须验证**用户名身份**，并覆盖错误密码/匿名/禁用/权限不足。`/service/rapture/session` 等非正式接口能否稳定用于 3.96.4，须通过 PoC，不能以匿名仓库 200 或管理员专有 API 冒充验证。
- 用户仅查询自己的任务；维护者经服务端 Nexus 用户名白名单查看全部历史/审计。Hosted 目标仓库由管理员白名单配置，禁止任意 URL（SSRF 防护）。用户至 Importer、Importer 至 Nexus 的凭据传输必须有 TLS 或已验证等效加密；裸 HTTP 是上线阻塞。

## 6. HTTP API 与 CLI 契约（拟议）

- REST/JSON `/api/v1`，FastAPI/OpenAPI 3.1；Web Cookie、CLI Bearer；所有接口验证 owner/权限。写操作持久化，异步结果以轮询获取；MVP 不做 WebSocket/SSE。统一脱敏错误 `{error:{code,message,retryable}}`，历史/文件/审计分页。
- API：`POST /auth/login`、`POST /auth/logout`、`GET /auth/me`、`GET /repositories`；`POST /imports`（流式上传与幂等）；`GET /imports/{id}`、`/preview`、`/files`、`/report`；`POST /imports/{id}/confirm`、`/authorize`；`GET /imports`、`GET /audit/events`。所有路径加 `/api/v1` 前缀，正式契约含上传超时核实/状态操作查询。
- 状态码：200/201/202 成功、400/422 请求违法、401 会话失效、403 无权限、409 状态/幂等冲突、413 太大、429/503 暂时不可用。Nexus 文件级失败记录到任务，不能统一映射成 HTTP API 失败。
- Exporter CLI（便携/安装均含）：`export / web / doctor`；Importer 远程 CLI：`login / submit / status / wait / report`，支持非交互、`--json`、提交后立即返回或等待、稳定退出码（成功/部分/鉴权/格式/网络/超时/内部错误）。CLI 和 Web 使用同一真实身份和任务审计。

## 7. 存储、保留与运维

**SQLite**：WAL 仅用于本地文件系统，短事务，网络 I/O 不占 DB 事务。最少语义表：`bundles`（ZIP/校验）、`import_tasks`（owner/仓库/状态）、`task_files`（原始文件/发布/核实）、`audit_events`（actor/事件）、`idempotency_records`（键/指纹/提交结果，可按实现合并）。状态与审计同事务，建立必要索引和唯一约束。

**目录基线**：`/opt/offline-package-sync/{compose.yaml,config/importer.yaml}`；`/var/lib/offline-package-sync/{db/importer.db,bundles/<id>.zip,staging/<id>.part,work/<id>/}`；`/var/backups/offline-package-sync/`。DB、待恢复 Bundle 和配置须在容器外挂持久化；staging → 正式存储尽量同文件系统原子替换。

**保留（已确认）**：已结束任务的 Bundle 默认 **30 天**、可配置；未完成/待授权/中断任务不清理；审计和文件级结果长期保留。同 ZIP 被多任务引用，所有任务结束后从最近完成时间起算 30 天；清理幂等且可审计，清理后仍可查历史报告。未经验证的建议：READY 7 天过期、孤立 `.part` 24 小时清理、单 Bundle 上限 5 GiB、轮询 2 秒。

**运行与运维**：Importer Docker Compose 单应用容器（API + Worker 同进程），必要时使用外部 Nginx/Caddy 提供 HTTPS；非 root、限制上传大小/超时/解压/磁盘低水位。容器镜像及依赖离线构建后经合规渠道传入，内网 `docker load`，运行时不拉公网镜像/安装包。结构化脱敏日志、`/health/live` 与 `/health/ready`；Docker 日志轮转建议 10 MB × 5。SQLite Backup API 一致性备份，连同配置/必需 Bundle，建议每日 DB 备份并保留最近 7 份及适量周期备份；不得用备份策略无意绕过 30 天 ZIP 保留决策。升级前暂停新任务并处理当前文件、备份 DB、迁移后核实恢复，DB 回滚需对应备份。

**配置**：`importer.yaml` 为 Hosted 白名单、限额、维护者、并发、保留期；环境变量为路径、网关/TLS 等差异，不存用户密码。建议单任务/单文件并发各 1，自动额外重试 2 次，结束 Bundle 保留 30 天；不符合配置、目录权限或错生态仓库时拒绝启动/显式不可用。

## 8. 分发与工程阶段

**Exporter 已确认只有两种正式分发**：

- **便携版**：Windows ZIP、Linux tar.gz，均为 PyInstaller `onedir`，自带 Python、uv、Node/npm、前端资源及 CLI。
- **安装版**：复用同一便携程序，Windows 用 Inno Setup 等封装、Linux 用支持发行版的 `.deb` 等（范围待定）；不单独维护第三套 Python 包发行渠道。Python Wheel 只用于内部构建/CI。

Windows/Linux 在各自 OS 构建，Linux 测试最低受支持 glibc；用户缓存/配置使用 Windows APPDATA/LOCALAPPDATA 或 Linux XDG。`doctor` 检查内置工具版本/路径/缓存权限。Importer CLI 独立轻量 Python 包，优先从内网 Nexus PyPI 安装；出现无 Python 环境需求再考虑独立二进制。

### Phase 0：必须先完成的验证

| ID | 验证点 / 阻塞边界 |
| --- | --- |
| `POC-NEXUS-01` / `POC-NPM-02` | 3.96.4 CE 普通用户身份/权限；Wheel/sdist/npm 原始文件发布、读回、哈希、同字节/异内容重复、npm 脚本；无法证明安全身份或字节一致则不上线 |
| `POC-PY-01` | CPython 3.9/3.12 × Windows/Ubuntu 20.04 markers、tags、ABI/manylinux，uv/pylock 真实输出；pip 优先选择路径能否覆盖 |
| `POC-PY-02`（含 `PY-02.C`） | sdist-only 最高候选、静态元数据、禁构建情况下的依赖闭包；不完整只能明确标记并提交 ADR |
| `POC-NPM-01` | lockfile v2/v3、peer/optional、生产/开发裁剪、跨平台与原生包、npm CLI 与必要的 Arborist/pacote 行为 |
| `POC-E2E-01` | 无公网的 Python/npm 客户端仅通过内网 Nexus 安装；覆盖 npm `resolved` URL、外部资源与安装脚本风险 |

新增复用验证并入对应 PoC：① uv 输出的依赖边/制品身份是否够用；② pip 与目标兼容标签的选择是否一致；③ npm 下载是否保留原始 tarball；④缓存复用后导出字节是否一致。PoC 结果和限制只记入 `technical-validation.md`，不能凭局部 PASS 推断全生态通过。

### Phase 1～5：交付顺序

| 阶段 | 交付物 / 退出标准 |
| --- | --- |
| 1 Shared Core | Bundle v1 JSON Schema、读写与安全校验、正反黄金样本、引用/去重正确 |
| 2 Exporter CLI | Python/npm Adapter、Artifact Store、Bundle、风险报告，四目标环境与代表性 npm 样本通过 |
| 3 Importer | Nexus 身份、API/SQLite/Worker、预检/幂等、UNKNOWN、重启恢复与报告；无 UI 可完成导入 |
| 4 Web + CLI | Vue 双 UI、内网轻量 API CLI，统一操作身份、任务与审计 |
| 5 交付与验收 | Windows/Linux 便携/安装、Compose、备份/升级、断网安装与故障恢复测试 |

需求映射：`AC-01～05` Exporter；`AC-06` Bundle；`AC-07～08` Nexus/幂等；`AC-09～10` Web/API/CLI；`AC-11` 恢复/授权；`AC-12` 完全离线安装。MVP 不扩展至 Electron、HA、分布式队列、全站镜像、自动跨区传输、分卷或不可信源码自动构建。

## 9. 决策登记与开放问题

### 9.1 已确认（16 项，原 ID 与含义保留）

| ID | 决策 |
| --- | --- |
| ARCH-01 | Monorepo；独立 Exporter/Importer；轻量纯逻辑 Shared Core。 |
| ARCH-02 | Importer 单进程 API + 内置持久 Worker，不用外部队列。 |
| PY-01 | 每环境/每已选版本默认仅选一个最优兼容 Wheel；同一文件跨环境去重。 |
| PY-02 | 版本优先，缺 Wheel 不自动降旧包。 |
| NPM-01 | npm 重点 Windows 与 Ubuntu 20.04 双平台。 |
| NPM-02 | npm 项目默认生产+开发，可选择仅生产。 |
| BND-01 | Bundle v1 单生态。 |
| BND-02 | Bundle 不携带原始项目依赖文件。 |
| IMP-01 | 文件级 best-effort，独立文件继续，允许 `PARTIAL`。 |
| IMP-02 | 单文件临时错误最多额外自动重试 2 次；未知结果先核实。 |
| AUTH-01 | Nexus 本地账号密码；无个人 User Token。 |
| AUTH-02 | 用户登录一次，Nexus 凭据短期内存复用；失效/重启重新授权。 |
| UI-01 | Vue 3 + Vite + TypeScript，两套独立 Web UI。 |
| DIST-01 | Exporter 正式分发便携版 + 安装版，均含 Web/CLI；不另发正式 Python 包。 |
| DEP-01 | Importer Docker Compose 单应用容器。 |
| DEP-02 | 结束 Bundle 默认保留 30 天；未完成不清理；审计长期保留。 |

### 9.2 设计基线（9 项，实施前按验证收敛）

| ID | 方案 / 完成条件 |
| --- | --- |
| STACK-01 | Python 3.12/FastAPI/Pydantic v2/HTTPX，通过原型兼容性验证。 |
| STACK-02 | SQLite WAL/SQLAlchemy 2.x/Alembic，通过恢复/迁移/备份测试。 |
| PY-03 | uv + pip + `packaging` + Simple API；优先评估 pylock，PoC 明确分工。 |
| NPM-03 | npm CLI + lockfile + 原始 Registry tarball；必要时 Arborist/pacote，跨平台 PoC 通过。 |
| BND-03 | 内容寻址 ZIP + JSON Schema v1，Phase 1 契约测试通过。 |
| API-01 | REST/OpenAPI + 轮询 + Idempotency-Key，超时恢复/接口测试通过。 |
| AUTH-03 | Nexus 原生权限 + Importer Session/内存凭据，身份/授权路径验收。 |
| DIST-02 | PyInstaller `onedir` + Windows 安装器/Linux 包，双 OS 冒烟。 |
| OPS-01 | YAML/环境变量、日志轮转、一致性备份，部署恢复测试。 |

**开放问题**：3.96.4 CE 身份验证与普通用户权限、禁用后行为、sdist/npm 发布边界；无构建代码条件下的 sdist-only 解析；pylock/不同 Python lockfile 方言；PyPy/ARM/musl/特殊 ABI；跨平台 npm optional/peer、URL 与安装脚本；大文件上传的幂等/查询/断线指纹；会话与后台授权期限及登出语义；容量/磁盘/备份预算；Windows/Linux 支持下限；最终 Schema/API/CLI 退出码。

**变更控制**：验证记录进入 `technical-validation.md`；涉及已确认决策或需求变更时，先形成 ADR（方案、影响、代价）并取得用户确认。本文不复制详细 PoC 日志。
