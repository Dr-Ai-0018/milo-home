# 部署配置

应用使用显式TOML配置，示例见[deployment.toml](../examples/deployment.toml)。保留原`MILO_*`环境变量，两个入口使用相同的字段转换及校验，不执行`.env`，不按当前工作目录搜索配置。

## 选择与核对

全局`--config FILE`选择本次命令的文件，优先于`MILO_CONFIG`。相对配置文件名在入口处解析为绝对位置；文件内路径必须是绝对路径，不展开`~`或`${VARIABLE}`。不指定文件时只读取受支持环境变量和通用默认，不创建文件或启用服务。

```sh
uv run --project backend --frozen milo-home --config examples/deployment.toml config-check
uv run --project backend --frozen milo-home --config examples/deployment.toml init-db
uv run --project backend --frozen milo-home --config examples/deployment.toml serve --port 7484
```

示例允许本机前端来源`http://127.0.0.1:7473`，使用非Secure开发Cookie；HTTPS部署必须换成自己的精确来源并启用Secure。基础默认数据库位于`$XDG_STATE_HOME/milo-home/home.sqlite3`；未设置XDG时为当前用户的`~/.local/state/milo-home/home.sqlite3`。空或相对的XDG值在需要默认数据库时拒绝。加载配置不建库，`init-db`或HTTP生命周期才初始化。

直接使用API factory时，通过`MILO_CONFIG=/absolute/site.toml`选择文件；`serve`将已解析设置交给应用实例，避免启动时回到另一份默认配置。维护monitor也接受`--config`或`MILO_CONFIG`，把同一个绝对文件选择传给CLI子进程；不在父进程另硬编码运行账号或备份目录。

同一字段优先级为：**显式操作参数 > 受支持环境变量 > 选定TOML > 通用默认**。命令行解析器自带默认不算显式覆盖。明确提供的空值、坏类型和未知TOML字段报错，不能被默认掩盖；每个显式来源都校验，因此环境覆盖不能掩盖文件中无效的值。

`config-check`返回字段名及`cli`、`environment`、`toml`或`default`来源，不输出实际值、配置路径、凭据或原文。它检查应用设置，不打开业务库、执行profile或凭据，不证明部署目录和运行绑定可用。全局`--database PATH`、重复`--origin ORIGIN`、`--database-user NAME`和`--run-directory PATH`可覆盖对应字段；其他操作参数仍按各命令`--help`执行。

`--help`不读取配置。`export-openapi`不读取现场设置；合成`dev-fixture`及不记录现场结果的离线恢复只读取相关路径保护声明，不校验无关的业务／认证环境变量或读取执行凭据。显式选择的TOML仍须能解析，缺失或损坏不能悄悄绕过保护。需要原库关联的恢复记录步骤照常校验所选现场配置。

## 字段

表中“未配置”不会自动激活对应能力。整数必须是正整数，TOML布尔值使用`true/false`，环境布尔值保持原`0/1`。来源列表在TOML中为非空字符串数组，在环境中以逗号分隔。

| TOML字段 | 环境变量 | 类型／默认 |
| --- | --- | --- |
| `app.database` | `MILO_DATABASE` | 绝对文件路径；用户状态目录规则 |
| `app.origins` | `MILO_ORIGINS` | 精确HTTP(S)来源列表；`["https://localhost"]` |
| `app.secure_cookie` | `MILO_COOKIE_SECURE` | 布尔；`true`，关闭仅限明确本机HTTP来源 |
| `app.session_seconds` | `MILO_SESSION_SECONDS` | 正整数；604800 |
| `app.auth_attempts` | `MILO_AUTH_ATTEMPTS` | 正整数；10 |
| `app.auth_window_seconds` | `MILO_AUTH_WINDOW_SECONDS` | 正整数；300 |
| `app.body_limit` | `MILO_BODY_LIMIT` | 正整数字节；16384 |
| `work_attachments.root` | `MILO_WORK_ATTACHMENT_ROOT` | 绝对目录；未配置，须与预算配齐 |
| `work_attachments.max_bytes` | `MILO_WORK_ATTACHMENT_MAX_BYTES` | 正整数；未配置 |
| `public_assets.root` | `MILO_PUBLIC_ASSET_ROOT` | 绝对目录；未配置，须与预算配齐 |
| `public_assets.max_bytes` | `MILO_PUBLIC_ASSET_MAX_BYTES` | 正整数；未配置 |
| `maintenance.file` | `MILO_MAINTENANCE_FILE` | 绝对文件路径；未配置，须与owner配齐 |
| `maintenance.owner` | `MILO_MAINTENANCE_OWNER` | 实际账号UUID；未配置 |
| `maintenance.backup_directory` | `MILO_BACKUP_DIRECTORY` | 绝对目录；未配置 |
| `maintenance.development_directory` | `MILO_DEVELOPMENT_DIRECTORY` | 绝对目录；未配置 |
| `maintenance.success_file` | `MILO_BACKUP_SUCCESS_FILE` | 绝对文件路径；未配置 |
| `maintenance.daily_unit` | `MILO_BACKUP_UNIT` | 只读观察的systemd service/timer名；未配置 |
| `execution.database_user` | `MILO_DATABASE_USER` | 数据库服务账号；未配置 |
| `execution.run_directory` | `MILO_WORK_DIRECTORY` | 已有私密运行目录；未配置 |
| `execution.workspace` | `MILO_WORKSPACE` | 已有工作区绝对目录；未配置 |
| `execution.reply_directory` | `MILO_REPLY_DIRECTORY` | 已有独立回信目录；未配置 |
| `execution.profile` | `MILO_CODEX_PROFILE` | 私密输入profile的绝对路径；未配置 |
| `execution.credential_file` | `MILO_CREDENTIAL_FILE` | 私密凭据来源绝对路径；未配置 |
| `execution.cli` | `MILO_CODEX_CLI` | 新主会话初始化用CLI绝对路径；未配置 |
| `execution.tool_host` | `MILO_CODEX_TOOL_HOST` | 新主会话初始化用配套宿主绝对路径；未配置 |
| `paths.code_root` | `MILO_CODE_ROOT` | 可选代码安装根，须包含实际安装包；未配置时仍保护实际包目录 |
| `paths.static_root` | `MILO_STATIC_ROOT` | Web服务器实际静态目录；仅API部署可省略 |
| `paths.protected_roots` | `MILO_PROTECTED_ROOTS` | 个人等禁止写入的绝对根列表；未配置为空，环境用冒号分隔 |

运行账号和运行目录仅作为确实使用它们的管理操作的默认，不给`init-db`、API或只读导出强加root执行要求。私密执行与运行资料观察要求明确选择数据库，不能猜用户状态目录。维护`collect/run`与配套`capture`允许从配置补齐工作区，缺失则在打开业务资料前拒绝；维护`retention-preview`沿用原语法，不要求额外工作区。显式`--apply`、`--execute`等确认仍必须提供。每日备份未配置时显示未知；观察路径与unit分开配置，单独一份服务退出码不证明远端快照可恢复。

模型、审批、既有安装位置与主对话身份仍沿原profile及绑定；配置文件不代替业务登记。`execution.cli/tool_host`只补齐明确的新主会话初始化参数，不覆盖已有journal中的安装事实。profile必须明确选择，文件名可自定义，来源文件与既有运行配置字节／摘要不因改路径重写；未配置时不猜某个用户的Codex目录。快速开始与模板用法见[根说明](../README.md)与[部署说明](README.md)。

## 路径保护与迁移

数据库、私密输出、运行目录、封存附件和公开资产使用同一保护规则，避开代码、实际静态目录和明确声明的私人根；目录还不能包住这些保护根。目标解析后再核对别名，原有属主、0700/0600、普通文件和运行目录祖先权限检查继续有效。不要把静态目录写成源码附近猜出来的位置，尤其是wheel非editable安装。

保护根使用规范绝对名称，部署者在有权限的预检中核对符号链接和目录类型。HTTP服务不需要遍历私人保护树；当保护根因权限或ProtectHome无法访问时，保留其禁止路径声明，访问目标自身的权限检查仍然执行。代码包位置来自安装事实，不随运行目录变化。仅配置API数据根，不会替Web服务器修改静态目录；二者必须在部署模板中明确对齐。

从旧部署迁移时，把原个人保护范围显式写入`paths.protected_roots`，把原目录和profile位置原样写入对应字段，再核对原库身份／主对话／profile字节与摘要；不要拿通用默认覆盖旧站。Linux受限旧执行器内部的rootfs文件布局、兼容profile别名和历史近况固定ID属于兼容协议，保持原样。

## 文件与秘密

应用配置仅保存非秘密设置和凭据来源引用，不接受内嵌token或密码字段。文件须为普通文件、最多128KiB，属主为当前用户或root，group/other不可写；可以由root持有并给服务账号只读。读取应用配置不沿用执行凭据“当前euid且0600”的权限规则。实际profile、凭据和执行目录仍执行各自的私密权限检查。

新安装不会导入站点自己的真实内容、创建真实运行绑定或开启timer。对已有部署切换前，应显式保留全部旧值并核对原数据库、主对话及配置摘要；不要仅靠通用默认去猜现有资料位置。
