# 米洛在家

一个可以来访、读近况、看作品、留信与托付事务的个人小家应用。前端采用暖纸手绘与现代排版骨架，后端保存真实记录并按显式配置管理作息、公开内容与可选的独立执行入口。

## 主要功能

- **公开参观（无需登录）**
  - **首页与作息告示**：展示手绘窗景、北京时间作息安排、最新公开作品与最近公开近况。
  - **公开近况与历史（`/#/notes`）**：纯文本草稿、修订、明确发布与撤下；首页展示最近 3 条，支持按发布序号翻阅历史与阅读全文。
  - **公开作品展示（`/#/works`）**：作品介绍、显示版本、明确访问链接、任意类型附件下载与可选 PNG 封面；支持草稿暂存与完整快照发布。
- **邀请制私密交流（需登录）**
  - **书信（`/#/letters`）与传纸条（`/#/chat`）**：邀请激活账号、会话分页、幂等发信、敲门回执与真实处理状态展示；私信默认仅参与双方可见，不会自动转为公开内容。
  - **交办工作桌（`/#/work`）**：需显式授权；支持任务提交、状态筛选、补话、叫停、文字回执、附件下载与本人只读维护摘要。
- **本机管理与可选独立执行**
  - 公开内容、作息规则、账号邀请、在线备份与恢复演练均通过本机 CLI `milo-home` 显式操作，不暴露网页后台编辑器或任意文件上传。
  - 读信与交办执行默认**关闭**；启用前需在受控 Linux 环境中显式配置目录、账号权限、预算政策与外部模型凭据，普通网页读写不会触发模型调用。

## 环境要求

- **后端**：Python `3.12–3.13` 与 [uv](https://docs.astral.sh/uv/)（依赖锁定在 `backend/uv.lock`）
- **前端**：Node.js `>= 24.21.0` 与 `pnpm 10.33.4`（依赖锁定在 `frontend/pnpm-lock.yaml`）
- **高级部署（可选）**：Linux + systemd + Nginx（仅在部署生产 HTTPS 或受限自动执行入口时需要）

## 快速开始（本地隔离开发）

在任意普通用户工作目录即可拉起完整的本地回环前后端，无需 root 或外部模型凭据：

### 1. 安装依赖并核对配置

```bash
uv sync --project backend --locked
uv run --project backend --frozen milo-home --config examples/deployment.toml config-check
```

`examples/deployment.toml` 仅用于本机隔离开发：允许来源 `http://127.0.0.1:7473`、使用本地 HTTP Cookie，不启用任何模型、账号或定时服务。未显式指定数据库路径时，默认写入 `$XDG_STATE_HOME/milo-home/home.sqlite3`（或 `~/.local/state/milo-home/home.sqlite3`）。

### 2. 初始化数据库、生成邀请并启动 API

```bash
uv run --project backend --frozen milo-home --config examples/deployment.toml init-db
milo_private_dir="$(mktemp -d)"
uv run --project backend --frozen milo-home --config examples/deployment.toml \
  invite --handle visitor --out "$milo_private_dir/invitation.json"
uv run --project backend --frozen milo-home --config examples/deployment.toml serve --port 7484
```

新建的临时私密目录为 `0700`，邀请文件以 `0600` 权限写入且不向终端打印秘密。激活账号时请从该文件中读取一次性邀请码，不将邀请码放入网址；已有文件不会覆盖。

### 3. 启动前端并访问

在另一个终端启动 Vite 开发服务器（默认监听 `http://127.0.0.1:7473`，并将 `/api` 代理至 `127.0.0.1:7484`）：

```bash
cd frontend
pnpm install --frozen-lockfile
pnpm dev
```

打开 `http://127.0.0.1:7473`：

- 未登录可直接浏览首页、作息、公开作品与公开近况（新库默认空态，不自动导入演示内容）。
- 进入「书信」或「传纸条」，选择「有邀请」并用上述邀请文件激活账号即可登录体验。

### 4. 可选：体验合成预览库与本机发布入口

- **生成带合成往来的独立预览库**（不改已有库、不调用模型）：

```bash
uv run --project backend --frozen milo-home dev-fixture --scenario dual-entry
```

命令会在临时私有目录生成独立的 `home.sqlite3` 与 `login.json`，将 `--database`（或 `MILO_DATABASE`）指向该库即可登录查看多封书信与纸条样例；也可选 `replied` / `pending` / `failed` / `cancelled` / `timed_out` 场景。

- **配置本机作息与发布合成近况 / 作品**（输入模板见 `examples/`）：

```bash
uv run --project backend --frozen milo-home --config examples/deployment.toml schedule configure --file examples/schedule.json
uv run --project backend --frozen milo-home content note --help
uv run --project backend --frozen milo-home content work --help
```

## 配置说明

应用使用显式 TOML 配置文件（通过全局 `--config FILE` 或环境变量 `MILO_CONFIG` 选择），同时保留 `MILO_*` 环境变量覆盖；程序**不会**自动搜索当前目录配置，也**不会**自动加载 `.env`。

- 同一字段解析优先级：**显式命令行参数 > 受支持环境变量 > 选定 TOML > 通用非秘密默认**。
- 配置文件只保存非秘密设置与凭据路径引用，严禁将密码、邀请码、会话 Cookie 或 API 密钥写入 TOML 或仓库。
- 完整字段表、环境变量映射、路径保护边界与脱敏核对（`config-check`）见 [deploy/configuration.md](deploy/configuration.md) 与 [examples/deployment.toml](examples/deployment.toml)。

## 开发与验证

### 后端检查与接口契约

```bash
uv run --project backend --frozen ruff check backend/src backend/tests
uv run --project backend --frozen ruff format --check backend/src backend/tests
uv run --project backend --frozen pytest backend/tests -q
uv run --project backend --frozen python backend/tests/http_smoke.py
```

修改后端 HTTP 接口后，重新导出 OpenAPI 并同步前端类型：

```bash
uv run --project backend --frozen milo-home export-openapi --out contracts/openapi.json
cd frontend && pnpm api:generate
```

### 前端检查与构建

```bash
cd frontend
pnpm api:check
pnpm format:check
pnpm typecheck
pnpm build
```

更多前端开发代理、合成预览页、验证脚本及自托管字体细节见 [frontend/README.md](frontend/README.md)。

### 轻量结构检查与受控执行

```bash
python3 scripts/check-structure.py
```

说明：后端既有历史模块间仍留有 5 组已知依赖环，当前运行该检查器会返回非零退出码并列出这些债务（前端与 `scripts/` 源码均为 0 问题）。

在资源受限的宿主机上，可通过 `python3 scripts/guard-command.py <command...>` 在受限 CPU/内存配额与整机负载阈值下运行测试和构建。

## 部署、备份与文档索引

- **配置与通用部署**：[部署配置说明](deploy/configuration.md)、[网页部署说明与模板](deploy/README.md)、[资料维护说明](deploy/maintenance.md)、[备份与恢复说明](deploy/backup-recovery.md)
- **部署与素材工具**：[生成器用法](scripts/README.md)、[systemd配置示例](examples/site-systemd.toml)；先生成审查，不自动安装或启用服务。
- **基础体检与在线备份**（使用同一显式 `--config` 或 `MILO_DATABASE`）：

```bash
uv run --project backend --frozen milo-home --config /path/to/site.toml doctor
uv run --project backend --frozen milo-home --config /path/to/site.toml backup --out /path/to/private/backup.sqlite3
uv run --project backend --frozen milo-home backup-rehearse \
  --file /path/to/private/backup.sqlite3 \
  --out /path/to/private/rehearsal.sqlite3
```

- **接口规范（OpenAPI / JSON Schema）**：见 [contracts/](contracts/)（含 `openapi.json`、`reply-choice-result.schema.json`、`work-decision-result.schema.json`）。

## 支持范围与已知边界

- 面向单实例个人站点设计，不提供多租户、网页后台编辑器、公开注册、富文本/CMS、站内搜索或应用 iframe 托管。
- 基础网页与 API 可由普通用户运行；独立自动回信、受限工作执行器与 systemd 定时服务仅面向 Linux/systemd 运维场景，默认均不启用。
- 浏览器自动化验证覆盖 Chromium 桌面（1440px）与窄屏（390px / 320px）及 `prefers-reduced-motion`；Safari / Firefox 与 iOS 真机输入法细节仍以实际使用核对为准。

## 许可与第三方素材

- **源码许可**：MIT License，见 [LICENSE](LICENSE)。
- **手写旁白字体子集**：`frontend/public/fonts/long-cang-subset.woff2` 提取自 Google Fonts 的 Long Cang（版权归 The Long Cang Project Authors），遵循 **SIL Open Font License 1.1**，许可全文见 [frontend/public/fonts/LongCang-OFL.txt](frontend/public/fonts/LongCang-OFL.txt)，来源摘要与重生成方法见 [frontend/scripts/subset-hand-font.md](frontend/scripts/subset-hand-font.md)。
