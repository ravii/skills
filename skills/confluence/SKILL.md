---
name: confluence
description: 读取、编辑、新建或删除 Confluence Cloud 页面（含 PlantUML 架构图）。用户给出页面链接并要求查看、修改、整理、画图或同步页面内容时使用；没有明确页面链接时不使用，而是先向用户要链接。
---

# Confluence 页面编辑

## 适用场景

- 用户给出一个 Confluence Cloud 页面链接，要求查看、修改、重写、加图、修正格式。
- 用户要求在某个页面下新建子页面，或删除某个页面（必须先确认，见“规则”）。
- 需要把页面内容拉到本地，用 Markdown 编辑后再推回去。

不适用：

- 没有页面链接的请求（例如“帮我在 Confluence 写点东西”）：先请用户给出页面链接，不要自己搜索或猜测页面。
- 本地仓库里已经有专门的同步流程时（例如某个仓库的 `scripts/confluence_sync.py`），镜像文档按仓库流程处理。

## 输入

- **页面链接**（必须由用户明确给出）。支持这些形式：`.../wiki/spaces/<KEY>/pages/<id>/...`、`.../pages/edit-v2/<id>`、`...?pageId=<id>`、短链 `.../wiki/x/<code>`。
- **凭据**：`~/.confluence/confluence.env`（或环境变量同名覆盖）：

  ```
  CONFLUENCE_BASE_URL=https://<site>.atlassian.net/wiki
  CONFLUENCE_EMAIL=<创建令牌的那个 Atlassian 账号的邮箱>
  CONFLUENCE_API_TOKEN=<API 令牌>
  ```

## 输出

- 修改后的页面（推送后给出页面链接），以及本次改动的 diff 摘要。
- 本地工作文件：`<id>-<标题>.md`（或 `.xml`）和旁边的 `.confluence.json`（记录页面 id 和拉取时的版本号，推送时用来检测冲突）。

## 执行步骤

下面的 `confluence.py` 都指 `scripts/confluence.py`（相对于本 skill 目录），用 `python3` 运行即可：缺少 `markdown` 包时它会自动换用 skill 自带的 `.venv`。

1. **首次调用前检查环境**：`python3 scripts/confluence.py doctor`。
   - 缺 `markdown`：直接运行 `bash scripts/install_deps.sh --no-plantuml`（不需要 sudo，可以代用户执行）。
   - 缺 `plantuml` / `java` / `dot`：需要 sudo 密码，**请用户自己在终端运行** `bash <skill 目录>/scripts/install_deps.sh`，装完再跑一次 `doctor`。只有画图或改图才需要它们；纯文字修改可以先做。
   - 缺凭据或令牌（`doctor` 或任何命令以退出码 3 结束）：**停下来提醒用户提供令牌**，说明在 https://id.atlassian.com/manage-profile/security/api-tokens 创建，并写入 `~/.confluence/confluence.env`、执行 `chmod 600`。建议用户自己写入文件，不要把令牌贴进对话；如果用户已经贴了，替他写入文件并 `chmod 600`，之后不要在任何输出里回显令牌。
2. **确认页面**：`confluence.py info <链接>`，把标题、所在空间、版本告诉用户，确认是要改的那个页面。`info` 也会说明这个页面能否完整转换成 Markdown。
3. **拉取**：
   - 能转换时：`confluence.py pull <链接> -o page.md`。
   - 页面含 Markdown 表达不了的内容（图片、附件、@提及、布局、其他插件的宏、合并单元格……）时：`confluence.py get-storage <链接> -o page.xml`，直接改页面源码（格式见 `references/storage-format.md`），保留不认识的部分原样不动。
4. **编辑**：按用户要求改本地文件，遵守下面的“页面内容规则”。画图用 PlantUML（见下文）。
5. **预览**：`confluence.py push page.md --dry-run`（或 `put-storage page.xml --dry-run`），检查 diff 只包含预期的改动。改动较大时先把 diff 摘要给用户看。
6. **推送**：去掉 `--dry-run`，可加 `-m "说明"` 作为版本备注。
   - 提示页面在拉取后被改过（别人或用户自己在浏览器里改了）：重新 `pull`，把本地改动合并进去再推。**只有用户明确同意覆盖时才用 `--force`。**
   - 提示 ASCII 图、`github.com` 引用、PlantUML 语法错误：按提示改正，不要绕过。
7. **核对**：再 `pull` 一次（或 `info`），确认版本号增加、内容正确，把页面链接交给用户。插件画的图只能在浏览器里看到渲染效果，需要时请用户看一眼。

## 规则

- **只操作用户给出的页面。** 不要顺带修改其父页面、子页面或同空间的其他页面。
- **新建、删除、移动页面前必须先询问用户并得到明确同意**，说清楚标题、位置和影响。
  - 新建：`confluence.py create --parent <父页面链接> new.md`（`--dry-run` 先看）。
  - 删除：`confluence.py delete <链接> --confirm-title "<完整标题>"`，只移到回收站；有子页面时拒绝。
- **不覆盖别人的修改，不静默丢内容。** `push` / `put-storage` 遇到版本冲突会拒绝；`pull` 遇到转换不了的内容会报错。按提示处理，不要用 `--force` 绕过，除非用户同意。
- **页面内容规则**（这些页面是给人读的）：
  - 不写给 AI 或工具看的说明（例如“本页由某脚本自动同步，请勿编辑”这类提示框）。
  - 不引用代码托管平台（GitHub 等）上的内容：仓库里的文件写成代码格式的路径，例如 `docker/setup.sh`，不加链接；克隆地址用 `<仓库地址>` 代替。推送时出现 `github.com` 会被拒绝。
  - **架构图一律用 PlantUML 插件画，不用 ASCII 字符画**；目录树可以保留为文本代码块。ASCII 图只允许出现在代码仓库自带的 Markdown 文档里。
- **不改用户手工内容的格式**：用 `get-storage` 改源码时，只改要改的部分，编辑器生成的属性（`local-id` 等）保持原样。

## PlantUML 图

- Markdown 里写成 ` ```plantuml ` 代码块（`@startuml` … `@enduml`），推送时自动变成 “PlantUML Diagram” 插件的图，图名取它前面最近的标题；拉取时插件图会还原成这样的代码块。
- 源码没变的图会原样保留（不丢插件里的修改历史）；新的或改过的图推送前会用本地 `plantuml -checkonly` 检查语法。
- 新图需要从空间里已有的一张插件图复制站点信息。如果整个空间还没有任何插件图，脚本会提示：请用户在任意页面手动插入一张 PlantUML 图（内容随意）后再继续。
- 想先看效果：`plantuml -tpng diagram.puml` 渲染成图片自己检查。布局建议见 `references/plantuml-app.md`。

## 资源

- `scripts/confluence.py`：全部固定流程（`doctor`、`check`、`info`、`pull`、`push`、`get-storage`、`put-storage`、`create`、`delete`、`plantuml encode/decode`）；`-h` 查看用法，退出码含义在文件开头。
- `scripts/install_deps.sh`：安装依赖（Python 包装进 skill 的 `.venv`；Java、Graphviz、固定版本的 PlantUML 需要 sudo）。
- `references/storage-format.md`：页面源码（storage XHTML）的格式、编辑器会加的东西、REST API 的用法和坑。
- `references/plantuml-app.md`：“PlantUML Diagram” 插件宏的存储格式、数据编码、站点信息。
