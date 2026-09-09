# 智仓成果弹窗 Markdown 预览功能实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 Silo 智仓的「查看报告」和「查看 Skill」弹窗提供自包含、零外部依赖的 Markdown 富文本渲染预览，并支持「预览/源码」切换与一键复制功能。

**Architecture:** 在 `app/templates/index.html` 中内嵌自包含 GFM Markdown 解析渲染器与专属于智仓书卷主题的 `.markdown-body` 样式，更新 `artifact-dialog` 结构与交互逻辑，并在 `tests/test_api.py` 中补充控制台模板契约测试。

**Tech Stack:** Vanilla JavaScript (ES6+), HTML5 `<dialog>`, CSS3, FastAPI, Pytest.

---

### Task 1: 编写控制台模板与 Markdown 预览元素的测试

**Files:**
- Modify: `tests/test_api.py`

- [ ] **Step 1: 编写测试用例**

在 `tests/test_api.py` 中增加对成果弹窗包含 Markdown 预览容器、切换按钮与一键复制按钮的验证：

```python
def test_console_contains_artifact_markdown_preview_components(tmp_path):
    settings = Settings(data_dir=tmp_path)
    with TestClient(build_app(settings)) as client:
        html = client.get("/").text
        assert "artifact-preview" in html
        assert "artifact-show-preview" in html
        assert "artifact-show-raw" in html
        assert "artifact-copy" in html
        assert "markdown-body" in html
```

- [ ] **Step 2: 运行测试并验证失败**

Run: `.venv/bin/pytest tests/test_api.py -k test_console_contains_artifact_markdown_preview_components`
Expected: FAIL with `AssertionError: assert 'artifact-preview' in html`

---

### Task 2: 在 `app/templates/index.html` 中实现 Markdown 解析器、CSS 样式与弹窗结构

**Files:**
- Modify: `app/templates/index.html`

- [ ] **Step 1: 添加 `.markdown-body` 专属排版样式与弹窗控件样式**

在 `app/templates/index.html` 的 `<style>` 中添加 `.markdown-body` 的视觉规范（层级标题、金色标记线、YAML 元数据卡片、表格边框斑马纹、代码块与行内代码高亮、引用块、列表及复制反馈浮层）。

- [ ] **Step 2: 更新 `artifact-dialog` 的 HTML 结构**

增加视图切换按钮（`#artifact-show-preview`、`#artifact-show-raw`）、一键复制按钮（`#artifact-copy`）和预览容器（`<div class="reader markdown-body" id="artifact-preview"></div>`）。

- [ ] **Step 3: 内嵌自包含 GFM Markdown 解析函数 `renderMarkdown(md)` 与交互逻辑**

实现完整的 GFM 解析：
- YAML Frontmatter 提取与格式化
- 标题（H1~H6）
- 粗体、斜体、删除线
- 代码块（带等宽字体与语法底色）与行内代码
- 表格（支持表头与对齐）
- 引用块（`>`）与嵌套列表（有序、无序、任务复选框）
- 段落与水平线
- 复制剪贴板与临时反馈机制

- [ ] **Step 4: 运行测试并验证通过**

Run: `.venv/bin/pytest -q`
Expected: All tests PASS

---

### Task 3: 整体功能回归与自验

**Files:**
- Modify: `tests/test_api.py` (如需)

- [ ] **Step 1: 运行全部单元与集成测试**

Run: `.venv/bin/pytest -q`
Run: `.venv/bin/ruff check app tests`

- [ ] **Step 2: 验证无任何控制台报错或破坏性改动**
