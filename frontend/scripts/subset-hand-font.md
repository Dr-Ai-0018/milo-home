# 手写体子集

页面的手写旁白与彩蛋使用 Long Cang 的子集 `public/fonts/long-cang-subset.woff2`，字体族名为 `Milo Hand`。

- 来源：Google Fonts 仓库 `ofl/longcang/LongCang-Regular.ttf`，版权归 The Long Cang Project Authors。
- 固定源码提交：`20da2e7644bac5325abbc9f30848ae3162bfc974`；下载后先核对下方SHA-256，拒绝来源变化。
- 许可：SIL Open Font License 1.1，全文随字体放在 `public/fonts/LongCang-OFL.txt`。
- 覆盖范围：可打印 ASCII 加上 `src/theme/handCopy.ts` 与 `src/theme/inks.ts` 中的全部非 ASCII 字符（注释行除外）。原字体没有 `←`，这个符号回落到正文字体。
- 来访者输入、错误、标签和数据从不使用手写体；子集缺字时回落到正文字体，不影响阅读。

## 重新生成

修改上述两个文件里的固定文案后执行。源字体只临时放在项目外，生成后删除，不进入项目依赖。

```bash
workdir="$(mktemp -d)"
curl -fsSL -o "$workdir/LongCang-Regular.ttf" \
  https://raw.githubusercontent.com/google/fonts/20da2e7644bac5325abbc9f30848ae3162bfc974/ofl/longcang/LongCang-Regular.ttf
printf '%s  %s\n' \
  e5bf2c3f24ef2327c6f136d8f73e2f9dfdf44896fdbeb35a9515f44777bb91bc \
  "$workdir/LongCang-Regular.ttf" | sha256sum --check --status || exit 1
node scripts/hand-glyphs.mjs > "$workdir/glyphs.txt"
uvx --from 'fonttools[woff]' pyftsubset "$workdir/LongCang-Regular.ttf" \
  --text-file="$workdir/glyphs.txt" --flavor=woff2 --layout-features='*' \
  --output-file=public/fonts/long-cang-subset.woff2
rm -- "$workdir/LongCang-Regular.ttf" "$workdir/glyphs.txt"
rmdir -- "$workdir"
```

命令在 `frontend/` 下运行。当前源字体 SHA-256 为 `e5bf2c3f24ef2327c6f136d8f73e2f9dfdf44896fdbeb35a9515f44777bb91bc`，子集约 54 KB、当前文案共 212 个不重复字符。
