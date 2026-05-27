#!/usr/bin/env python3
"""
API 文档生成器 - 基于 AST 静态解析，无需导入任何依赖。

用法:
    python scripts/generate_api_docs.py

输出:
    docs/api/index.html  - 主页
    docs/api/*.html      - 各模块页面
"""

import ast
import html
import os
import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

# ──────────────────────────── 项目根目录 ────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIR = PROJECT_ROOT / "fast3r"
OUTPUT_DIR = PROJECT_ROOT / "docs" / "api"


# ═══════════════════════════ 数据模型 ═══════════════════════════════
@dataclass
class ArgInfo:
    name: str
    annotation: Optional[str] = None
    default: Optional[str] = None


@dataclass
class FuncInfo:
    name: str
    kind: str  # "function", "method", "staticmethod", "classmethod"
    docstring: Optional[str] = None
    args: list = field(default_factory=list)
    returns: Optional[str] = None
    line_start: int = 0
    line_end: int = 0
    decorators: list = field(default_factory=list)


@dataclass
class ClassInfo:
    name: str
    bases: list = field(default_factory=list)
    docstring: Optional[str] = None
    methods: list = field(default_factory=list)
    class_vars: list = field(default_factory=list)
    line_start: int = 0
    line_end: int = 0
    decorators: list = field(default_factory=list)


@dataclass
class ModuleInfo:
    name: str
    filepath: str
    docstring: Optional[str] = None
    functions: list = field(default_factory=list)
    classes: list = field(default_factory=list)
    imports: list = field(default_factory=list)


# ═══════════════════════════ AST 解析 ═══════════════════════════════
def _annotation_to_str(node):
    """将 AST 注解节点转为字符串。"""
    if node is None:
        return None
    try:
        return ast.unparse(node)
    except Exception:
        return str(node)


def _default_to_str(node):
    """将 AST 默认值节点转为字符串。"""
    if node is None:
        return None
    try:
        return ast.unparse(node)
    except Exception:
        return "..."


def _parse_args(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list:
    """解析函数参数。"""
    args = []
    pos_only = getattr(node.args, "posonlyargs", [])

    for arg in pos_only:
        args.append(ArgInfo(
            name=arg.arg,
            annotation=_annotation_to_str(arg.annotation),
        ))

    for arg in node.args.args:
        args.append(ArgInfo(
            name=arg.arg,
            annotation=_annotation_to_str(arg.annotation),
        ))

    if node.args.vararg:
        args.append(ArgInfo(
            name=f"*{node.args.vararg.arg}",
            annotation=_annotation_to_str(node.args.vararg.annotation),
        ))

    for arg in node.args.kwonlyargs:
        args.append(ArgInfo(
            name=arg.arg,
            annotation=_annotation_to_str(arg.annotation),
            default="...",  # 标记为 kwonly
        ))

    if node.args.kwarg:
        args.append(ArgInfo(
            name=f"**{node.args.kwarg.arg}",
            annotation=_annotation_to_str(node.args.kwarg.annotation),
        ))

    # 填充默认值
    defaults = node.args.defaults
    n_args = len(node.args.args)
    n_defaults = len(defaults)
    for i, d in enumerate(defaults):
        idx = n_args - n_defaults + i
        if idx < len(args) and args[idx].name == node.args.args[idx].arg:
            args[idx].default = _default_to_str(d)

    kw_defaults = node.args.kw_defaults
    for i, d in enumerate(kw_defaults):
        if d is not None and i < len(node.args.kwonlyargs):
            for a in args:
                if a.name == node.args.kwonlyargs[i].arg:
                    a.default = _default_to_str(d)
                    break

    # 跳过 self / cls
    if args and args[0].name in ("self", "cls"):
        args = args[1:]

    return args


def _get_decorators(node):
    """获取装饰器列表。"""
    decs = []
    for d in node.decorator_list:
        try:
            decs.append(ast.unparse(d))
        except Exception:
            decs.append("...")
    return decs


def parse_file(filepath: str, module_name: str) -> ModuleInfo:
    """解析单个 Python 文件，提取所有类和函数信息。"""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source, filename=filepath)
    except SyntaxError as e:
        print(f"  [WARN] 语法错误 {filepath}: {e}")
        return ModuleInfo(name=module_name, filepath=filepath)
    except Exception as e:
        print(f"  [WARN] 无法解析 {filepath}: {e}")
        return ModuleInfo(name=module_name, filepath=filepath)

    mod = ModuleInfo(name=module_name, filepath=filepath)
    mod.docstring = ast.get_docstring(tree)

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func = FuncInfo(
                name=node.name,
                kind="function",
                docstring=ast.get_docstring(node),
                args=_parse_args(node),
                returns=_annotation_to_str(node.returns),
                line_start=node.lineno,
                line_end=node.end_lineno or node.lineno,
                decorators=_get_decorators(node),
            )
            mod.functions.append(func)

        elif isinstance(node, ast.ClassDef):
            cls = ClassInfo(
                name=node.name,
                bases=[_annotation_to_str(b) for b in node.bases],
                docstring=ast.get_docstring(node),
                line_start=node.lineno,
                line_end=node.end_lineno or node.lineno,
                decorators=_get_decorators(node),
            )
            for item in ast.iter_child_nodes(node):
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    kind = "method"
                    for d in item.decorator_list:
                        try:
                            d_str = ast.unparse(d)
                        except Exception:
                            d_str = ""
                        if d_str == "staticmethod":
                            kind = "staticmethod"
                        elif d_str == "classmethod":
                            kind = "classmethod"

                    method = FuncInfo(
                        name=item.name,
                        kind=kind,
                        docstring=ast.get_docstring(item),
                        args=_parse_args(item),
                        returns=_annotation_to_str(item.returns),
                        line_start=item.lineno,
                        line_end=item.end_lineno or item.lineno,
                        decorators=_get_decorators(item),
                    )
                    cls.methods.append(method)

                elif isinstance(item, (ast.Assign, ast.AnnAssign)):
                    # 类变量
                    if isinstance(item, ast.Assign):
                        for t in item.targets:
                            if isinstance(t, ast.Name):
                                cls.class_vars.append(t.id)
                    elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                        cls.class_vars.append(item.target.id)

            mod.classes.append(cls)

    return mod


def collect_modules(source_dir: Path, base_package: str = "fast3r") -> list:
    """递归收集所有 Python 模块。"""
    modules = []
    for root, dirs, files in os.walk(source_dir):
        # 跳过 __pycache__
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in sorted(files):
            if f.endswith(".py"):
                filepath = os.path.join(root, f)
                rel = os.path.relpath(filepath, source_dir.parent)
                module_name = rel.replace(os.sep, ".").removesuffix(".py")
                if module_name.endswith(".__init__"):
                    module_name = module_name[: -len(".__init__")]
                modules.append((filepath, module_name))
    return modules


# ═══════════════════════════ HTML 生成 ═══════════════════════════════
def _esc(text):
    """HTML 转义。"""
    if text is None:
        return ""
    return html.escape(str(text))


def _format_signature(func: FuncInfo, include_name: bool = True) -> str:
    """格式化函数签名为 HTML。"""
    parts = []
    if include_name:
        parts.append(f'<span class="fn-name">{_esc(func.name)}</span>')

    args_html = []
    for a in func.args:
        s = f'<span class="arg-name">{_esc(a.name)}</span>'
        if a.annotation:
            s += f': <span class="arg-type">{_esc(a.annotation)}</span>'
        if a.default:
            s += f' = <span class="arg-default">{_esc(a.default)}</span>'
        args_html.append(s)

    parts.append("(" + ", ".join(args_html) + ")")

    if func.returns:
        parts.append(f' -> <span class="ret-type">{_esc(func.returns)}</span>')

    return " ".join(parts)


def _format_docstring(doc: Optional[str]) -> str:
    """格式化 docstring 为 HTML。"""
    if not doc:
        return '<p class="no-doc">⚠️ 缺少 docstring</p>'
    # 简单换行处理
    lines = _esc(doc).split("\n")
    # 检测 Google / NumPy 风格的段落标题
    result = []
    in_block = False
    for line in lines:
        stripped = line.strip()
        # Google 风格标题: Args:, Returns:, Raises: 等
        if stripped.endswith(":") and stripped[:-1].strip().isalpha() and len(stripped) > 2:
            if in_block:
                result.append("</div>")
            result.append(f'<div class="doc-section"><strong>{stripped}</strong>')
            in_block = True
        else:
            if in_block and stripped:
                result.append(f'<div class="doc-detail">{line}</div>')
            else:
                if in_block:
                    result.append("</div>")
                    in_block = False
                result.append(f'<p>{line}</p>' if stripped else "")
    if in_block:
        result.append("</div>")
    return "\n".join(r for r in result if r)


def _module_link(name: str) -> str:
    """生成模块链接。"""
    slug = name.replace(".", "/")
    return f'{slug}.html'


def generate_module_page(mod: ModuleInfo, all_modules: list) -> str:
    """生成单个模块的 HTML 页面。"""
    # 侧边栏
    sidebar_items = []
    for m_name in sorted(m.name for m in all_modules):
        active = "active" if m_name == mod.name else ""
        sidebar_items.append(
            f'<li class="{active}"><a href="{_module_link(m_name)}">{_esc(m_name)}</a></li>'
        )
    sidebar = "\n".join(sidebar_items)

    # 统计
    n_classes = len(mod.classes)
    n_funcs = len(mod.functions)
    n_methods = sum(len(c.methods) for c in mod.classes)
    n_no_doc_classes = sum(1 for c in mod.classes if not c.docstring)
    n_no_doc_funcs = sum(1 for f in mod.functions if not f.docstring)
    n_no_doc_methods = sum(1 for c in mod.classes for m in c.methods if not m.docstring)

    # 内容
    content_parts = []

    # 模块头部
    content_parts.append(f"""
    <div class="module-header">
        <h1>{_esc(mod.name)}</h1>
        <div class="module-stats">
            <span>📁 {n_classes} 类</span>
            <span>⚡ {n_funcs} 函数</span>
            <span>🔧 {n_methods} 方法</span>
            <span class="{'warn' if n_no_doc_classes + n_no_doc_funcs + n_no_doc_methods > 0 else 'ok'}">
                ⚠️ {n_no_doc_classes + n_no_doc_funcs + n_no_doc_methods} 处缺 docstring
            </span>
        </div>
        <div class="module-path">源码: {_esc(mod.filepath)}</div>
    </div>
    """)

    if mod.docstring:
        content_parts.append(f"""
        <div class="module-doc">
            <h2>模块说明</h2>
            {_format_docstring(mod.docstring)}
        </div>
        """)

    # 目录
    if mod.classes or mod.functions:
        content_parts.append('<div class="toc"><h2>目录</h2><ul>')
        for cls in mod.classes:
            content_parts.append(f'<li><a href="#class-{_esc(cls.name)}">class {_esc(cls.name)}</a></li>')
        for func in mod.functions:
            content_parts.append(f'<li><a href="#func-{_esc(func.name)}">{_esc(func.name)}()</a></li>')
        content_parts.append('</ul></div>')

    # 类
    for cls in mod.classes:
        bases_str = ""
        if cls.bases:
            bases_str = f"({', '.join(_esc(b) for b in cls.bases)})"
        decs_str = ""
        if cls.decorators:
            decs_str = "".join(f"<span class='decorator'>@{_esc(d)}</span>\n" for d in cls.decorators)

        methods_html = ""
        for m in cls.methods:
            badge = {
                "staticmethod": '<span class="badge static">static</span>',
                "classmethod": '<span class="badge cls">cls</span>',
                "method": "",
            }.get(m.kind, "")
            methods_html += f"""
            <div class="method" id="method-{_esc(cls.name)}-{_esc(m.name)}">
                <div class="method-header">
                    {badge}
                    <code>{_format_signature(m)}</code>
                    <span class="line-no">L{m.line_start}-{m.line_end}</span>
                </div>
                <div class="docstring">{_format_docstring(m.docstring)}</div>
            </div>
            """

        class_vars_html = ""
        if cls.class_vars:
            class_vars_html = '<div class="class-vars"><strong>类变量:</strong> ' + ", ".join(
                f'<code>{_esc(v)}</code>' for v in cls.class_vars
            ) + "</div>"

        content_parts.append(f"""
        <div class="class-block" id="class-{_esc(cls.name)}">
            <h2>
                {decs_str}
                class <span class="cls-name">{_esc(cls.name)}</span>{bases_str}
                <span class="line-no">L{cls.line_start}-{cls.line_end}</span>
            </h2>
            <div class="docstring">{_format_docstring(cls.docstring)}</div>
            {class_vars_html}
            <div class="methods">
                <h3>方法</h3>
                {methods_html if cls.methods else '<p class="no-doc">无公开方法</p>'}
            </div>
        </div>
        """)

    # 函数
    for func in mod.functions:
        decs_str = ""
        if func.decorators:
            decs_str = "".join(f"<span class='decorator'>@{_esc(d)}</span>\n" for d in func.decorators)

        content_parts.append(f"""
        <div class="func-block" id="func-{_esc(func.name)}">
            <h2>
                {decs_str}
                <code>{_format_signature(func)}</code>
                <span class="line-no">L{func.line_start}-{func.line_end}</span>
            </h2>
            <div class="docstring">{_format_docstring(func.docstring)}</div>
        </div>
        """)

    content = "\n".join(content_parts)

    return _render_html(
        title=f"{mod.name} - Fast3R API",
        sidebar=sidebar,
        content=content,
    )


def generate_index_page(all_modules: list) -> str:
    """生成首页。"""
    sidebar_items = []
    for m_name in sorted(m.name for m in all_modules):
        sidebar_items.append(
            f'<li><a href="{_module_link(m_name)}">{_esc(m_name)}</a></li>'
        )
    sidebar = "\n".join(sidebar_items)

    # 总览统计
    total_classes = sum(len(m.classes) for m in all_modules)
    total_funcs = sum(len(m.functions) for m in all_modules)
    total_methods = sum(len(c.methods) for m in all_modules for c in m.classes)
    no_doc = sum(
        1 for m in all_modules
        for f in m.functions if not f.docstring
    ) + sum(
        1 for m in all_modules
        for c in m.classes if not c.docstring
    ) + sum(
        1 for m in all_modules
        for c in m.classes for mt in c.methods if not mt.docstring
    )
    total_items = total_classes + total_funcs + total_methods
    coverage = ((total_items - no_doc) / total_items * 100) if total_items else 100

    # 模块表格
    rows = []
    for m in sorted(all_modules, key=lambda x: x.name):
        nc = len(m.classes)
        nf = len(m.functions)
        nm = sum(len(c.methods) for c in m.classes)
        nd = sum(1 for f in m.functions if not f.docstring) + \
             sum(1 for c in m.classes if not c.docstring) + \
             sum(1 for c in m.classes for mt in c.methods if not mt.docstring)
        status = "✅" if nd == 0 else f"⚠️ {nd} 处缺 docstring"
        rows.append(f"""
        <tr>
            <td><a href="{_module_link(m.name)}">{_esc(m.name)}</a></td>
            <td>{nc}</td>
            <td>{nf}</td>
            <td>{nm}</td>
            <td class="{'ok' if nd == 0 else 'warn'}">{status}</td>
        </tr>
        """)

    content = f"""
    <div class="index-header">
        <h1>🚀 Fast3R API 文档</h1>
        <div class="overview-stats">
            <div class="stat-card">
                <div class="stat-number">{len(all_modules)}</div>
                <div class="stat-label">模块</div>
            </div>
            <div class="stat-card">
                <div class="stat-number">{total_classes}</div>
                <div class="stat-label">类</div>
            </div>
            <div class="stat-card">
                <div class="stat-number">{total_funcs}</div>
                <div class="stat-label">函数</div>
            </div>
            <div class="stat-card">
                <div class="stat-number">{total_methods}</div>
                <div class="stat-label">方法</div>
            </div>
            <div class="stat-card">
                <div class="stat-number">{coverage:.0f}%</div>
                <div class="stat-label">Docstring 覆盖率</div>
            </div>
        </div>
    </div>

    <div class="module-table">
        <h2>模块一览</h2>
        <table>
            <thead>
                <tr><th>模块</th><th>类</th><th>函数</th><th>方法</th><th>Docstring</th></tr>
            </thead>
            <tbody>
                {"".join(rows)}
            </tbody>
        </table>
    </div>
    """

    return _render_html(
        title="Fast3R API 文档",
        sidebar=sidebar,
        content=content,
    )


# ═══════════════════════════ HTML 模板 ═══════════════════════════════

_CSS = """
:root {
    --bg: #1e1e2e;
    --sidebar-bg: #181825;
    --card-bg: #252540;
    --text: #cdd6f4;
    --text-dim: #7f849c;
    --accent: #89b4fa;
    --accent2: #a6e3a1;
    --warn: #f9e2af;
    --err: #f38ba8;
    --border: #45475a;
    --code-bg: #313244;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    font-family: 'Inter', -apple-system, sans-serif;
    background: var(--bg);
    color: var(--text);
    display: flex;
    min-height: 100vh;
}
.sidebar {
    width: 260px;
    background: var(--sidebar-bg);
    border-right: 1px solid var(--border);
    padding: 20px 0;
    position: fixed;
    top: 0;
    left: 0;
    bottom: 0;
    overflow-y: auto;
}
.sidebar h2 {
    padding: 0 16px 12px;
    font-size: 14px;
    color: var(--accent);
    text-transform: uppercase;
    letter-spacing: 1px;
}
.sidebar ul { list-style: none; }
.sidebar li a {
    display: block;
    padding: 6px 16px;
    color: var(--text-dim);
    text-decoration: none;
    font-size: 13px;
    font-family: 'JetBrains Mono', monospace;
}
.sidebar li a:hover { color: var(--accent); background: rgba(137,180,250,0.08); }
.sidebar li.active a { color: var(--accent); background: rgba(137,180,250,0.12); font-weight: 600; }
.main {
    margin-left: 260px;
    padding: 32px 40px;
    max-width: 960px;
    flex: 1;
}
h1 { font-size: 28px; margin-bottom: 16px; color: var(--text); }
h2 { font-size: 20px; margin: 24px 0 12px; color: var(--accent); border-bottom: 1px solid var(--border); padding-bottom: 8px; }
h3 { font-size: 16px; margin: 16px 0 8px; color: var(--text-dim); }
code, pre {
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    background: var(--code-bg);
    padding: 2px 6px;
    border-radius: 4px;
    font-size: 13px;
}
pre { padding: 12px; overflow-x: auto; }

/* 模块头部 */
.module-header { margin-bottom: 24px; }
.module-stats { display: flex; gap: 16px; margin: 8px 0; font-size: 14px; }
.module-stats span { padding: 4px 10px; background: var(--card-bg); border-radius: 6px; }
.module-path { font-size: 12px; color: var(--text-dim); }

/* 首页统计 */
.overview-stats { display: flex; gap: 16px; margin: 20px 0; flex-wrap: wrap; }
.stat-card {
    background: var(--card-bg);
    padding: 16px 24px;
    border-radius: 10px;
    text-align: center;
    min-width: 100px;
}
.stat-number { font-size: 28px; font-weight: 700; color: var(--accent); }
.stat-label { font-size: 12px; color: var(--text-dim); margin-top: 4px; }

/* 目录 */
.toc { background: var(--card-bg); padding: 16px 20px; border-radius: 8px; margin-bottom: 24px; }
.toc ul { columns: 2; list-style: none; }
.toc li { margin: 4px 0; }
.toc a { color: var(--accent); text-decoration: none; font-size: 13px; }
.toc a:hover { text-decoration: underline; }

/* 类块 */
.class-block { background: var(--card-bg); padding: 20px 24px; border-radius: 10px; margin-bottom: 20px; }
.class-block h2 { border-bottom: none; margin-top: 0; }
.cls-name { color: var(--accent2); }
.class-vars { margin: 8px 0; font-size: 13px; }

/* 方法 */
.methods { margin-top: 12px; }
.method {
    background: var(--code-bg);
    padding: 12px 16px;
    border-radius: 6px;
    margin: 8px 0;
    border-left: 3px solid var(--border);
}
.method-header { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.fn-name { color: var(--accent2); font-weight: 600; }
.arg-name { color: var(--text); }
.arg-type { color: var(--accent); }
.arg-default { color: var(--warn); }
.ret-type { color: var(--accent2); }
.line-no { font-size: 11px; color: var(--text-dim); margin-left: auto; }
.decorator { color: var(--warn); font-size: 12px; }

/* 函数块 */
.func-block {
    background: var(--card-bg);
    padding: 16px 20px;
    border-radius: 8px;
    margin-bottom: 16px;
    border-left: 3px solid var(--accent);
}

/* Docstring */
.docstring { margin-top: 8px; font-size: 13px; line-height: 1.6; }
.doc-section { margin: 8px 0 4px; }
.doc-detail { padding-left: 16px; border-left: 2px solid var(--border); margin: 4px 0; }
.no-doc { color: var(--err); font-style: italic; font-size: 13px; }
.warn { color: var(--warn); }
.ok { color: var(--accent2); }
.badge {
    font-size: 10px;
    padding: 2px 6px;
    border-radius: 3px;
    font-weight: 600;
    text-transform: uppercase;
}
.badge.static { background: #45475a; color: var(--accent); }
.badge.cls { background: #45475a; color: var(--accent2); }

/* 表格 */
table { width: 100%; border-collapse: collapse; margin-top: 12px; }
th { text-align: left; padding: 10px 12px; background: var(--code-bg); color: var(--accent); font-size: 13px; }
td { padding: 8px 12px; border-bottom: 1px solid var(--border); font-size: 13px; }
td a { color: var(--accent); text-decoration: none; }
td a:hover { text-decoration: underline; }

/* 滚动条 */
::-webkit-scrollbar { width: 6px; }
::-webkit-scrollbar-track { background: var(--bg); }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
"""

def _render_html(title, sidebar, content):
    """渲染 HTML 页面。"""
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>{_CSS}</style>
</head>
<body>
<nav class="sidebar">
    <h2>📦 Fast3R</h2>
    <ul>
        <li><a href="index.html">🏠 首页</a></li>
    </ul>
    <h2>Modules</h2>
    <ul>
    {sidebar}
    </ul>
</nav>
<main class="main">
{content}
</main>
</body>
</html>"""


# ═══════════════════════════ 主流程 ═══════════════════════════════
def main():
    print(f"🔍 扫描模块: {SOURCE_DIR}")
    module_list = collect_modules(SOURCE_DIR)
    print(f"📋 发现 {len(module_list)} 个模块\n")

    all_modules = []
    for filepath, mod_name in module_list:
        print(f"  解析: {mod_name}")
        mod = parse_file(filepath, mod_name)
        all_modules.append(mod)

    # 生成输出目录
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 为每个模块创建子目录
    for mod in all_modules:
        out_path = OUTPUT_DIR / _module_link(mod.name)
        out_path.parent.mkdir(parents=True, exist_ok=True)

    # 生成首页
    index_html = generate_index_page(all_modules)
    (OUTPUT_DIR / "index.html").write_text(index_html, encoding="utf-8")
    print(f"\n✅ 首页已生成: {OUTPUT_DIR / 'index.html'}")

    # 生成各模块页面
    for mod in all_modules:
        page_html = generate_module_page(mod, all_modules)
        out_path = OUTPUT_DIR / _module_link(mod.name)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(page_html, encoding="utf-8")
        print(f"  ✅ {mod.name}")

    # 打印统计
    total_classes = sum(len(m.classes) for m in all_modules)
    total_funcs = sum(len(m.functions) for m in all_modules)
    total_methods = sum(len(c.methods) for m in all_modules for c in m.classes)
    no_doc = sum(1 for m in all_modules for f in m.functions if not f.docstring) + \
             sum(1 for m in all_modules for c in m.classes if not c.docstring) + \
             sum(1 for m in all_modules for c in m.classes for mt in c.methods if not mt.docstring)
    total_items = total_classes + total_funcs + total_methods
    coverage = ((total_items - no_doc) / total_items * 100) if total_items else 100

    print(f"\n📊 统计:")
    print(f"   模块: {len(all_modules)}")
    print(f"   类: {total_classes}")
    print(f"   函数: {total_funcs}")
    print(f"   方法: {total_methods}")
    print(f"   Docstring 覆盖率: {coverage:.1f}%")
    print(f"\n📂 输出目录: {OUTPUT_DIR}")
    print(f"🌐 打开 {OUTPUT_DIR / 'index.html'} 查看文档")


if __name__ == "__main__":
    main()
