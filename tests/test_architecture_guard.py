"""分层架构守卫测试（T01 验收要点 ⑩ + 架构设计 9.6）。

**核心断言**：
  1. ``core/`` 与 ``app/`` 目录下**任何 .py** 的 import 语句中，
     **不得出现 ``PySide6`` / ``PyQt`` / ``PyQt5`` / ``PyQt6`` / ``PySide2``**；
  2. 依赖方向单向：``infra`` 不 import 上层；``core`` 只 import ``infra`` + ``core``；
     ``app`` 只 import ``infra`` + ``core`` + ``app``。

这条保证**引擎可脱离 GUI 独立跑（CLI / CI）** —— 即核心解耦目标可被机器验证。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: 禁止在 core/ 与 app/ 出现的 GUI 框架模块前缀
FORBIDDEN_GUI_PREFIXES: tuple[str, ...] = (
    "PySide6",
    "PySide2",
    "PyQt",
    "PyQt5",
    "PyQt6",
    "QApplication",
)

#: 各层的允许依赖前缀
LAYER_ALLOWED: dict[str, tuple[str, ...]] = {
    "infra": ("infra",),
    "core": ("core", "infra"),
    "app": ("app", "core", "infra"),
    "ui": ("ui", "app", "core", "infra"),
}

#: 标准库 / 第三方库白名单（这些不算"层依赖"，直接放行）
_EXTERNAL_OK: tuple[str, ...] = (
    "__future__",
    "abc",
    "argparse",
    "ast",
    "collections",
    "contextlib",
    "csv",
    "dataclasses",
    "datetime",
    "enum",
    "functools",
    "hashlib",
    "importlib",
    "io",
    "json",
    "logging",
    "os",
    "pathlib",
    "re",
    "shutil",
    "stat",
    "string",
    "sys",
    "tempfile",
    "threading",
    "time",
    "types",
    "typing",
    "unicodedata",
    "uuid",
    "warnings",
    "concurrent",
    "itertools",
    "math",
    # 第三方（infra 层边界内的库，允许 core/ocr_engine 直接调用 rapidocr）
    "yaml",
    "rapidocr",
    "cv2",
    "numpy",
    "PIL",
    "openpyxl",
    "omegaconf",
    "pyclipper",
    "shapely",
)


def _iter_py_files(layer_dir: Path) -> list[Path]:
    """递归列出层目录下的全部 .py 文件。"""
    if not layer_dir.is_dir():
        return []
    return sorted(layer_dir.rglob("*.py"))


def _extract_imports(py_file: Path) -> list[tuple[str, int]]:
    """用 AST 提取文件的全部 import 模块名（顶层名）。

    Args:
        py_file: Python 文件路径。

    Returns:
        ``[(顶层模块名, 行号), ...]``；语法错误时返回空列表。
    """
    try:
        source = py_file.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(py_file))
    except (SyntaxError, UnicodeDecodeError):
        return []

    modules: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.append((alias.name.split(".")[0], node.lineno))
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                # 相对 import（如 from . import x）—— 同层，跳过
                continue
            if node.module:
                modules.append((node.module.split(".")[0], node.lineno))
    return modules


def _layer_of(module_name: str) -> str | None:
    """判断顶层模块名属于哪一层。"""
    for layer in LAYER_ALLOWED:
        if module_name == layer:
            return layer
    return None


class TestNoGuiImports:
    """core/ 与 app/ 不得 import 任何 GUI 框架（架构设计 9.6）。"""

    @pytest.mark.architecture
    @pytest.mark.parametrize("layer", ["core", "app"])
    def test_no_gui_imports(self, layer: str) -> None:
        layer_dir = PROJECT_ROOT / layer
        violations: list[str] = []

        for py_file in _iter_py_files(layer_dir):
            for module_name, lineno in _extract_imports(py_file):
                for prefix in FORBIDDEN_GUI_PREFIXES:
                    if module_name == prefix or module_name.startswith(prefix):
                        rel = py_file.relative_to(PROJECT_ROOT)
                        violations.append(f"{rel}:{lineno} imports {module_name}")

        assert not violations, (
            f"{layer}/ 目录出现 GUI 依赖（违反分层约束，引擎将无法脱离 GUI 运行）：\n"
            + "\n".join(violations)
        )

    @pytest.mark.architecture
    def test_no_gui_string_in_source_headers(self) -> None:
        """兜底：源码中不得出现 ``import PySide6`` 之类的字符串写法。

        覆盖 AST 无法解析的场景（如条件 import / 字符串注入）。
        """
        pattern = re.compile(r"^\s*(?:from|import)\s+(PySide6|PyQt5?|PySide2)", re.MULTILINE)
        violations: list[str] = []
        for layer in ("core", "app"):
            for py_file in _iter_py_files(PROJECT_ROOT / layer):
                text = py_file.read_text(encoding="utf-8", errors="ignore")
                if pattern.search(text):
                    violations.append(str(py_file.relative_to(PROJECT_ROOT)))
        assert not violations, f"发现 GUI import 字符串：{violations}"


class TestLayerDirection:
    """依赖方向单向（infra ← core ← app ← ui）。"""

    @pytest.mark.architecture
    def test_infra_does_not_import_upper_layers(self) -> None:
        """infra/ 不得 import core / app / ui。"""
        violations: list[str] = []
        for py_file in _iter_py_files(PROJECT_ROOT / "infra"):
            for module_name, lineno in _extract_imports(py_file):
                if module_name in ("core", "app", "ui"):
                    rel = py_file.relative_to(PROJECT_ROOT)
                    violations.append(f"{rel}:{lineno} imports {module_name}")
        assert not violations, f"infra/ 反向依赖上层：{violations}"

    @pytest.mark.architecture
    def test_core_does_not_import_app_or_ui(self) -> None:
        """core/ 不得 import app / ui。"""
        violations: list[str] = []
        for py_file in _iter_py_files(PROJECT_ROOT / "core"):
            for module_name, lineno in _extract_imports(py_file):
                if module_name in ("app", "ui"):
                    rel = py_file.relative_to(PROJECT_ROOT)
                    violations.append(f"{rel}:{lineno} imports {module_name}")
        assert not violations, f"core/ 反向依赖上层：{violations}"

    @pytest.mark.architecture
    def test_app_does_not_import_ui(self) -> None:
        """app/ 不得 import ui。"""
        violations: list[str] = []
        for py_file in _iter_py_files(PROJECT_ROOT / "app"):
            for module_name, lineno in _extract_imports(py_file):
                if module_name == "ui":
                    rel = py_file.relative_to(PROJECT_ROOT)
                    violations.append(f"{rel}:{lineno} imports {module_name}")
        assert not violations, f"app/ 反向依赖 UI：{violations}"

    @pytest.mark.architecture
    def test_third_party_only_in_allowed_layers(self) -> None:
        """第三方重库（openpyxl/cv2/PIL）原则上只在 infra 出现。

        例外：``core/ocr_engine.py`` 必须直接调用 rapidocr（架构设计 13.3）。
        """
        heavy_third_party = {"openpyxl", "cv2", "PIL", "PySide6"}
        allowed_exceptions = {
            "core/ocr_engine.py": {"cv2", "PIL", "rapidocr"},
            "core/result_exporter.py": {"openpyxl"},
            "core/excel_probe.py": {"openpyxl"},
        }

        violations: list[str] = []
        for layer in ("core", "app"):
            for py_file in _iter_py_files(PROJECT_ROOT / layer):
                rel = str(py_file.relative_to(PROJECT_ROOT)).replace("\\", "/")
                permitted = allowed_exceptions.get(rel, set())
                for module_name, lineno in _extract_imports(py_file):
                    if module_name in heavy_third_party and module_name not in permitted:
                        violations.append(f"{rel}:{lineno} imports {module_name}")
        assert not violations, f"第三方库越层使用：{violations}"

    def test_no_wildcard_imports(self) -> None:
        """禁止 ``from x import *``（可读性 + 命名冲突）。"""
        violations: list[str] = []
        for layer in ("infra", "core", "app"):
            for py_file in _iter_py_files(PROJECT_ROOT / layer):
                text = py_file.read_text(encoding="utf-8", errors="ignore")
                if re.search(r"^\s*from\s+[\w\.]+\s+import\s+\*", text, re.MULTILINE):
                    violations.append(str(py_file.relative_to(PROJECT_ROOT)))
        assert not violations, f"发现通配符 import：{violations}"


class TestProjectStructure:
    """T01 文件清单完整性。"""

    @pytest.mark.architecture
    def test_root_files_exist(self) -> None:
        for name in (
            "pyproject.toml",
            "requirements.txt",
            "requirements-dev.txt",
            "main.py",
            "customs_checker.spec",
            "build_exe.ps1",
            "README.md",
        ):
            assert (PROJECT_ROOT / name).is_file(), f"缺少根文件 {name}"

    @pytest.mark.architecture
    def test_infra_modules_exist(self) -> None:
        for name in (
            "__init__.py",
            "config.py",
            "encoding.py",
            "logger.py",
            "errors.py",
            "fs_lock.py",
            "resources.py",
        ):
            assert (PROJECT_ROOT / "infra" / name).is_file(), f"缺少 infra/{name}"

    @pytest.mark.architecture
    def test_core_t01_modules_exist(self) -> None:
        for name in ("__init__.py", "models.py", "constants.py", "rule_repository.py"):
            assert (PROJECT_ROOT / "core" / name).is_file(), f"缺少 core/{name}"

    @pytest.mark.architecture
    def test_rules_yaml_exist(self) -> None:
        for name in (
            "fields_blacklist.yaml",
            "brand_patterns.yaml",
            "model_clean_rules.yaml",
            "whole_machine_brand.yaml",
            "separators.yaml",
            "noise_signals.yaml",
        ):
            assert (PROJECT_ROOT / "rules" / name).is_file(), f"缺少 rules/{name}"

    @pytest.mark.architecture
    def test_tools_check_env_exists(self) -> None:
        assert (PROJECT_ROOT / "tools" / "check_env.py").is_file()

    @pytest.mark.architecture
    def test_tests_files_exist(self) -> None:
        for name in ("__init__.py", "conftest.py", "test_rule_repository.py"):
            assert (PROJECT_ROOT / "tests" / name).is_file(), f"缺少 tests/{name}"
