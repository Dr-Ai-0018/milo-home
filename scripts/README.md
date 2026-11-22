# 开发与生成工具

从源码根运行。部署文件先用[部署生成器](../deploy/README.md)预览；脚本不安装服务或开启自动执行。[结构检查](check-structure.py)只做静态检查，不能代替实际运行。

## 独立源码副本

`python3 scripts/export-source.py`只检查审定的`public-source-files.json`；加`--out /absolute/new-directory`会按清单复制源码，不带Git对象、旧分支、本地协作资料或运行文件。目标须不存在且位于原源码树之外，输出摘要可用来核对候选。不创建提交、远程或上传；清单变动须随对应代码审查更新。

## 公开作品材料

`prepare-public-work.mjs`为明确选定的公开网页截图，并把自己提供的单页HTML编译成PDF；需要已有Playwright模块与Chromium，不自动下载。网址不能带用户名、密码、query或fragment。仅使用新浏览器上下文，不带登录资料；页面出现写请求、Cookie或登录状态时拒绝采集。输出目录必须不存在，失败保留现场供检查，不覆盖旧材料。

```sh
node scripts/prepare-public-work.mjs \
  --playwright /absolute/node_modules/playwright/index.mjs \
  --url http://127.0.0.1:7473/ \
  --guide /absolute/synthetic-guide.html \
  --out /absolute/new-material-directory \
  --synthetic
```

示例针对自己启动的隔离网页。`--synthetic`明确标记合成材料；真实作品由作者选定可公开页面和原稿后省略该选项。生成`cover.png`、`visitor-guide.pdf`和含来源／实际时间／长度／SHA-256的`sources.json`。PDF须一页，截图有尺寸预算；输出不会自动导入、上传或发布作品。确认权利和公开范围后，沿`content work --help`显式创建、导入和发布。

## 有限验证

本机开发命令统一经`python3 scripts/guard-command.py COMMAND ...`：当前允许CPU的一半、半核配额、768MiB与50秒期限，宿主繁忙时拒绝或停止自己的验证。不要因为中断就降低保护或重跑全套。

`verify_public_notes_ui.py`、`verify_public_works_ui.py`、`verify_maintenance_ui.py`用于明确选择的合成隔离后端与生产包组合验证，参数见各脚本`--help`。不会替部署者证明真实备份或现站已发布；禁止把测试库接入生产执行。
