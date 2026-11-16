# 每日一语素材库

此目录集中保存名言、背景图、人物图与来源记录，运行时不依赖外部图片服务。

- `quotes.json`：42 条不同名言，含作者、作品、来源链接、原文及对应图片路径。外文的中文为项目自译或节译，不冒用已有译本署名。
- `backgrounds/`：六张自然景观背景。本地文件，来源与 Unsplash 许可见 `image-sources.json`。
- `portraits/`：历史人物画像或照片；缺少合适肖像的条目使用自然背景。人物图准确来源与署名许可见 `credits.json`、`portrait-licenses.json`。不将人物姓名的字形图当作肖像。
- `credits.json`：人物图的署名、许可与来源链接。完整署名另外保存在 `public/licenses/IMAGE-CREDITS.txt`，网页中不显示图片来源入口。人物图保留原图文件，灰度、遮罩与叠色仅在页面通过 CSS 实现。
- `image-sources.json`：下载来源记录；`portrait-licenses.json`：Wikimedia Commons 返回的完整许可元数据。
- `名言总览.md`：可直接阅读的全部 42 条名言，包含英文原文、中文释义及图片文件名。
- `fonts/original/`：从用户 `D:/字体/字体/字体` 目录复制的 6 个字体原文件；`fonts/font-sources.json` 保存原始字体名称、版权、许可字段。
- 网页使用 `public/fonts/quote-*.woff2`，它们是覆盖名言库全部所需字形的压缩字体子集。`kai`、`xingkai`、`li` 为中文楷体、行楷、隶书；`serif`、`hand`、`slab` 为英文衬线、手写、板衬线。原字体版权信息保留在字体内及 `public/licenses/FONT-CREDITS.txt`。

英文名言（存在 `original` 字段）在第二页以英文原文逐字显示，中文保存在“中文释义”折叠区域。每条名言的 `font` 字段固定其字体，刷新不会随机换字体。编辑或增加名言后，请确认网页字体子集包含新增的字形；原始完整字体均在本目录留存。

## 日期与不重复规则

`src/lib/quoteSchedule.ts` 对全部名言使用固定种子的 Fisher–Yates 洗牌，然后按北京时间的自然日依次取一条。同一天刷新、重新进入或换设备都得到同一句；北京零点后换句，不依赖设备时区，也不受后端是否在线影响。

任意连续 42 天内不重复，覆盖目前默认的 21 天打卡活动。第 43 天开始循环。如果活动周期延长到 42 天以上，请先补充足够的名言；名言库的条目顺序或数量更新会改变后续的每日安排，建议在一轮活动结束后再调整。

## 更换素材

1. 修改 `quotes.json` 的 `text`、`author`、`source` 和 `sourceUrl`，保留唯一的 `id`。
2. 图片放进本目录对应文件夹，填写相对路径。没有合适人物图时将 `portrait` 设为 `null`。
3. 增加人物图时同步填写 `credits.json` 的署名和许可。外文名言保留 `original`，便于核对出处。
4. 运行 `npm run check:journey` 与 `npm run build` 检查文件引用及构建。

校徽、院徽、标题字体单独存放在 `public/brand/`；来源见该目录的 `SOURCES.md`。
