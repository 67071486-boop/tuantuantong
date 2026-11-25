"""频道文章正文的**清洗**与**移动端块解析**（唯一实现处）。 ⛔ **只有这一份**：写入前清洗（`sanitize_html`）与读取时解析（`parse_blocks` / `extract_image_keys`）都从这里走。视图里再写第二份，两边一漂就是「编辑器里看得见、移动端看不见」那类事故（`AGENTS.md` §5-E：同一个事实不许两套判据）。 ## 白名单（业务方确认的「富文本基础版」） - 保留：`p` / `h2` / `h3` / `strong` / `em` / `br` / `img` / `ul` / `ol` / `li`。 - **别名归一化**（前端 `contenteditable` 实际会产出它们，不认就等于用户加粗白按了）：
`b` → `strong`、`i` → `em`（`document.execCommand('bold')` 常产出 `<b>`）、 `div` → `p`（浏览器回车常生成 `<div>`；⛔ 不能「丢掉 div 标签、把文字粘成一团」）。 - **内容也要丢**：`script` / `style` 连里面的文本一起丢（只丢标签会把它变成正文）。 - 其余标签：标签丢掉、**文本保留**（避免用户在别处粘进来的内容整段消失）。 - 属性：**只有** `img` 的 `data-key` 保留，其它属性（`style` / `class` / `onerror` / `href` …）一律丢弃。⛔ `src` 尤其不许落库——签名 URL 是会过期的，写死进去等于存了一条死链
（与头像 / 订单截图 / 游戏 logo 的「只存对象名」同一条口径，见 `apps/core/storage.py`）。 ## `data-key` 的白名单（不是防御性写法，是落库前的边界）对象名的形状是 `<租户>/users/<用户 id>/article-media.<随机>.<ext>`，只允许 `[A-Za-z0-9._/-]`，且**不许出现 `..` 段**（`demo/../victim/…` 能过字符串前缀、解析后却落在别人的命名空间里，见 `apps/core/storage.py::key_belongs_to_tenant` 的 docstring）。不合法的 `data-key` ⇒ **整张图丢掉**，⛔ 不原样落库、也不改写成别的形状。 ## 为什么用 `html.parser.HTMLParser` 而不是正则标准库的 `HTMLParser` 自己处理未闭合标签、嵌套与实体（`convert_charrefs=True` 时 `&amp;` / `&#x27;` 已解成字符）；正则做不到这三件事，尤其是 `strong` / `em` 的嵌套与 `<p>a<strong>b` 这种没闭合的收尾。"""
from __future__ import annotations

import html as html_module
import re
from html.parser import HTMLParser

#: 允许落库的标签（规范化之后的形状）。
ALLOWED_TAGS = ("p", "h2", "h3", "strong", "em", "br", "img", "ul", "ol", "li")

#: 别名 → 规范标签。见模块 docstring 的「别名归一化」。
TAG_ALIASES = {"b": "strong", "i": "em", "div": "p"}

#: 连内容一起丢的标签。
DROP_CONTENT_TAGS = ("script", "style")

#: 自闭合（没有文本内容）的标签。
VOID_TAGS = ("br", "img")

#: 图片对象名属性。
IMAGE_KEY_ATTR = "data-key"

#: 合法的 `data-key` 取值（不允许 `..` 段，见模块 docstring）。
IMAGE_KEY_RE = re.compile(r"^[A-Za-z0-9._/-]+$")

#: 正文长度上限（字符）。**接口层与模型层共用的那条口径**：序列化器按它拒，
#: 超限回 `code=1000` + 一句中文。定这个数是因为 `content_html` 是 `TextField`（库层没有上限），
#: 而一段 10 万字的富文本已经远远超出「文章作品」的用途，再大只会拖慢列表 / 详情。
MAX_CONTENT_LENGTH = 100_000

#: 能被解析成移动端块的块级标签（`ul` / `ol` 只是结构，不单独出块）。
BLOCK_TAGS = ("p", "h2", "h3", "li")


def is_valid_image_key(key: str) -> bool:
    """对象名是否合法（白名单字符 + 不含 `..` 段）。清洗与块解析**共用这一份**。"""
    if not key or ".." in key:
        return False
    return bool(IMAGE_KEY_RE.match(key))


def _emit_attrs_for_img(attrs) -> str:
    """把 `<img>` 的属性收成「只剩一个 `data-key`」；没有合法 key 回空串（整张图丢掉）。"""
    key = ""
    for name, value in attrs:
        if name.lower() == IMAGE_KEY_ATTR:
            key = (value or "").strip()
    if not is_valid_image_key(key):
        return ""
    return f'<img data-key="{html_module.escape(key, quote=True)}">'


class _Sanitizer(HTMLParser):
    """把任意 HTML 洗成白名单形状（输出永远闭合，丢弃的属性一个不留）。
    `_stack` 存**已经输出**的开放标签（规范化名）；`_skip` 是 `script` / `style` 的嵌套深度。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._stack: list[str] = []
        self._skip = 0

    # -- 输出 ---------------------------------------------------------------
    def _open(self, tag: str) -> None:
        self._out.append(f"<{tag}>")
        self._stack.append(tag)

    def _close_one(self) -> str:
        tag = self._stack.pop()
        self._out.append(f"</{tag}>")
        return tag

    # -- HTMLParser 回调 ----------------------------------------------------
    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in DROP_CONTENT_TAGS:
            self._skip += 1
            return
        if self._skip:
            return
        if tag == "img":
            # ⚠️ 自闭合：不进 `_stack`（`<img>` 没有闭合标签）。
            self._out.append(_emit_attrs_for_img(attrs))
            return
        canonical = TAG_ALIASES.get(tag, tag)
        if canonical not in ALLOWED_TAGS:
            return
        if canonical == "br":
            self._out.append("<br>")
            return
        self._open(canonical)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in DROP_CONTENT_TAGS:
            if self._skip:
                self._skip -= 1
            return
        if self._skip:
            return
        canonical = TAG_ALIASES.get(tag, tag)
        if canonical in VOID_TAGS:
            return
        if canonical not in self._stack:
            return
        # 未闭合的内层标签一并收口（`<p>a<strong>b</p>` ⇒ `</strong></p>`），
        # 否则存下来的 HTML 里会有跨块的开放标签。
        while self._stack:
            if self._close_one() == canonical:
                return

    def handle_data(self, data):
        if self._skip or not data:
            return
        self._out.append(html_module.escape(data, quote=False))

    def handle_startendtag(self, tag, attrs):
        # `<img …/>` / `<br/>`：HTMLParser 的默认实现会分别调 start + end，
        # 对自闭合标签走在这里更容易读，且不会误进 `_stack`。
        self.handle_starttag(tag, attrs)

    def handle_comment(self, data):
        # 注释一律丢（里面可能藏着条件注释 / 脚本片段）。
        return

    def result(self) -> str:
        while self._stack:
            self._close_one()
        return "".join(self._out)


def sanitize_html(raw: str) -> str:
    """把正文洗成白名单 HTML（⛔ 落库前必须过它）。这是**幂等**的（已洗净的内容再洗一遍逐字相同），所以读路径再洗一次也不会累积转义
    ——`parse_blocks` 就靠这一点对「脏数据 / 老数据」也安全。"""
    parser = _Sanitizer()
    parser.feed(raw or "")
    parser.close()
    return parser.result()


class _BlockParser(HTMLParser):
    """把（已清洗的）正文解析成移动端直出的扁平块。"""

    def __init__(self, resolve_url) -> None:
        super().__init__(convert_charrefs=True)
        self._resolve_url = resolve_url
        self._blocks: list[dict] = []
        self._current: dict | None = None
        self._stack: list[str] = []
        self._bold = 0
        self._italic = 0
        self._skip = 0

    # -- 块与片段 -----------------------------------------------------------
    def _flush(self) -> None:
        block = self._current
        self._current = None
        if not block:
            return
        text = "".join(span["text"] for span in block["spans"])
        if not text.strip():
            # 空块跳过：`<p></p>` / `<p><br></p>` / 空 `<div>` 都不该在移动端占一行。
            return
        self._blocks.append(block)

    def _ensure_block(self) -> dict:
        """取当前块；没有就开一个**隐式段落**（contenteditable 顶层的裸文本）。"""
        if self._current is None:
            self._current = {"type": "p", "spans": []}
        return self._current

    def _append_text(self, text: str) -> None:
        if not text:
            return
        block = self._ensure_block()
        bold = self._bold > 0
        italic = self._italic > 0
        spans = block["spans"]
        if spans and spans[-1]["bold"] == bold and spans[-1]["italic"] == italic:
            spans[-1]["text"] += text
            return
        spans.append({"text": text, "bold": bold, "italic": italic})

    # -- HTMLParser 回调 ----------------------------------------------------
    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in DROP_CONTENT_TAGS:
            self._skip += 1
            return
        if self._skip:
            return
        if tag == "img":
            key = ""
            for name, value in attrs:
                if name.lower() == IMAGE_KEY_ATTR:
                    key = (value or "").strip()
            if not is_valid_image_key(key):
                return
            self._flush()
            self._blocks.append(
                {"type": "img", "key": key, "url": self._resolve_url(key)}
            )
            return
        canonical = TAG_ALIASES.get(tag, tag)
        if canonical not in ALLOWED_TAGS:
            return
        if canonical == "br":
            # `<br>` = 当前块里的换行（没有当前块时先开个隐式段落）。
            self._append_text("\n")
            return
        if canonical in BLOCK_TAGS:
            self._flush()
            self._current = {"type": canonical, "spans": []}
            self._stack.append(canonical)
            return
        if canonical == "strong":
            self._bold += 1
        elif canonical == "em":
            self._italic += 1
        self._stack.append(canonical)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in DROP_CONTENT_TAGS:
            if self._skip:
                self._skip -= 1
            return
        if self._skip:
            return
        canonical = TAG_ALIASES.get(tag, tag)
        if canonical in VOID_TAGS or canonical not in self._stack:
            return
        while self._stack:
            popped = self._stack.pop()
            if popped == "strong":
                self._bold = max(0, self._bold - 1)
            elif popped == "em":
                self._italic = max(0, self._italic - 1)
            if popped == canonical:
                break
        if canonical in BLOCK_TAGS and self._current is not None:
            if self._current["type"] == canonical:
                self._flush()

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_data(self, data):
        if self._skip or not data:
            return
        self._append_text(data)

    def handle_comment(self, data):
        return

    def result(self) -> list[dict]:
        self._flush()
        return self._blocks


def parse_blocks(html: str, resolve_url) -> list[dict]:
    """把正文 HTML 解析成移动端块列表（图片 key 已换成 `resolve_url(key)`）。返回的形状（计划 §1）： - `{"type": "p"|"h2"|"h3"|"li", "spans": [{"text", "bold", "italic"}, …]}`
    - `{"type": "img", "key": …, "url": …}` ⚠️ 先过 `sanitize_html` 再解析：读路径上的老数据也走同一道白名单，所以 `<script>` 之类**不可能**借这条路径漏到移动端。
    ⚠️ `resolve_url` 由调用方给（公开详情 / 管理端 media 各签各的时效），本模块不碰存储层 ——`apps/business` 里只有这里需要它，写死成 `presign_get` 会让清洗/解析不可单测。"""
    parser = _BlockParser(resolve_url)
    parser.feed(sanitize_html(html or ""))
    parser.close()
    return parser.result()


def extract_image_keys(html: str) -> list[str]:
    """正文里出现过的图片 key（**按出现顺序去重**）。管理端编辑器的 `media` 字段靠它取读链接：⛔ 不要用「解析块 + 过滤 img」那条路
    ——那会逐张调 `resolve_url`，而这里只需要 key 清单。"""
    parser = _BlockParser(lambda key: "")
    parser.feed(sanitize_html(html or ""))
    parser.close()
    seen: list[str] = []
    for block in parser.result():
        if block.get("type") != "img":
            continue
        key = block.get("key") or ""
        if key and key not in seen:
            seen.append(key)
    return seen
