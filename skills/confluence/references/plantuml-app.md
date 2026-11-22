# “PlantUML Diagram” 插件（Forge 应用 `plantumlcloud`）

页面上用这个插件画架构图。它是 Forge 应用，存储成 `ac:adf-extension`，而不是传统的 `ac:structured-macro`。同一个站点里可能还装了别的画图插件（例如 “All-In-One Diagrams for Confluence”，存的是 Mermaid），格式不同，不要混用。

## 识别

`ac:adf-attribute key="extension-key"` 以 `/static/plantumlcloud` 结尾，例如：

```
85516c62-101e-41a9-b155-0be6a906042a/2c435852-96a8-44fb-a8fc-cf5fa3fb4040/static/plantumlcloud
```

前两段是应用 id 和环境 id，不同站点可能不同，所以要从已有的图里读取，不要写死。

## 结构

```xml
<ac:adf-extension>
  <ac:adf-node type="extension">
    <ac:adf-attribute key="extension-key">{应用 key}</ac:adf-attribute>
    <ac:adf-attribute key="extension-type">com.atlassian.ecosystem</ac:adf-attribute>
    <ac:adf-attribute key="parameters">
      <ac:adf-parameter key="local-id">{uuid}</ac:adf-parameter>
      <ac:adf-parameter key="extension-id">ari:cloud:ecosystem::extension/{应用 key}</ac:adf-parameter>
      <ac:adf-parameter key="extension-title">PlantUML Diagram</ac:adf-parameter>
      <ac:adf-parameter key="layout">extension</ac:adf-parameter>
      <ac:adf-parameter key="forge-environment">PRODUCTION</ac:adf-parameter>
      <ac:adf-parameter key="render">default</ac:adf-parameter>
      <ac:adf-parameter key="embedded-macro-context">
        <ac:adf-parameter key="extension-data">
          <ac:adf-parameter key="type">macro</ac:adf-parameter>
          <ac:adf-parameter key="content">
            <ac:adf-parameter key="id">{页面 id}</ac:adf-parameter>
            <ac:adf-parameter key="type">page</ac:adf-parameter>
            <ac:adf-parameter key="version" type="integer">{插入时的页面版本}</ac:adf-parameter>
          </ac:adf-parameter>
          <ac:adf-parameter key="space">
            <ac:adf-parameter key="key">{空间 key}</ac:adf-parameter>
            <ac:adf-parameter key="id">{空间 id}</ac:adf-parameter>
          </ac:adf-parameter>
        </ac:adf-parameter>
        <ac:adf-parameter key="context-ids"><ac:adf-parameter-value>ari:cloud:confluence:{cloud id}:workspace/{workspace id}</ac:adf-parameter-value></ac:adf-parameter>
        <ac:adf-parameter key="account-id">{插入者的账号 id}</ac:adf-parameter>
        <ac:adf-parameter key="cloud-id">{cloud id}</ac:adf-parameter>
      </ac:adf-parameter>
      <ac:adf-parameter key="guest-params">
        <ac:adf-parameter key="id">{uuid}</ac:adf-parameter>
        <ac:adf-parameter key="name">{图名，编辑器里显示}</ac:adf-parameter>
        <ac:adf-parameter key="data">{压缩后的源码}</ac:adf-parameter>
        <ac:adf-parameter key="revision" type="integer">1</ac:adf-parameter>
        <ac:adf-parameter key="compressed" type="boolean">true</ac:adf-parameter>
        <ac:adf-parameter key="history"><ac:adf-parameter-value>
          <ac:adf-parameter key="data">{同上}</ac:adf-parameter>
          <ac:adf-parameter key="revision" type="integer">1</ac:adf-parameter>
          <ac:adf-parameter key="created-at">2026-11-22T15:09:02.736Z</ac:adf-parameter>
        </ac:adf-parameter-value></ac:adf-parameter>
        <ac:adf-parameter key="draft" type="boolean">false</ac:adf-parameter>
      </ac:adf-parameter>
    </ac:adf-attribute>
    <ac:adf-attribute key="text">PlantUML Diagram</ac:adf-attribute>
    <ac:adf-attribute key="layout">default</ac:adf-attribute>
    <ac:adf-attribute key="local-id">{同 local-id}</ac:adf-attribute>
  </ac:adf-node>
  <ac:adf-fallback>{整个 adf-node 再写一份}</ac:adf-fallback>
</ac:adf-extension>
```

## 源码编码

`data` = base64（标准字母表）( raw deflate（无 zlib 头，level 9）( UTF-8 源码 ) )。`compressed=false` 时 `data` 直接是源码。

```python
zlib.decompress(base64.b64decode(data), -15).decode()                  # 解码
c = zlib.compressobj(9, zlib.DEFLATED, -15)
base64.b64encode(c.compress(src.encode()) + c.flush()).decode()        # 编码，和插件结果逐字节一致
```

命令行：`confluence.py plantuml decode <data>` / `confluence.py plantuml encode diagram.puml`。

## 站点信息从哪来

`embedded-macro-context` 里的 workspace id 在 REST API 里拿不到，所以 `confluence.py` 会从**同一空间里已有的任意一张插件图**复制 cloud id、账号 id、context-ids 和空间信息，再换成目标页面的 id 和版本。空间里一张都没有时，请用户在任意页面手动插入一张图（内容随意），例如：

```
@startuml
A --> B : test
@enduml
```

之后就能自动生成了。这张测试图用完可以删掉。

## 新图与改图

- 源码没变的图：原样保留整段 XML，包括 id、`revision` 和 `history`，插件里的修改历史不会丢。
- 新图或改过的图：生成新的一段，`revision=1`，`history` 只有一条。`local-id` 和 `guest-params/id` 由页面 id、序号和源码派生出确定的 uuid5，重复推送不会制造无谓的变化。
- 图名（`name`）取图前面最近的标题文字。

## 画图建议

- 部署图、组件图：`skinparam componentStyle rectangle`、`skinparam shadowing false`，用 `rectangle "分组" { component ... }` 表示进程或板子，`queue` 表示消息总线或 topic，`file` / `folder` 表示文件和目录，`agent` 表示外部传感器或设备。
- 有先后顺序的交互（锁步、唤醒、请求/应答）用时序图（`participant`、`loop`、`note over`），比方框图清楚。
- 布局拥挤时用 `left to right direction`，或者给箭头加方向，例如 `-right->`、`-up->`。
- 标签里的 `<b>`、`<i>`、`<u>`、`<color:…>`、`<size:…>` 等是格式标签；要原样显示时写成 `~<b>`。不认识的尖括号内容（例如 `<board>`）会原样显示。
- 发布前 `plantuml -checkonly *.puml` 检查语法，再用 `plantuml -tpng` 渲染出来看一遍；插件渲染的效果只能在浏览器里看到。
