# 前端开发与验证说明

`frontend/` 是「米洛在家」的 Vue 3 + TypeScript + Vite 单页应用，采用「手绘基调 + 现代骨架」暖纸双色油墨视觉语言。

## 环境与依赖

- Node.js `>= 24.21.0`
- pnpm `10.33.4`（锁定在 `frontend/pnpm-lock.yaml`）

安装依赖：

```bash
cd frontend
pnpm install --frozen-lockfile
```

## 日常命令

```bash
cd frontend
pnpm dev           # 启动回环开发服务器（http://127.0.0.1:7473）
pnpm format:check  # 检查 Prettier 格式
pnpm typecheck     # 运行 vue-tsc --noEmit
pnpm api:check     # 核对 src/api/schema.d.ts 与 ../contracts/openapi.json 一致
pnpm api:generate  # 根据已导出的 ../contracts/openapi.json 重新生成类型
pnpm build         # 构建生产静态产物到 frontend/dist/
pnpm preview       # 仅用于检查静态产物（不包含 /api 代理）
```

### API 代理与隔离后端

- `pnpm dev` 默认将 `/api` 代理到 `http://127.0.0.1:7484`（与后端 `milo-home serve` 默认端口一致）。
- 如需指向其他本机隔离测试端口，可传入 `MILO_DEV_API_TARGET`（也可写入被忽略的 `frontend/.env.local`，不会暴露到浏览器）：

```bash
MILO_DEV_API_TARGET=http://127.0.0.1:7485 pnpm dev
```

- 前端接口全部使用相对路径（`${BASE_URL}api/...`），不写死域名或宿主机目录；生产环境由 Nginx 等反向代理将静态文件与 `/api` 同源托管。

## 页面与路由边界

- **公开只读路由（无需登录）**
  - `/#/`：首页（窗景、作息告示、公开作品一角、最近公开近况）
  - `/#/works`、`/#/works/:id`：公开作品目录与详情（含站外安全链接与附件下载）
  - `/#/notes`、`/#/notes/:id`：公开近况历史与纯文本正文
- **邀请制私密路由（需登录 Cookie；`/work` 需额外授权）**
  - `/#/letters`、`/#/letters/:id`：书信主题列表与单封续写
  - `/#/chat`：传纸条聊天
  - `/#/work`、`/#/work/:id`：交办工作桌（含本人只读维护摘要）
- **仅开发环境可用的纯前端样板预览（生产构建自动排除）**
  - `/#/preview/home`：首页多状态与油墨主题预览（使用 `src/data/homePreviewFixture.ts` 合成样本）
  - `/#/preview/work-detail`：交办详情样板预览（使用 `src/data/workDetailPreviewFixture.ts` 合成样本）

前端数据约定：

- 不将私密记录、草稿或公开近况/作品写入 `localStorage` / `sessionStorage`，仅在会话内存保留必要的返回位置与已读页。
- 公开近况、公开作品与维护摘要均按需读取，不持续轮询。
- 所有用户/作者正文均作为纯文本渲染，禁止 `v-html`。

## 合成样例与验证脚本

- 仓库根目录 `examples/` 提供明确标记为合成的接口样例（`public-notes.json`、`public-works.json`、`maintenance-status.json`、`noncompulsory-interaction.json`、`automatic-reply-status.json`、`work-progress.json`），用于在不依赖真实站点或私人数据的情况下核对前端状态。
- `frontend/scripts/` 包含若干面向本地回环隔离环境的自动化脚本：
  - `check-api.mjs`：核对生成类型与 `contracts/openapi.json` 一致（`pnpm api:check`）。
  - `work-read-job.test.mjs`：工作桌任务读取状态机单测（`node scripts/work-read-job.test.mjs`）。
  - `verify-*.mjs`：配合隔离后端与 Playwright Chromium 进行真实回环页面核验（默认使用合成数据与 `127.0.0.1` 回环地址，不读写生产服务）。
  - `hand-glyphs.mjs` 与 `subset-hand-font.md`：手写旁白字体子集的字表提取与重生成说明。

## 字体与素材许可

- 界面正文使用系统无衬线字体，运行时**不请求任何外部 CDN 或外部字体服务**。
- 手写旁白与彩蛋使用自托管的 Long Cang 子集 `public/fonts/long-cang-subset.woff2`（字体族名 `Milo Hand`），版权归 The Long Cang Project Authors，遵循 **SIL Open Font License 1.1**，许可全文随字体保存在 [public/fonts/LongCang-OFL.txt](public/fonts/LongCang-OFL.txt)，来源与重生成命令见 [scripts/subset-hand-font.md](scripts/subset-hand-font.md)。
- 窗景插画、纸张纹理、印章及界面组件均为本项目原创，遵循根目录 [LICENSE](../LICENSE)（MIT）。
