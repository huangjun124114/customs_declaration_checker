"""infra.fs_lock 只读红线守卫测试（T01 验收要点 ⑥）。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from infra.errors import OutputPathViolation, PathNotAccessibleError
from infra.fs_lock import assert_readonly, assert_within, input_readonly


class TestAssertReadonly:
    """写目标不得落在受保护输入根内（铁律一）。"""

    def test_write_inside_protected_root_raises(self, tmp_path: Path) -> None:
        protected = tmp_path / "原始输入"
        protected.mkdir()
        target = protected / "out.xlsx"
        with pytest.raises(OutputPathViolation):
            assert_readonly(target, [protected])

    def test_write_outside_protected_root_ok(self, tmp_path: Path) -> None:
        protected = tmp_path / "原始输入"
        protected.mkdir()
        allowed = tmp_path / "成果产出"
        allowed.mkdir()
        # 不抛异常即通过
        assert_readonly(allowed / "out.xlsx", [protected])

    def test_nested_subdir_inside_protected_raises(self, tmp_path: Path) -> None:
        """受保护根的子目录同样受保护。"""
        protected = tmp_path / "原始输入"
        nested = protected / "子" / "更深的"
        nested.mkdir(parents=True)
        with pytest.raises(OutputPathViolation):
            assert_readonly(nested / "x.json", [protected])

    def test_empty_protected_root_ignored(self, tmp_path: Path) -> None:
        assert_readonly(tmp_path / "x.txt", ["", "   "])

    def test_exception_carries_user_message(self, tmp_path: Path) -> None:
        protected = tmp_path / "in"
        protected.mkdir()
        with pytest.raises(OutputPathViolation) as info:
            assert_readonly(protected / "x", [protected])
        assert "铁律" in info.value.user_message


class TestAssertWithin:
    """写目标必须落在允许的输出根内（产物分流）。"""

    def test_inside_allowed_ok(self, tmp_path: Path) -> None:
        root = tmp_path / "成果产出"
        (root / "子目录").mkdir(parents=True)
        assert_within(root / "子目录" / "x.xlsx", root)

    def test_outside_allowed_raises(self, tmp_path: Path) -> None:
        root = tmp_path / "成果产出"
        root.mkdir()
        other = tmp_path / "过程产出"
        other.mkdir()
        with pytest.raises(OutputPathViolation):
            assert_within(other / "x.xlsx", root)


class TestInputReadonly:
    """只读访问上下文管理器。"""

    def test_yields_normalized_path(self, tmp_path: Path) -> None:
        src = tmp_path / "a.xlsx"
        src.write_text("data", encoding="utf-8")
        with input_readonly(src) as path:
            assert isinstance(path, Path)
            assert path.read_text(encoding="utf-8") == "data"

    def test_missing_input_raises_path_error(self, tmp_path: Path) -> None:
        with pytest.raises(PathNotAccessibleError):
            with input_readonly(tmp_path / "nope.xlsx"):
                pass  # pragma: no cover

    def test_expect_exists_false_allows_missing(self, tmp_path: Path) -> None:
        with input_readonly(tmp_path / "nope.xlsx", expect_exists=False) as path:
            assert not path.exists()

    def test_modification_during_read_raises(self, tmp_path: Path) -> None:
        """读期间输入被改动 → 抛红线异常（严格守卫）。"""
        src = tmp_path / "a.txt"
        src.write_text("v1", encoding="utf-8")
        with pytest.raises(OutputPathViolation):
            with input_readonly(src):
                src.write_text("v2-much-longer", encoding="utf-8")

    def test_no_modification_no_raise(self, tmp_path: Path) -> None:
        src = tmp_path / "a.txt"
        src.write_text("v1", encoding="utf-8")
        with input_readonly(src) as path:
            path.read_text(encoding="utf-8")
        # 未抛异常即通过

    def test_directory_input_supported(self, tmp_path: Path) -> None:
        folder = tmp_path / "图"
        folder.mkdir()
        (folder / "a.jpg").write_bytes(b"xx")
        with input_readonly(folder) as path:
            assert path.is_dir()

    @pytest.mark.skipif(os.name != "nt", reason="ReadOnly 属性仅 Windows")
    def test_readonly_file_detected(self, tmp_path: Path) -> None:
        from infra.fs_lock import is_readonly_file

        target = tmp_path / "ro.txt"
        target.write_text("x", encoding="utf-8")
        assert is_readonly_file(target) is False
