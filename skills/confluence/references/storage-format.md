# Confluence 页面源码（storage format）与 REST API

Confluence Cloud 页面的源码是 XHTML 片段（storage format），外加 `ac:`（宏、链接）和 `ri:`（资源引用）两个命名空间。`confluence.py get-storage` 拿到的就是它。

## 解析

- 不是完整的 XML 文档：外面要包一层根元素并声明命名空间才能解析：
  `<root xmlns:ac="urn:ac" xmlns:ri="urn:ri" xmlns:at="urn:at">…</root>`。
- 编辑器会写入 HTML 命名实体（`&nbsp;`、`&ldquo;`、`&rdquo;` …），XML 解析器不认识，要先换成数字实体（`&#160;`）。CDATA 里的内容不能替换。
- 代码块的内容在 CDATA 里；代码里出现 `]]>` 要拆成 `]]]]><![CDATA[>`。

## 常见元素

| 内容 | 源码 |
|------|------|
| 标题、段落、列表、表格 | `h1`–`h6`、`p`、`ul/ol/li`、`table/tbody/tr/th/td`，单元格里通常还包一层 `<p>` |
| 行内格式 | `strong`、`em`、`code`、`br`，`span`（颜色等样式） |
| 代码块 | `<ac:structured-macro ac:name="code"><ac:parameter ac:name="language">bash</ac:parameter><ac:plain-text-body><![CDATA[…]]></ac:plain-text-body></ac:structured-macro>`；没有语言时用 `none` |
| 信息框 | `ac:name="info"`（还有 `note`、`warning`、`tip`），正文在 `<ac:rich-text-body>` 里 |
| 带背景色的面板 | `ac:name="panel"`，参数 `bgColor` 等 |
| 页面链接 | `<ac:link><ri:page ri:content-title="标题" /><ac:link-body>文字</ac:link-body></ac:link>`；跨空间时 `ri:page` 还带 `ri:space-key`；锚点在 `ac:link` 的 `ac:anchor` 属性上 |
| 普通链接 | `<a href="…">` |
| 用户提及 | `<ac:link><ri:user ri:account-id="…" /></ac:link>` |
| 图片、附件 | `<ac:image><ri:attachment ri:filename="…" /></ac:image>`，`view-file` 宏 |
| 页面布局（分栏） | `<ac:layout><ac:layout-section><ac:layout-cell>…` |
| 评论高亮 | `<ac:inline-comment-marker ac:ref="…">文字</ac:inline-comment-marker>`：是评论锚点，不是正文内容 |
| Forge 插件的宏 | `<ac:adf-extension><ac:adf-node type="extension">…</ac:adf-node><ac:adf-fallback>…同样一份…</ac:adf-fallback></ac:adf-extension>`，参数在 `ac:adf-parameter` 里，见 `plantuml-app.md` |

## 编辑器会加的东西

用户在浏览器里编辑过的页面，源码里会多出这些，解析时忽略即可，用 `get-storage` 改源码时原样保留：

- `local-id` / `ac:local-id` 属性（几乎每个块都有）
- 表格的 `data-table-width`、`data-layout`、`<colgroup><col style="width: …"/></colgroup>`
- 宏的 `ac:schema-version`、`ac:macro-id`
- 链接的 `data-card-appearance`

## 判断能否用 Markdown 往返

`confluence.py` 的 Markdown 转换只覆盖：标题、段落、列表（含嵌套）、表格（不合并单元格、每格一段）、代码块、引用、info/note/warning/tip 信息框、页面链接、普通链接、粗体、斜体、行内代码、换行、PlantUML 插件图。其余内容一律报错并指出位置，绝不静默丢弃；遇到时改用 `get-storage` / `put-storage`。

## REST API（v2）

- 认证：HTTP Basic，`邮箱:API令牌` 做 base64。401/403 通常是邮箱和令牌不属于同一个账号，或者令牌过期或被撤销。
- 读页面：`GET /wiki/api/v2/pages/<id>?body-format=storage`；读历史版本加 `&version=<n>`；版本列表：`GET /wiki/api/v2/pages/<id>/versions`（最新的在前）。
- 改页面：`PUT /wiki/api/v2/pages/<id>`，body 里带 `version.number = 当前版本 + 1`；`version.message` 会显示在页面历史里。版本号不对会返回 409，这就是并发修改保护。
- 新建：`POST /wiki/api/v2/pages`（`spaceId`、`parentId`、`title`、`body.representation = storage`）。删除：`DELETE /wiki/api/v2/pages/<id>`，页面会进回收站。
- 同空间列页面：`GET /wiki/api/v2/pages?space-id=<id>&body-format=storage&limit=100`。按标题找页面：再加 `&title=…`。
- 当前用户：`GET /wiki/rest/api/user/current`（v1）。
- 短链 `/wiki/x/<code>`：`<code>` 是页面 id 按小端序编码后的 base64url（没有填充）。

## 坑

- **网络会断**：读请求可以重试。POST 和 PUT 不要盲目重试，先重新读一次页面，确认上一次请求是否已经生效（例如页面已经建好，或者版本号已经加 1）。
- **页面可能被改名**：不要靠标题定位页面，用 id。
- **页面可能被人手动改过**：推送前比较版本号，不一致就停下来重新拉取，不要覆盖。
- **内容约束**：页面是给人读的，不放工具或 AI 的说明框，不引用代码托管平台上的内容，架构图用 PlantUML 插件画。
