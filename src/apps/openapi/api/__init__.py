"""开放平台对外端点的视图包（spec §12.1：`api/{__init__,app_views,order_query_views}.py`）。

`app_views.py`（应用管理端点）属 **T5**，本包今天只有 `order_query_views.py`（⚠️ 名字与 spec §12.1 的初稿 `order_views.py` 不同：
后者会让 `src/` 内出现**第二个**同名 basename，从而让 13 处既有注释引用的行号校验**静默失效**
——见 `tests/test_doc_line_references.py` 的 `_EXPECTED_SKIPPED_REFERENCES` 口径）。
"""
