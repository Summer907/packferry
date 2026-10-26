# PackFerry 桌面工作区

本轮同时重构 Exporter 和 Importer 的视觉、交互与任务体验。两端仍使用 Vue 3 + Vite + TypeScript，分别构建，静态资源无需 CDN。共享源码位于 `frontend/shared`，在构建时编入各自 dist，不需要运行共享服务。

## 操作变化

### Exporter

- 侧栏提供新建导出、最近任务、当前任务和环境收藏。切换页面不停止任务，也不清空当前输入。
- PyPI/npm 保留各自的本轮输入草稿与目标列表。
- 目标环境由用户填写运行时、解释器版本、平台和 ABI；高级参数提供 libc 和环境 ID。没有自动选中的固定 Python 组合；平台建议值可自由修改。
- 环境可命名收藏、应用、编辑更新和删除。收藏存于 Exporter 数据目录的 SQLite 中，重启后保留，不包含账号凭据。
- 项目路径与直接约束输入互斥。保留桌面文件选择、锁文件策略、历史解析复用及 npm 生产依赖选项。
- 历史支持状态筛选、分页和任务 ID 查询；终态任务可以按原配置创建新任务，记录原任务关联。
- Bundle 生成完成与内容完整性分开显示；缺口 Bundle 可保存，离线安装未验证时明确显示“未执行离线安装验证”。

### Importer

- 使用 Nexus 身份登录；目标仓库继续来自服务端允许列表，现有 Nexus 配置方式不变。
- ZIP 可拖放或点击选择，先上传预检，后核对仓库、风险和完整性并明确确认。
- 上传百分比来自浏览器真实传输事件；传输结束后显示服务端预检，不冒充预检进度。预检完成前尚无持久化任务，关闭页面后不能恢复未完成的上传。
- 上传和确认先持久化操作键；网络错误或 5xx 后先查询结果，阻止重复提交。待核实操作与最近任务按当前账号保存于浏览器会话中。
- 文件结果支持筛选、分页；全量计数与当前页条数独立。已发布并核验、同内容跳过、失败、冲突和结果未知分别呈现。
- 中断恢复继续使用原有重新授权机制。终态失败使用原 Bundle 创建新任务，不新增逐文件重试语义；远端同身份检查和未知结果保护保持原样。

## 状态与诊断契约

Exporter 阶段为排队、准备输入、解析与制品收集、完整性汇总、Bundle 生成。解析适配器可能交错求解和下载，因此没有虚构独立线性解析/下载进度，也没有总任务百分比。

Importer 阶段为预检与用户确认、排队、发布与逐文件核验。发布前检查、发布、发布后核验按原逻辑逐文件执行。文件处理进度统计本轮已有处理结果的数量，失败、冲突和未知均单独列出，不能将进度条视为成功率。

新增任务观测字段 `progress` 包含 `stage`、`operation`、可空的 `completed` / `total` 和 `updated_at`。缺少可靠分母时不显示进度条。旧 Exporter 任务没有阶段数据时仅显示总体状态，不补造历史。

任务详情按需读取脱敏事件和完整报告。事件日志不是完整子进程 stderr；不会采集或展示未经脱敏的下载地址、令牌和密码。现有文件错误与报告入口继续保留。

两端使用轮询，运行任务约每 2 秒更新；断连退避至最多 30 秒，保留最后状态。任务切换时忽略旧响应，终态停止该任务的状态轮询。Importer 仍可更新历史列表以发现其他任务变化。

## API 与数据库兼容

Exporter 在原 SQLite 中增量增加任务的 progress、updated_at、rerun_of 字段及 task_events、favorites 表，启动时自动兼容旧库。新建库与升级均不修改 Bundle manifest。

新增 Exporter 接口：

- `GET /api/v1/exports?limit=25&offset=0&status=...`
- `POST /api/v1/exports/{id}/rerun`
- `GET /api/v1/exports/{id}/events?limit=100&offset=0`
- `GET /api/v1/favorites`
- `PUT /api/v1/favorites/{id}`，请求为 `{name, environment}`
- `DELETE /api/v1/favorites/{id}`

这些接口沿用本机会话与同源保护。已有创建、详情、保存 Bundle 接口兼容保留。

Importer 不需要数据库迁移。详情增量提供 `progress` 与全量文件 `counts`；任务列表与文件列表增加 status 筛选；新增 `GET /api/v1/imports/{id}/events` 并复用任务所有者/维护者权限。该事件接口只输出事件名称、制品标识和时间，不输出任意审计 detail。

不新增消息队列、Redis、SSE 或 WebSocket，不改变解析规则、Bundle 格式、导入语义及 Nexus 仓库协议。

## 构建与验证

在完整仓库内分别进入 `frontend/exporter` 和 `frontend/importer` 执行 `npm ci`、`npm test`、`npm run build`。共享目录仅在源码构建时需要；部署产物仍是各自 dist。原桌面和 Importer Docker 构建路径继续使用各自构建结果。

后端执行 `uv sync --frozen` 与 `uv run pytest`。新增回归覆盖旧库升级、收藏重启保留、原配置重跑、事件权限、全量计数、自由环境、生态草稿、真实传输计数、结果待核实保护和过期响应竞争。

本轮浏览器检查使用本机实际 FastAPI 服务和生产静态文件，核对多种窗口宽度、无外部静态请求、真实 PyPI 导出及测试 Nexus 导入闭环。测试 Nexus 不替代真实 Nexus 实例验收；桌面窗口与 Windows/Linux 安装包仍需在相应运行环境中进行发布验收。

### 2026-10-25 验证记录

- Python：72 passed、1 skipped；修改的 Python 文件通过 Ruff。
- Exporter：7 项 Vitest 通过；Importer：12 项 Vitest 通过。两端 TypeScript 检查和生产构建均通过。
- 本机生产页面实际完成 `packaging==25.0` 的 PyPI 导出及 `is-number@7.0.0` 的 npm 导出；两者解析与制品收集均完整，离线安装验证仍如实显示未执行。
- Importer 通过真实页面完成 ZIP 上传、预检、确认、测试 Nexus 发布与核验，并验证刷新后重新打开任务。
- 650、1024、1440 宽度页面无横向溢出；1024/1280 下 125% 和 150% 缩放检查无横向溢出。浏览器无脚本错误，静态资源无外部请求。
- 未进行真实 Nexus 实例验收、离线安装验证或 Windows/Linux 桌面安装包实机验收。
