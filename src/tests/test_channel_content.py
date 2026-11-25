"""频道文章正文的清洗与块解析（`apps/business/article_content.py`，2026-10-23）。

纯单元测试（`SimpleTestCase`，不碰数据库 / 不碰存储）：

- **清洗**：白名单、别名归一化（`b`/`i`/`div`）、`script`/`style` 连内容丢、
  属性只留 `img[data-key]`、`data-key` 白名单、未闭合标签收口；
- **块解析**：`p`/`h2`/`h3`/`li` 出块、`strong`/`em` 进 `spans`、`br` 变 `\\n`、
  空块跳过、`img` 的 key 换成 URL、实体解码；
- `extract_image_keys`：按出现顺序去重。

⚠️ 断言的是**字面量**（E-0024），⛔ 不从被测模块 import 期望值——否则改坏实现时
期望值跟着变、本文件恒绿。
"""
from django.test import SimpleTestCase

from apps.business.article_content import (
    extract_image_keys,
    parse_blocks,
    sanitize_html,
)

#: 一个形状合法的文章图片对象名（`<租户>/users/<用户 id>/article-media.<随机>.<ext>`）。
KEY_A = "demo/users/1/article-media.abc123.png"
KEY_B = "demo/users/2/article-media.def456.jpg"


def _resolve(key: str) -> str:
    return f"https://cdn.example/{key}?sign=x"


class SanitizeHtmlTests(SimpleTestCase):
    def test_script_and_style_drop_their_content(self):
        raw = (
            "<p>前</p><script>alert('xss')</script>"
            "<style>p{color:red}</style><p>后</p>"
        )
        cleaned = sanitize_html(raw)
        self.assertEqual(cleaned, "<p>前</p><p>后</p>")
        self.assertNotIn("alert", cleaned)
        self.assertNotIn("color:red", cleaned)

    def test_event_handlers_and_unknown_attributes_are_dropped(self):
        raw = '<p onclick="steal()" class="x" style="color:red">正文</p>'
        self.assertEqual(sanitize_html(raw), "<p>正文</p>")

    def test_link_is_dropped_but_its_text_survives(self):
        """`<a href="javascript:…">` —— 标签丢掉、文本保留（⛔ 不整段消失）。"""
        raw = '<p><a href="javascript:alert(1)">点我</a></p>'
        self.assertEqual(sanitize_html(raw), "<p>点我</p>")

    def test_img_keeps_only_data_key_and_never_src(self):
        raw = (
            '<p><img src="https://bucket.example/x.png?sig=1" '
            f'data-key="{KEY_A}" onerror="alert(1)" width="10"></p>'
        )
        cleaned = sanitize_html(raw)
        self.assertEqual(cleaned, f'<p><img data-key="{KEY_A}"></p>')
        self.assertNotIn("src=", cleaned)
        self.assertNotIn("onerror", cleaned)
        self.assertNotIn("https://bucket.example", cleaned)

    def test_img_without_a_legal_key_is_dropped(self):
        # 没有 data-key ⇒ 丢掉整张图（没有 key 就签不出 URL）。
        self.assertEqual(sanitize_html('<p><img src="https://x/y.png"></p>'), "<p></p>")
        # `..` 段 ⇒ 丢弃（防跨命名空间）。
        self.assertEqual(sanitize_html('<img data-key="demo/../victim/x.png">'), "")
        # 白名单外的字符 ⇒ 丢弃。
        self.assertEqual(sanitize_html('<img data-key="demo/users/1/a b.png">'), "")
        self.assertEqual(sanitize_html('<img data-key="demo/users/1/a&quot;.png">'), "")

    def test_bold_and_italic_aliases_become_strong_and_em(self):
        """`document.execCommand('bold')` 常产出 `<b>`；不归一化 = 用户加粗白按了。"""
        self.assertEqual(
            sanitize_html("<p><b>粗</b>与<i>斜</i></p>"),
            "<p><strong>粗</strong>与<em>斜</em></p>",
        )

    def test_div_becomes_paragraph(self):
        """浏览器回车常生成 `<div>` ⇒ 落库统一成 `<p>`（⛔ 不是丢掉标签、把文本粘成一团）。"""
        self.assertEqual(
            sanitize_html("<div>第一段</div><div>第二段</div>"),
            "<p>第一段</p><p>第二段</p>",
        )

    def test_nested_and_unclosed_tags_are_closed(self):
        self.assertEqual(
            sanitize_html("<p>a<strong>b<em>c</p>"),
            "<p>a<strong>b<em>c</em></strong></p>",
        )

    def test_entities_are_decoded_then_re_escaped_once(self):
        # `&amp;` 解成 `&`、再转义回 `&amp;` ⇒ 不双重转义（`&amp;amp;` 是错的）。
        self.assertEqual(sanitize_html("<p>A &amp; B</p>"), "<p>A &amp; B</p>")
        self.assertEqual(sanitize_html("<p>&lt;script&gt;</p>"), "<p>&lt;script&gt;</p>")

    def test_whitelisted_block_tags_survive(self):
        raw = "<h2>标题</h2><ul><li>一</li><li>二</li></ul><br>"
        self.assertEqual(sanitize_html(raw), raw)

    def test_sanitize_is_idempotent(self):
        raw = f'<p>x<b>y</b></p><p><img src="s" data-key="{KEY_A}"></p>'
        once = sanitize_html(raw)
        self.assertEqual(sanitize_html(once), once)

    def test_empty_and_blank_input(self):
        self.assertEqual(sanitize_html(""), "")
        self.assertEqual(sanitize_html("   "), "   ")


class ParseBlocksTests(SimpleTestCase):
    def test_plain_paragraph(self):
        self.assertEqual(
            parse_blocks("<p>你好</p>", _resolve),
            [{"type": "p", "spans": [{"text": "你好", "bold": False, "italic": False}]}],
        )

    def test_strong_and_italic_go_into_spans(self):
        blocks = parse_blocks("<p>普通<strong>粗</strong><em>斜</em></p>", _resolve)
        self.assertEqual(
            blocks,
            [
                {
                    "type": "p",
                    "spans": [
                        {"text": "普通", "bold": False, "italic": False},
                        {"text": "粗", "bold": True, "italic": False},
                        {"text": "斜", "bold": False, "italic": True},
                    ],
                }
            ],
        )

    def test_nested_strong_and_em(self):
        blocks = parse_blocks("<p><strong>粗<em>又粗又斜</em></strong></p>", _resolve)
        self.assertEqual(
            blocks[0]["spans"],
            [
                {"text": "粗", "bold": True, "italic": False},
                {"text": "又粗又斜", "bold": True, "italic": True},
            ],
        )

    def test_headings_and_list_items_have_their_own_types(self):
        blocks = parse_blocks("<h2>大标题</h2><h3>小标题</h3><ul><li>条目</li></ul>", _resolve)
        self.assertEqual([b["type"] for b in blocks], ["h2", "h3", "li"])

    def test_br_becomes_newline_inside_the_block(self):
        blocks = parse_blocks("<p>第一行<br>第二行</p>", _resolve)
        self.assertEqual(
            blocks,
            [{"type": "p", "spans": [{"text": "第一行\n第二行", "bold": False, "italic": False}]}],
        )

    def test_empty_blocks_are_skipped(self):
        self.assertEqual(parse_blocks("<p></p><p>  </p><p><br></p><div></div>", _resolve), [])
        self.assertEqual(parse_blocks("", _resolve), [])

    def test_image_key_becomes_signed_url(self):
        blocks = parse_blocks(f'<p><img data-key="{KEY_A}"></p>', _resolve)
        self.assertEqual(
            blocks,
            [{"type": "img", "key": KEY_A, "url": f"https://cdn.example/{KEY_A}?sign=x"}],
        )

    def test_script_never_reaches_blocks(self):
        """解析前先清洗 ⇒ 脏数据 / 老数据也漏不到移动端。"""
        blocks = parse_blocks("<script>alert(1)</script><p>正文</p>", _resolve)
        self.assertEqual([b["type"] for b in blocks], ["p"])
        self.assertNotIn("alert", str(blocks))

    def test_entities_are_decoded(self):
        blocks = parse_blocks("<p>A &amp; B &lt; C</p>", _resolve)
        self.assertEqual(blocks[0]["spans"][0]["text"], "A & B < C")

    def test_paragraph_after_image_is_its_own_block(self):
        blocks = parse_blocks(
            f'<p>前<img data-key="{KEY_A}">后</p>', _resolve
        )
        self.assertEqual([b["type"] for b in blocks], ["p", "img", "p"])


class ExtractImageKeysTests(SimpleTestCase):
    def test_keys_are_ordered_and_deduplicated(self):
        raw = (
            f'<p><img data-key="{KEY_A}"></p>'
            f'<p><img data-key="{KEY_B}"></p>'
            f'<p><img data-key="{KEY_A}"></p>'
        )
        self.assertEqual(extract_image_keys(raw), [KEY_A, KEY_B])

    def test_no_image_gives_empty_list(self):
        self.assertEqual(extract_image_keys("<p>纯文字</p>"), [])
