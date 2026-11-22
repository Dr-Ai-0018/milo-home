# 有限维护与恢复入口

所有命令沿明确的`--config`／`MILO_CONFIG`，字段见[配置表](configuration.md)；示例路径须换成自己的值。

## 摘要与容量

`maintenance.file`及`owner`成对配置。摘要放在独立root持有目录，服务组只读，例如目录0750、摘要0640；不能放到API账号可替换的目录。相邻`backup-facts.json`记录备份事实、`retention-cursor.json`记录扫描位置，均应纳入部署者备份，不放进原生home改变既有清单。

```sh
milo-home --config /etc/milo-home/site.toml maintenance show
milo-home --config /etc/milo-home/site.toml maintenance collect
milo-home --config /etc/milo-home/site.toml maintenance retention-preview
```

collect／preview沿明确配置的服务账号、原运行目录、工作区和回信目录；仅读本项目有界元数据与已有证据，不封存、不读模型凭据或扫描全机。额外开发目录只有明确配置才统计。附件和公开资产各须配根与预算；未配置或扫描未完成不冒充零占用成功。每日unit和成功标记未配置时保持未知。

## 有限自动维护

`maintenance run --apply`在原资格、共享锁、身份与空间检查下最多封存两流，热16MiB触发；不删除未知或未发布证据，忙碌与异常不推进游标。监视器启动前宿主达到70%暂缓，运行中达到80%或40秒停止自身子进程；systemd另设50秒期限。

用[部署生成器](README.md)生成实际CPU集合、半核／768MiB及每30分钟timer，模板本身不启用服务；先独立采集并核权限，再给API接同一摘要，最后手动启用。停用仅停维护timer，不误停交办或回信。产生冷段后保留兼容读取版本，不删段回退。

## 完整采集与演练

政策JSON须显式给预算与include／exclude／native_exclude；缺失不猜默认。路径变量由操作者明确赋值，目标为新的私密目录：

```sh
milo-home --config /etc/milo-home/site.toml maintenance backup-check \
  --policy "$backup_policy" --out "$new_archive"
milo-home --config /etc/milo-home/site.toml maintenance capture \
  --policy "$backup_policy" --out "$new_archive" --writers-stopped
milo-home --config /etc/milo-home/site.toml maintenance rehearse \
  --archive "$new_archive" --sha256 "$trusted_digest" \
  --policy "$backup_policy" --out "$new_restore"
```

预检先解决未分类文件、预算和余盘。capture保存原入口状态，短维护窗口暂停新领、忙则暂缓、停止写入采集，随后只恢复本次原先运行的入口；上传／取回在窗口外做。`--writers-stopped`只适用于明确安排的窗口，不在每日timer中添加。

rehearse不接执行器。可选`--record`仅原库身份与owner仍匹配时登记文件恢复，不代填远端事实。中断时沿本次持久状态恢复入口：`maintenance restore-entries --state-file "$saved_entry_state" --confirm`；不会重做采集、清占用或启动模型。来源身份变化、孤段、未知进程或容量到限时停止对应操作并保留资料，不删映射或重建thread绕过。
