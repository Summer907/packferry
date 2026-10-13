# Offline Package Sync — Technical Validation

> 版本：v1.4 uv 桥接扩展实测｜日期：2026-10-12｜状态：**原基线 Phase 0 / PASS；uv 桥接限定验证已扩展，生产 Provider/Bundle 契约仍待完成；不代表生产验收**  
> 基线：`requirements.md` v0.2、`architecture.md` v0.2（均由原 v0.1 等义压缩，已确认决策不变）。**后续 Nexus 验证仅针对 3.96.4 Community Edition**；已有外部 3.96.4 测试实例优先。其他版本的历史数据不用于判定本轮 PASS。Python/uv/npm 的本地隔离 PoC 不要求运行在 Nexus 主机上。  
> 状态：`PASS` 实测达到限定范围标准；`PARTIAL` 部分通过；`FAIL` 未达标；`PENDING` 未执行；`BLOCKED` 缺条件。风险被成功复现 ≠ 方案通过。

> 2026-10-12 增量范围：针对 `architecture_v2.md` 的 pip/Arborist/pacote/缓存/uv 桥接及扩大离线闭环进行实验；**未将 v2 自动升级为已批准架构，也未替换已批准的 B 路线**。新增证据见 `v2-*.json`；2026-10-11 历史证据原样保留。

## 1. 目标、边界与准入

目标：用最小可复现的 PoC，确认 **Python/npm 离线依赖收集 → Bundle → Nexus Hosted 发布 → 离线安装** 的核心技术可行性；判断重大不确定性是否已降低到可以开始 Phase 1（Shared Core）的程度。

- **硬性约束**：外网文件只按合规路径进入内网；不设计内网数据回流。Bundle v1 为单生态 ZIP。普通包同步遵循**版本优先**，缺 Wheel 不得静默选择旧版并宣称完整；不能执行不可信第三方构建/安装脚本。`解析 / 文件收集 / Nexus 发布 / 离线安装` 四种状态相互独立。
- **Nexus 范围**：仅测 3.96.4 CE 外部测试实例。`GET /service/rest/v1/status` 的 `Server` 为 **`Nexus/3.96.4-01 (COMMUNITY)`**，并通过部署镜像标签交叉核实。现有 `pypi-hosted` 为 `ALLOW_ONCE`；`npm-hosted` 为 **`ALLOW`**，不能把重发保护结论套用于它。独立 `poc-pypi-07f1b0b9` / `poc-npm-07f1b0b9` 均确认 `ALLOW_ONCE`，冲突写入仅针对自建合成包。用户明确批准增加并保留普通账号的两仓库专用角色；已保留原角色并核实绑定，未更改仓库策略。正式上线仍需实际部署环境的传输、权限、安全与回归验收。
- **变更控制**：先做实验与记录；如果需要改变 `requirements.md` / `architecture.md` 已确认的语义，提交选项、影响范围及 ADR 候选，**得到用户确认后**才能修改。
- **阶段边界**：PoC 不实现正式 Exporter/Importer、Web UI、数据库、Worker；普通框架兼容、Bundle 安全、幂等与故障恢复分别在 Phase 1/3/5 用自动化测试验收。

## 2. 已验证事实（保留必要证据）

| ID | 状态 | 环境和真实结果 | 证据 / 限制 |
| --- | --- | --- | --- |
| NX-B PyPI Hosted | **PASS** | Nexus **3.96.4 CE（外部测试实例）**；管理员与普通用户经 Twine 7.0.0 发布合成 Wheel，Simple API 和文件下载均 HTTP 200，哈希一致。普通账号最初 403，补仓库级角色后成功。 | 普通用户 Wheel SHA-256 `043e4a2ef78b8d23c91c86d53c7859bdf2358349ccbd1685fa8a3caa9801270f`；脚本 `nexus_pypi_poc_v2.py`。仅验证 Wheel，sdist 待测。 |
| NX-C npm Hosted | **PASS** | Nexus **3.96.4 CE（外部测试实例）**；管理员/普通用户对预先生成的 `.tgz` 执行 `npm publish --ignore-scripts`，Registry 元数据和下载均 HTTP 200，原始字节 SHA-256 一致，测试脚本标记未发现。普通账号最初 403，调整 npm 仓库角色后成功。 | 普通用户 tarball SHA-256 `510ba0e17d45c1941d7d77ba09d21beb6f05e131cda6ab55e78f882b416afab2`；脚本 `nexus_npm_poc.py`。**具体最终有效权限清单、Node/npm 版本未记录**；无标记只代表本样本。 |
| NX-D1 PyPI 重复上传 | **PASS** | Nexus **3.96.4 CE（外部）**，`Disable redeploy`；相同 Wheel 分别由管理员与普通用户重发，两次均 HTTP **409**，前/后读取 HTTP 200 且 SHA-256 未变化。 | SHA-256 同 NX-B；脚本 `nexus_pypi_redeploy_poc.py`。**409 必须读回比较**才可判 `SKIPPED`/`CONFLICT`。尚未测试不同字节及 npm 重发。 |
| PY-02.A uv 版本选择 | **FAIL（朴素方案）/ 风险已复现** | 本地 Linux、Python 3.13.5、uv 0.10.0、虚拟目标 Python 3.12、合成 Simple Index。`1.0.0` 仅 Wheel，`2.0.0` 仅 sdist（有 PEP 658 静态元数据）：`uv pip compile --no-build` exit 0 却选 **1.0.0**；普通 compile 在受控样本选 **2.0.0**。 | `uv_sdist_resolution_poc.py`。普通 compile 不能直接用于不可信第三方包：可能触发构建代码。 |
| PY-02.B 静态元数据预检 | **PASS（仅预检）** | 同一 Linux/Python/uv 环境。B1 PEP 658/714 sidecar、B2 sdist `PKG-INFO`：均识别新版 `2.0.0`、哈希核实并读取静态依赖；B3 `Dynamic: Requires-Dist`：识别新版但标记依赖未知。三例 `uv --no-build` 仍选旧版 `1.0.0`。 | `uv_sdist_metadata_poc.py`、`uv_sdist_metadata_poc_result.json`；结果 `PASS_STATIC_PREFLIGHT_ONLY`。**未验证 uv 安全完整传递求解，也未证明 OS 级隔离。** |
| LAB-NPM 生命周期 | **发现风险** | Linux Node 22.16.0/npm 10.9.2：对合成目录执行 `npm pack --ignore-scripts`，观察到 `prepare` 标记执行；对已有 `.tgz` 的 `npm publish --ignore-scripts --dry-run --offline` 未观察到标记执行。 | 原始测试见历史版；禁止以 `npm pack --ignore-scripts` 作为不可信目录无代码执行保证。 |
| PY-02.C 原生静态注入 | **FAIL（两版本已执行）** | Windows x64 Python 3.12.12 / uv **0.11.29**，Linux x64 Python 3.13.5 / uv **0.12.5**。合成 `root 2.0.0 sdist → helper 1.0.0 Wheel → leaf 1.0.0 Wheel`，另有旧 root Wheel。两环境 compile 无注入/有版本精确的 `dependency-metadata` 注入均 exit 0 选 **1.0.0**；精确要求 `2.0.0` exit 1。lock 配置正确加载，但实际 root 仍为 **1.0.0**。 | [py02c.json](evidence/py02c.json)、[py02c-linux.json](evidence/py02c-linux.json)、`tests/poc/poc_py02c*.py`。动态依赖明确 `PARTIAL`；sidecar 损坏/缺失、身份不匹配均拒绝。未放宽构建限制，未取得新版闭包；不等于所有安全替代求解器均不可行。 |
| PY-01 四目标 | **PASS（合成解析范围）** | Windows 宿主 Python 3.12.12，uv 0.11.29、packaging **26.3**；CPython **3.9/3.12 × Windows x64 / manylinux glibc 2.31 x64** 独立求解。OS/Python markers、extra 正确，每目标 5 节点；pylock 的 ABI Wheel 集与目标 tags 集一致，接受 cp39 abi3，拒绝 glibc 2.34，选出每目标一个最优 Wheel。 | [py01.json](evidence/py01.json)、`tests/poc/poc_py01.py`。这是 Ubuntu 20.04 等效标签的虚拟求解，**不是四个原生解释器安装或真实编译扩展的 ABI 验收**；pylock 保留多个兼容 Wheel，Exporter 仍须独立筛选。 |
| NPM-01 合成矩阵 | **PASS（限定样本）** | Windows，Node **26.4.0** / npm **11.17.0**；lock v2/v3 × win32/linux x64 glibc 四组合。锁中保留两平台 optional；安装按平台选取；scoped、自动 peer、生产+开发与 `--omit=dev` 均正确，peer 负例非零退出。tarball integrity + SHA-256 核实；install/prepare 标记未执行。 | [npm01.json](evidence/npm01.json)、`tests/poc/poc_npm01.py`。只用合成 Registry 和原始 `.tgz`，未调用 npm pack；Linux 组合使用平台 override，不冒充原生 Linux 执行。 |
| NPM-01 真实制品 | **PASS（收集）/ PARTIAL（运行验证）** | 同一工具版本，固定本轮选定的 **esbuild 0.28.2、sharp 0.35.5**；v2/v3 × 两平台独立生成锁。Windows/Linux 必需的 esbuild、sharp/libvips 节点均在锁中；两目标制品并集 **13** 份，全部下载并校验 npm integrity 与 SHA-256。静态记录 esbuild 生命周期脚本风险。 | [npm-real.json](evidence/npm-real.json)、`tests/poc/poc_npm_real.py`。真实包脚本/原生代码未运行；sharp/libvips 对 Ubuntu 20.04 的运行时兼容仍未验证。Git/直接 URL/file/workspace 不声称完整覆盖。 |
| NX-A 3.96.4 身份 | **PASS（本实例）** | 正确普通凭据登录 **204** 且设 Session Cookie；错误凭据、合成禁用账号均 **403**。普通用户 `rapture_Security.getUser` 返回身份，与输入账号精确匹配；匿名/错误凭据不绑定该身份。Cookie RPC 需要成对的 `NX-ANTI-CSRF-TOKEN` Cookie/Header；Basic-auth RPC 也核实同一身份。 | [nexus.json](evidence/nexus.json)、`tests/poc/poc_nexus*.py`。缺少 CSRF 时 Session RPC 为 401，补齐后 200。内部接口稳定性、会话租约/账号运行中失效仍不视为通过；临时 active/disabled 测试账号已删除。 |
| NX-B sdist | **PASS（发布/读回）** | Twine **7.0.0**；管理员在独立仓库、普通用户在已有授权的 PyPI Hosted 发布数据型合成 sdist，Simple 与下载均 200、SHA-256 一致。普通用户对新独立仓库的发布为 403，符合实际仓库权限。 | [nexus.json](evidence/nexus.json)：`sdist_readback` / `sdist_existing_ordinary`。普通用户 sdist SHA-256 `4fa5baf7a47f0a61d4f883a667ed2b296b8a90e5c832872cd70bd2dd878ac486`。**没有构建/安装 sdist**。 |
| NX-D2 异内容/重复 | **PASS（管理员及授权后普通用户）** | 两个独立 `ALLOW_ONCE` 仓库：普通用户经 Twine/npm CLI 首次发布 exit 0、读回哈希一致；PyPI 同字节/异字节均服务端 **409**，npm 合成发布 PUT 同字节/异字节均 **409**；每次读回原哈希不变。比对内容后分别判 `SKIPPED` / `CONFLICT`。管理员矩阵在前轮通过。 | [nexus-authorized.json](evidence/nexus-authorized.json)、[nexus.json](evidence/nexus.json)。后者普通用户 403 为授权前历史，未篡改。npm CLI 同版本预检错误不当作服务端 409；专用角色按用户要求保留。 |
| PY-02.B-ADR 独立静态求解 | **PASS（最小方案 B）** | Python **3.12.12** / packaging **26.3**；静态 Provider + 有界独立回溯。sidecar 和 PKG-INFO 均选 **root 2.0.0 sdist → helper → leaf**，记录原始 sdist 身份/哈希；真实约束冲突回溯、无解识别、四虚拟目标 markers/extras、后到 extras、循环、Requires-Python、yanked 通过。动态依赖/Requires-Python、缺失/损坏 sidecar、身份错误均 **PARTIAL**，未读取旧版候选。 | [py02b-solver.json](evidence/py02b-solver.json)、`tests/poc/poc_py02b_solver.py`。18 项断言通过；无 build/install 命令。仅合成回环索引、通用 Wheel、1000 状态搜索上限；不证明生产性能、所有预发布/镜像/同版多制品策略，未构建/安装 sdist。 |
| E2E-01 两生态 | **PASS（最小隔离闭环）** | Linux 容器 Python **3.12.15** / pip **25.0.1**，Node **26.4.0** / npm **11.17.0**；独立 Docker **internal=true** 网络只连 Nexus 与临时客户端，公网 IP 连接探针失败。普通用户发布的合成 Wheel / npm 包均以空缓存、只配置 Hosted 安装成功，安装后文件内容一致。 | [e2e.json](evidence/e2e.json)、`tests/poc/poc_e2e.py`。npm 标准 `registry.npmjs.org` lock URL 重定向到 Nexus 并成功；自定义外部 URL 仍尝试外网且失败，须显式报告。临时网络/目录已清理。单包最小样本，不覆盖真实原生包/完整项目/生产部署。 |

**本轮证据优先级**：上方新增行与 `docs/evidence/` 是 2026-10-11 本轮实测；旧 NX-B/C/D1、PY-02.A/B 和 LAB-NPM 行保留为历史基线，其“待测/未记录”不覆盖本轮新增结论。认证已从其他版本历史观察推进到 **3.96.4 实例的身份、错误密码与禁用账号验证**，但内部 `/service/rapture/session`、ExtDirect 均不是稳定公开 API；仍需锁版本并回归。

**普通用户实际权限**：原有 PyPI `pypi-hosted` 的 **browse/read/add**、npm `npm-hosted` 的 **browse/read/edit** 保留。用户本轮明确授权增加并保留 `poc-validation-07f1b0b9`：仅含独立 PyPI 测试仓库 **browse/read/add**、独立 npm 测试仓库 **browse/read/edit**，共六项，无嵌套角色；绑定和范围已读回核实。这是本实例有效权限，**不是普遍适用的最小权限证明**。现有 npm 仓库 `ALLOW` 的风险须在正式部署前处理；本轮未更改策略。

**架构已得到支持的部分**：PyPI/npm 分开的 `NexusPublisher`、仓库级授权、远端 SHA-256 读回、内容相同跳过、冲突不覆盖、结果未知先核实。Nexus 的 `409` 不单独代表内容相同。**用户已批准继续 B 的最小 PoC；生产适配器设计细化仍在 Phase 1，未改写需求/架构基线。**

## 3. 剩余事项与执行顺序

### 3.1 2026-10-12：v2 增量实测

| ID | 状态 | 实测与通过条件 | 证据 / 局限 / 下一动作 |
| --- | --- | --- | --- |
| V2-PY-PIP | **PASS（四虚拟目标）** | Windows Python 3.12.12、pip 25.3、uv 0.11.29；CPython 3.9/3.12 × Windows x64 / glibc 2.31 x64。对固定版本执行 pip 目标下载，四组合各取一个 Wheel，均与目标 packaging 排序一致，字节/SHA-256 与原始合成文件一致；Linux 显式传兼容的平台集合。 | [v2-python.json](evidence/v2-python.json)、`poc_v2_python.py`。不运行 install/build；目标是虚拟标签，不是四原生系统 ABI 验收。分发仍需明确内置 pip。 |
| V2-PY-LOCK | **PARTIAL（契约缺字段）** | 本轮 pylock 节点字段为 `marker,name,version,wheels`，**没有 dependencies 边**。版本/节点/制品可读取，不能直接把选中节点集合当作完整依赖图。 | 同上。正式 Adapter 需从原始可靠 Core Metadata 补边，保留 extras/markers 与来源；正式 Manifest 图提取尚未实现。 |
| V2-PY-BRIDGE | **PASS（最小安全桥接实验）** | 只读合成 sdist 静态 PKG-INFO，以临时 Wheel 作为 uv 元数据载体：新版 root 2.0.0 → helper → leaf，四虚拟目标的 OS markers/extras 正确；约束冲突回溯、无解识别通过。Dynamic 及身份不匹配禁止生成载体。原生 sdist-only `--no-build` 对照仍失败。 | 同上。载体仅用于求解，**不安装/导出/发布**，不代表真实 Wheel。映射回原始制品及 Bundle 防载体污染尚待工程验证；不是生产 Provider 或 OS 沙箱，不能替代已批准 B 决策。 |
| V2-NPM-TREE | **PASS（合成 v2/v3）** | Node 26.4.0/npm 11.17.0；使用随 npm 安装的 Arborist 9.8.0 `loadVirtual`。同包不同版本保留不同路径；共享 prod/dev 节点未误判为纯 dev；peer、optional 保留；生产预测与 Windows `npm ci --omit=dev` 安装路径一致。 | [v2-npm.json](evidence/v2-npm.json)、`poc_v2_npm.py` / `v2_npm_reader.cjs`。未验证原生 Linux、Git/workspace 等特殊源，不能只靠 `dev` 标志取代全部平台/可达性处理。 |
| V2-NPM-CACHE | **PASS（原生缓存及原始字节）** | pacote 21.5.1 以 tarball URL + integrity 获取原始字节，冷获取与 offline 热缓存均与合成 tarball SHA-256 一致；破坏其真实内容寻址缓存后，offline 读取拒绝，返回 ENOTCACHED。 | 同上。仅使用 Registry tarball 路径，不调用 pack。**未实现生产 Artifact Store**，其索引/并发/原子写/损坏复用仍需工程测试；不把 npm 缓存当 Bundle 权威原始库。 |
| V2-NX-AUTH | **PASS（登录/登出/运行中禁用）** | 同一 Nexus 3.96.4 CE，普通用户 login 204、身份 RPC 核实；logout 204 后同一 Cookie jar 不再绑定该身份；错误 Basic 凭据 401。另创建仅绑定已有专用测试角色的临时 active 账号，登录后由管理员禁用；原 Basic 身份 RPC 401、原 Cookie RPC 200 但不再绑定该身份；临时账号删除 204。 | [v2-nexus.json](evidence/v2-nexus.json)、`poc_v2_nexus.py`。本地回环→SSH 加密隧道→远端回环，不在裸远端 HTTP 传密码。现有账号/角色/仓库策略不变；不证明自然租约过期或角色权限运行中撤回，HTTP 200 必须核对返回身份。 |
| V2-NX-UNKNOWN | **PASS（服务端响应丢失）** | 普通用户两生态分别一次上传，代理收到 Nexus 200 后故意断开客户端响应；客户端 RemoteDisconnected。无重传，远端读回 SHA-256 相同，分类 VERIFIED_UPLOADED。 | 同上。复用已授权 ALLOW_ONCE 专用仓库，仅增加唯一合成包；未改用户/角色/策略。测试原始 HTTP 请求语义，不证明 Twine/npm 自动重试或生产 Worker 恢复。 |
| V2-E2E-REAL | **PASS（真实依赖集合隔离安装）** | requests 2.32.5 + certifi 2026.7.22 / charset-normalizer 3.5.2 / idna 3.20 / urllib3 2.8.0，共 5 个 Wheel；debug 4.4.3 / ms 2.1.3 + dev is-number 7.0.0，共 3 个原始 tarball。普通用户发布/读回哈希相同；Docker internal 网络、空缓存、公网 IP 探针失败，仅 Nexus 安装成功。npm 原 public Registry lock URL 切到 Nexus，production 正确排除 dev；安装文件与原始制品内容哈希相同。 | [v2-e2e.json](evidence/v2-e2e.json)、`poc_v2_e2e.py` 及两客户端。Linux 容器 Python 3.12.15/pip 25.0.1、Node 26.4.0/npm 11.17.0。不 import/运行第三方代码，不证明真实扩展/Ubuntu 20.04 运行兼容，也未生成正式 Bundle。临时网络/目录已清理，真实样本留在专用测试仓库。 |

**路线结论（按最新用户指示更新）**：用户已明确“继续验证 uv 桥接，尽量不重复造轮子”。后续优先验证“可靠静态元数据适配 + 临时载体 + uv 求解 + 原始制品映射”，B 最小独立求解器保留历史证据，不扩大其实现。验证授权不等于生产架构验收；见 [ADR-PY-UV-BRIDGE](adr/ADR-PY-UV-BRIDGE.md)。需求与正式架构基线未修改。

**仍待完成**：①原始 Core Metadata 依赖图提取/与目标求解结果关联；②Artifact Store 与 uv 原生缓存的复用/损坏/并发工程验证；③真实 Windows/Ubuntu 20.04 的 Python 扩展与 sharp/esbuild 原生运行（不放宽不可信脚本限制）；④会话自然租约过期、角色撤权及生产任务遇鉴权失效后的暂停/重授权；⑤正式 Bundle/API/Worker/DB/分发/备份/清理/HTTPS 的 Phase 1/3/5 测试。未测项保持 PENDING，本轮不宣称全部 v2 或上线验收通过。

### 3.2 uv 桥接扩展（2026-10-12）

| 验证项 | 实测结论 / 边界 |
| --- | --- |
| uv 求解复用 | 四虚拟目标、传递 sdist、菱形冲突回溯/无解、后到 extras、依赖环、Requires-Python、yanked 默认/精确固定、预发布默认/精确固定及 80 节点链通过；全程只有 uv 求解，没有自研回溯。 |
| 原始元数据图 | 从已核实原始 Core Metadata 对 uv 固定节点补边，传播被请求 extras；核对约束/可达性。pylock 仍无完整依赖边，但限定样本的图可追溯至原始 metadata SHA-256。 |
| 载体隔离与原始交付 | 原始 Wheel 优先/无目标 Wheel 回到原 sdist；收集见证 ZIP 的原始字节/SHA-256 正确。实际注入载体、投影锁哈希/外部 URL 篡改、求解后原文件变化均拒绝。见证 ZIP 不是正式 Bundle v1。 |
| 静态性/一致性/归档负例 | Dynamic Requires-Dist/Requires-Python/Provides-Extra、legacy metadata、同版不同元数据、重复字段、无效依赖/直接 URL、未指定目标 marker、制品/sidecar 哈希及内容错误、文件身份、PKG-INFO symlink/重复/超大声明均拒绝。未知新版不删除后静默选旧 Wheel。 |
| 真实正例 | `urllib3[socks]==2.8.0` 原始静态 sdist + PySocks 1.7.1 原始 Wheel，取得有限目录完整闭包和依赖边，收集不含载体。**不构建/安装 sdist，离线安装状态 NOT_VERIFIED**。 |
| 真实负例 | requests 2.32.5 sdist 的 Requires-Dist、Requires-Python、Provides-Extra 为 Dynamic，明确返回 `PARTIAL_REJECTED_METADATA_OR_MAPPING`、不启动 uv；负例拒绝 PASS 不代表 requests sdist 闭包 PASS，也不覆盖已有 Wheel 安装的 PASS。 |

Windows **35/35**：[uv-bridge-extended.json](evidence/uv-bridge-extended.json)，Python 3.12.12 / uv 0.11.29；Linux **33/33** 合成重放：[uv-bridge-linux.json](evidence/uv-bridge-linux.json)，Python 3.14.6 / uv 0.12.5；均用 packaging 26.3。数目包含预期拒绝负例，不代表 68 个真实项目成功；四环境仍是虚拟目标。脚本 `poc_uv_bridge.py` / `uv_bridge.py` / `poc_uv_bridge_remote.py`；CLI 输出/锁节点/依赖边/原始文件和元数据哈希/来源 URL/负例均记录。Linux 包装器用一次性 venv，仅以 binary-only 安装 packaging 26.3；实验目录与 venv 清理，不接触 Nexus。

**复用与局限**：标准库只读归档/email，packaging 解析规范，uv 执行求解；新增代码是投影/映射/对已定解的图核实。输入仍是明确有限目录，保守全目录预检可能拒绝无关未知候选，同版平台元数据差异也保守拒绝；生产 Provider、精确目标 patch/release、完整 Bundle、复杂真实项目性能仍待完成。原生 C 失败与 B 历史证据保留，未将源码收集成功外推为可安装。

### 3.3 原基线阶段准入（2026-10-11，保留）

本轮保留 PY-02.C 失败对照，按用户决策完成方案 B 最小 PoC 与普通用户冲突矩阵。Phase 0 准入条件在限定代表样本内满足；后续进入 Phase 1 的 Shared Core/生产适配器设计，不把实验求解器视为正式 Resolver。

| 次序 | PoC | 验证问题 / 通过条件 | 失败时处理 | 当前 |
| --- | --- | --- | --- | --- |
| **1 / P0** | **PY-02.C / B-ADR** | 不运行第三方构建，较新 sdist 的可靠静态元数据经等效安全求解路径取得传递闭包；动态元数据明确 `PARTIAL`。 | 元数据未知保留候选身份/已核实制品并标 PARTIAL，不因缺 Wheel 暗中回退；真实约束冲突可以在已知候选间回溯。 | **原生 C FAIL；用户选择的 B 最小静态闭包 PASS** |
| **2 / P0** | **PY-01** | 对 CPython **3.9/3.12 × Windows x64/Ubuntu 20.04 x64** 验证 markers、extras、ABI、manylinux（Ubuntu 20.04 glibc 2.31）、独立求解及每环境兼容 Wheel 选择；结果可追溯且无隐性漏包。 | 调整适配器或限定高级环境支持；报告缺口。 | **PASS：四虚拟目标合成解析/标签；原生安装未外推** |
| **3 / P0** | **NPM-01** | 固定 npm/Node 版本，验证 lockfile v2/v3、Windows/Linux `optionalDependencies`（如 `esbuild`/`sharp`）、peer、dev/prod、scoped 包及公网 `resolved` URL；不遗漏应收集的 tarball，外部下载/脚本风险显式告警。 | 逐目标平台重新解析或报告部分覆盖；不能假称完整。 | **PASS：合成矩阵/真实制品收集；PARTIAL：原生运行/特殊源** |
| **4 / P0** | **E2E-01** | 两生态各用一个小型代表案例：从已验证的制品集合导入 **Nexus 3.96.4 CE 测试仓库**，客户端禁用公网源、从该 Hosted 仓库完成安装；记录可复现结果和联网尝试。无需正式 Bundle/UI/Worker。 | 定位额外资源、lockfile URL、Python tag、Registry 配置等阻碍。 | **PASS：两生态单包隔离安装；自定义公网 lock URL 负例明确** |
| 5 / P1 | NX-A(3.96.4)、NX-D2 | 普通账号正确/错误/匿名身份验证；独立测试仓库的两生态同字节/异字节重发与远端读回。 | 403/409/超时均核实，不误报 SKIPPED；内部登录接口稳定性单列风险。 | **NX-A PASS（本实例）；NX-D2 PASS（授权后普通用户矩阵补齐）** |
| 后续工程测试 | BND-SEC / IMP-REC / DIST / OPS | ZIP 安全、文件级幂等/UNKNOWN、重启授权恢复、CLI/API 契约、Windows/Linux 分发、SQLite/备份/30 天清理、HTTPS。 | 在相应 Phase 1/3/5 形成集成测试与上线门槛。 | PENDING（非当前 PoC） |

## 4. PY-02.C 实测结论与 ADR 决策（B 最小 PoC 已批准并通过）

**结论**：Windows uv **0.11.29** 与 Linux uv **0.12.5** 的原生静态注入都不能让 `--no-build` 下的 sdist-only 候选参加本轮求解。`[pip].dependency-metadata` 和 `[tool.uv].dependency-metadata` 都按正确配置测试；`uv lock` 的 manifest 中出现新版元数据**不代表实际 package 节点选中了新版**。本轮检查实际节点，均为旧版。精确固定新版明确失败，未获得 `2.0.0 → helper → leaf` 闭包。

**资料与实测分开**：uv 官方 [dependency-metadata](https://docs.astral.sh/uv/reference/settings/#dependency-metadata) 允许提供依赖静态元数据；[pip no-build](https://docs.astral.sh/uv/reference/settings/#no-build_1) 文档注明其为 `--only-binary :all:` 别名。实测表明在这两个版本中，元数据注入没有改变候选过滤。这个结果仅约束本轮原生路径，不能据此断言其他版本或其他安全求解器也失败。

**原生路径安全/完整性**：只运行合成回环 Index，无构建/安装命令，不存在可用构建后端；不声称 OS 沙箱证明。静态 sidecar/PKG-INFO 仍可保留最高候选、原始 sdist 和直接依赖；Dynamic、缺失/错误哈希、身份不匹配不当成空依赖。原生实验分别记录收集完成、`PARTIAL_UNRESOLVED_SDIST_CLOSURE` 和安装未验证。其负约束命令 exit 1 先被“新版无可用 Wheel”阻断，不作为传递依赖冲突求解正确的证据；独立 B 的冲突实测见下文。

**ADR-PY-02.C 候选**：已确认的版本优先/禁执行构建规则继续有效，尚未修改 `requirements.md` / `architecture.md`。

| 选项 | 可行性与代价 | 对承诺的影响 |
| --- | --- | --- |
| A. Registry 静态候选预检 + 受控适配层交给 uv | uv 仍处理版本/markers/extras；需探索安全的候选桥接，验证传递回溯与来源追踪。不能把临时元数据载体当真实可安装 Wheel 或上传 Nexus；维护成本中高，**PENDING**。 | 若成功可保留静态 sdist 闭包目标；未验证前不宣称可用。 |
| B. 静态元数据 Provider + 无代码执行的独立依赖求解器 | **用户选择并批准继续，最小 PoC PASS**。静态读取与求解分离；packaging 处理约束/markers/extras/版本；有界独立回溯处理闭包与冲突；元数据未知停止。生产实现/测试成本仍高。 | 维持静态 sdist 完整闭包目标；Phase 1 细化 `PY-03` 生产适配器、目标 tags/多制品一致性与性能。当前脚本不替代正式 Resolver。 |
| C. MVP 对 sdist-only 保留最高候选与制品，显式 PARTIAL | 最小改动：候选预检作为 uv 二进制求解后的强制校验，发现被过滤新版不采用旧版的“完整”结果；明确 `UNRESOLVED_SDIST_CLOSURE` / `DYNAMIC_REQUIRES_DIST`，不执行构建。 | 保留版本优先与尽力而为；**暂不承诺完整静态 sdist 传递闭包**。是否接受该阶段限制需用户确认，不能自行宣布 Phase 0 退出。 |

**本轮决策与结果**：用户明确要求“按 B 继续”，批准 B 的验证路线；不采用 C 的缩减承诺，未放宽安全规则。静态 sidecar/PKG-INFO 闭包均通过，原生 uv 注入失败保留为对照。18 项断言覆盖闭包、冲突回溯/无解、四目标 markers/extras、后到 extras、循环、Requires-Python、yanked 与六种未知/损坏/身份错误；见 [py02b-solver.json](evidence/py02b-solver.json)。

**后续设计边界**：PoC 使用最多 1000 状态的穷举回溯，仅服务合成索引、通用 Wheel，不是可直接发布的生产求解器。正式 `PythonResolver` 需整合 PY-01 目标制品筛选，明确预发布、同版本多个制品、镜像/元数据一致性策略，并评估生产算法。解析/收集/发布/安装状态继续独立：静态样本解析 COMPLETE、选中制品收集有哈希，未测试本链发布或 sdist 安装；未知元数据解析 PARTIAL。复现命令及输入/输出见 [evidence/README.md](evidence/README.md)。

## 5. 外部 Agent 工作协议与完成标准

1. 开始前同时读取 `requirements.md`、`architecture.md`、本文；**以本文第 2 节实测为基线，不能把历史失败当作最新状态**。只对 Nexus **3.96.4 CE** 做后续测试；不要接触旧版本，未经授权不修改仓库角色/部署策略。
2. `PY-02.C` 原生注入已失败，方案 B 最小 PoC 历史 PASS 保留；**最新用户指示优先继续 uv 桥接验证、尽量复用，不扩大 B 独立求解器实现**。不把重复运行原生路径当作解决。PoC 使用合成包/临时目录；真实 Registry 制品只解析、下载与静态检查，严禁执行不可信安装/构建脚本。
3. 每项结果按固定结构记录：`ID / 目标 / 操作环境与工具版本 / 输入样本 / 完整可复现命令 / 预期与通过标准 / 实测关键输出 / SHA-256或其他证据 / PASS|PARTIAL|FAIL|PENDING / 局限 / 下一动作 / 架构影响`。区分实际测得与官方资料/推断；账号、密码、Cookie、Token、内部完整地址等不得写入报告。
4. 发现架构基线冲突：形成 ADR 候选（问题、2～3 个选项、风险与代价、推荐、需用户批准项），**停止扩大实现**；不得直接修改 `requirements.md` / `architecture.md`。未执行的项目必须留 `PENDING`。
5. 每完成一个 PoC，更新本文第 2、3 节状态及对应结论；详细运行日志/JSON 存 `tests/poc/` 或 `docs/evidence/`，不要再把多轮过程逐字追加到主文档。
6. **Phase 0 退出判断**：PY-02 安全版本策略明确；PY-01 四目标代表样本通过或限制已获批准；NPM-01 无静默漏包或缺口可解释；E2E 至少有两生态最小离线闭环证据，或有明确允许延后的理由。若任一核心项仍不可解释，保持 `IN PROGRESS`。生产发布前另行完成 3.96.4 实际部署回归与安全/恢复验收。

## 6. 资料及现有脚本索引

现有脚本（均属 PoC；不等于正式应用代码）：

- `scripts/uv_sdist_resolution_poc.py` — PY-02.A，历史版本降级风险。
- `scripts/uv_sdist_metadata_poc.py` + 其 result JSON — PY-02.B，静态预检基线。
- `scripts/nexus_{pypi_poc_v2,npm_poc,pypi_redeploy_poc,auth_probe}.py` — 历史发布/认证脚本；新实测采用下列专用脚本。
- `tests/poc/poc_py02c.py` — 原生静态注入对照、负例、锁节点核实。
- `tests/poc/poc_py02c_remote.py` — 同一合成样本的 Linux/uv 0.12.5 重放与临时目录清理。
- `tests/poc/poc_py02b_solver.py` — B 的静态 Provider、有界独立回溯及负例。
- `tests/poc/poc_nexus_authorized.py` — 保留授权及普通用户冲突矩阵。
- `tests/poc/poc_py01.py` — 四目标 markers/extras/ABI/manylinux 与 pylock。
- `tests/poc/poc_npm01.py`、`poc_npm_real.py` — 合成矩阵、真实 esbuild/sharp 制品收集。
- `tests/poc/poc_nexus.py`、`poc_nexus_followup.py` — 3.96.4 身份/实际权限/sdist/独立仓库冲突读回。
- `tests/poc/poc_e2e.py`、`e2e_python_client.py`、`e2e_npm_client.js` — 真实公网隔离安装与 lock URL 负例。
- [evidence/README.md](evidence/README.md) — 每项固定结构记录、工具准备、完整复现命令、SHA-256 与残留资源。

参考标准与官方资料（PoC 中应复核实际适用版本）：

- uv：<https://docs.astral.sh/uv/concepts/resolution/>；<https://docs.astral.sh/uv/reference/settings/>；<https://docs.astral.sh/uv/reference/cli/>
- Python 元数据：<https://peps.python.org/pep-0643/>；<https://peps.python.org/pep-0658/>；<https://peps.python.org/pep-0714/>
- Nexus：<https://help.sonatype.com/en/pypi-cli-usage.html>；<https://help.sonatype.com/en/publishing-npm-packages.html>；<https://help.sonatype.com/en/privileges.html>；<https://help.sonatype.com/en/configurable-repository-fields.html>
- npm：<https://docs.npmjs.com/cli/v10/commands/npm-publish/>；<https://docs.npmjs.com/cli/v10/using-npm/scripts/>

**最终结论（当前）**：原基线 Phase 0 在限定 PoC 范围 PASS，可进入 Phase 1。v2 工具复用增量证据保留；按最新用户指示，uv 桥接已扩展到复杂合成依赖、原始元数据图补齐、原制品交付/载体拒绝和真实静态/动态样本。限定验证通过，后续优先走 uv 适配路线；仍缺生产 Registry Provider、正式 Bundle 契约、原生运行及恢复/分发/运维验收，不外推为上线通过。
