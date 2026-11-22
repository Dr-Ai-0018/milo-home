# 部署与恢复

基础开发不需要root或systemd，见[根说明](../README.md)。本页是Linux/systemd、Nginx与HTTPS的单站部署；使用自己的域名、目录、账号和凭据。源码中的service和Nginx文件是带`@@FIELD@@`的模板，必须先生成，不能原样安装。

## 准备安装与配置

需要Python 3.12–3.13、uv、Node/pnpm，以及支持cgroup v2、AllowedCPUs和LoadCredential的systemd；Nginx和证书由部署者管理。安装前核对本机版本、可用CPU、端口和备份能力，不覆盖已有站点。

1. 把源码放进自己的安装根，例如`/opt/milo-home`，由管理员持有、服务账号只读。按锁文件安装非editable生产依赖：`uv sync --project /opt/milo-home/backend --frozen --no-dev --no-editable`。解释器和安装文件须在API的`ProtectHome=true`视图内可读；不要用指向私人home的解释器链接。
2. 在源码树构建前端，把`frontend/dist`发布到独立静态目录；该目录只放网页产物。保留旧入口和指纹资源用于回退，不把源码、数据库、附件根或配置文件放进Web根。
3. 复制并编辑[systemd配置示例](../examples/site-systemd.toml)，放到自己的绝对路径，例如`/etc/milo-home/site.toml`。根目录、域名与账号只是示例；字段和优先级见[配置表](configuration.md)。应用TOML不含密钥，root持有、服务组可读，文件0640、父目录0750；profile和实际密钥继续分别私密持有。
4. 创建无登录权限的专用服务账号及实际数据／存储目录，数据目录归该账号、0700。所有配置根使用规范绝对路径；代码、静态根和私人保护根与数据分开。新安装用该账号、该配置运行`init-db`；已有站点先做备份与兼容核对，不拿空库替换。分别以管理员及服务账号执行`config-check`，再核目录和文件权限。

安装根使用实际版本路径也可以；代码根须包含真正安装的包，不能以site-packages旁边猜静态目录。脚本在目标机器生成CPU集合，支持单CPU或受限cpuset；默认取当前可用集合的一半，至少一个。

## 生成并审查

从源码根运行，以下路径均需换成自己的实际值：

```sh
uv run --project backend --frozen python scripts/render-deployment.py \
  --config /etc/milo-home/site.toml \
  --python /opt/milo-home/backend/.venv/bin/python \
  --working-directory /opt/milo-home/backend \
  --domain home.example.test \
  --acme-root /srv/www/milo-acme \
  --certificate /etc/letsencrypt/live/home.example.test/fullchain.pem \
  --certificate-key /etc/letsencrypt/live/home.example.test/privkey.pem \
  --out /tmp/milo-deployment-review
```

不传`--out`只向标准输出给预览，不创建目录。传入时目标须不存在，输出0700目录及0600文件，包含配置／产物摘要清单；不安装、reload、启用服务、建账号、读密钥或调用模型。只按明确选中的TOML生成，不继承终端中`MILO_DATABASE`等覆盖；域名须与配置的唯一精确HTTPS来源一致。路径支持空格，拒绝变量、specifier和控制语法；可用`--cpus 2,4`选择当前允许集合的子集。生成只做声明及语法预检，目标文件、真实权限、符号链接和安装包位置仍需在目标机核对。

基础输出仅API unit、HTTP引导与HTTPS两个Nginx配置。检查全部替换值和`manifest.json`，运行`systemd-analyze verify /tmp/milo-deployment-review/milo-home-api.service`。生成器保留半核配额、内存／进程／期限保护；解释器使用绝对路径，不让systemd猜可执行文件。

## 启用基础网页

确认具体路径后手动安装选定unit及本站Nginx配置，先备份原件。执行`systemctl daemon-reload`，启动API（生成模板默认回环端口为`7474`，与本地开发示例的`7484`区分）并核对仅回环监听及`/api/health`。API以专用账号运行，无执行密钥，只允许回环网络，私有写入仅开放明确数据／存储根。

先用HTTP引导配置准备ACME挑战目录，此时`/api/`返回503；用自己的证书管理器申请证书。再审查并切到HTTPS配置，执行`nginx -t`后reload。不要同时装两份相同域名的server。检查HTTP跳转、TLS、health、匿名内容、私密接口401、登录的精确Origin校验及桌面／手机浏览器；邀请通过私密文件交付，不放进URL。HTTP只信任回环反代，Nginx覆盖转发头，不透传来访者自报来源。

确认后才按自己的开机策略启用API。证书续期单独管理；续期hook先做Nginx语法检查再reload，首次发证不代表已验证自然续期。

## 可选自动执行与维护

普通收件、近况与作品读取不需要执行器。需要自动处理时，先按 `milo-home work setup --help`、`milo-home automatic-service --help` 及各命令 `--help` 完成显式权限、工作区、共同政策、作息、主绑定和原生策略登记。既有实例沿原journal安装／thread／模型政策，不因生成模板新建主对话。

在同一生成命令增加`--execution --owner 实际账号UUID --session 已登记业务主会话UUID`，并补齐TOML中的执行和维护字段，才生成三组service/timer。回信明确接续同一原生home；模型从既有策略取得，模板不写死型号。凭据由systemd `LoadCredential`私密交付，维护service无模型凭据。生成器不会读取profile、密钥或原生home，也不验证绑定就绪。

安装前运行只读准备检查，确认服务账号数据库访问、原绑定、预算和作息。timer仅负责检查已保存资料，不能代替授权或跳过占用恢复。三个timer分别为交办每分钟、回信每15秒、维护每30分钟；全都默认不安装、不启用。最后按选定范围逐一手动启用，记录原状态与停止入口。停用某一能力只停对应timer，保留资料；忙碌／未知运行按原恢复命令核对，不杀任务或清账本。

维护配置及四层证据见[维护说明](maintenance.md)，备份和恢复见[恢复说明](backup-recovery.md)。未配置每日备份来源保持未知；本模板不创建备份仓库或自动保留规则。

## 更新与回退

保存应用版本、原配置／unit、静态入口、timer原状态和最新备份；在独立副本验证升级，再安排窗口停止新领、等原任务收尾。数据schema升级后不能直接用旧代码或旧库覆盖新写入。失败时只按预先核对的兼容范围恢复代码／静态入口／配置和本次暂停入口；数据恢复需要单独选择准确备份与唯一执行身份。

公开源码不携带原部署记录、备份凭据或本地协作白板。部署者自行把配置、运行事实、原件与恢复索引保存在Web根和源码之外；本机证据不能由模板默认值代填。
