"""infra.encoding 回归测试（T01 验收要点 ⑤）。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from infra.encoding import (
    file_sha256,
    force_utf8_encoding,
    normalize_path,
    normalize_path_str,
    read_json,
    read_text,
    safe_listdir,
    write_json,
    write_text,
)


class TestForceUtf8:
    """启动即强制 UTF-8（SOP 陷阱 #4/#5）。"""

    def test_sets_env_when_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PYTHONUTF8", raising=False)
        monkeypatch.delenv("PYTHONIOENCODING", raising=False)
        force_utf8_encoding()
        assert os.environ["PYTHONUTF8"] == "1"
        assert os.environ["PYTHONIOENCODING"] == "utf-8"

    def test_does_not_override_existing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """已设置的值不被覆盖（尊重用户显式配置）。"""
        monkeypatch.setenv("PYTHONUTF8", "0")
        force_utf8_encoding()
        assert os.environ["PYTHONUTF8"] == "0"


class TestNormalizePath:
    """路径规范化（Qt file:/// 前缀 + 盘符 + UNC）。"""

    def test_strips_file_uri_prefix(self) -> None:
        assert normalize_path("file:///D:/a/b.xlsx") == Path("D:/a/b.xlsx")

    def test_fixes_leading_slash_drive(self) -> None:
        assert normalize_path("/D:/a/b") == Path("D:/a/b")

    def test_strips_quotes_and_space(self) -> None:
        assert normalize_path('  "D:\\a b\\c.xlsx"  ') == Path("D:\\a b\\c.xlsx")

    def test_empty_returns_cwd(self) -> None:
        assert normalize_path("") == Path(".")

    def test_accepts_path_object(self) -> None:
        assert normalize_path(Path("D:/x")) == Path("D:/x")

    def test_unicode_path_preserved(self) -> None:
        """中文路径不被破坏。"""
        raw = r"\\172.20.99.220\制造中心\仓储物流部\成品科\2026年报关要素图片"
        assert str(normalize_path(raw)) == raw


class TestNormalizePathStr:
    """指纹比对用规范化字符串（架构设计 12.A.3）。"""

    def test_backslash_and_trailing_removed(self) -> None:
        assert normalize_path_str("D:/a/b/") == "d:\\a\\b"

    def test_drive_letter_lowercased(self) -> None:
        assert normalize_path_str("D:\\X") == "d:\\X"

    def test_unc_preserved_without_lowercase(self) -> None:
        result = normalize_path_str(r"\\host\share\Ticket")
        assert result == r"\\host\share\Ticket"

    def test_two_forms_equal(self) -> None:
        """正斜杠 / 反斜杠 / 尾斜杠 三种写法规范化后相等。"""
        a = normalize_path_str(r"D:\a\b")
        b = normalize_path_str("D:/a/b/")
        assert a == b


class TestTextIO:
    """文本读写（显式编码 + 自动建目录 + BOM）。"""

    def test_write_then_read(self, tmp_path: Path) -> None:
        target = tmp_path / "深" / "目录" / "a.txt"
        write_text(target, "你好\n世界", encoding="utf-8")
        assert read_text(target, encoding="utf-8") == "你好\n世界"

    def test_write_utf8_sig_has_bom(self, tmp_path: Path) -> None:
        """CSV 用 utf-8-sig 防 Excel 乱码。"""
        target = tmp_path / "a.csv"
        write_text(target, "出货通知书号,料号\nSA1,P1\n", encoding="utf-8-sig")
        raw = target.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf")

    def test_read_missing_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            read_text(tmp_path / "nope.txt")


class TestJsonIO:
    """JSON 读写。"""

    def test_roundtrip_chinese_not_escaped(self, tmp_path: Path) -> None:
        target = tmp_path / "cfg.json"
        write_json(target, {"票号": "SA26090215", "count": 18})
        text = target.read_text(encoding="utf-8")
        assert "SA26090215" in text
        assert "\\u" not in text  # 中文未被转义
        assert read_json(target)["count"] == 18

    def test_default_on_missing(self, tmp_path: Path) -> None:
        assert read_json(tmp_path / "nope.json", default={}) == {}

    def test_raises_on_missing_without_default(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            read_json(tmp_path / "nope.json")


class TestSha256:
    """流式 sha256（断点指纹，UNC 安全）。"""

    def test_hash_stable_and_differs(self, tmp_path: Path) -> None:
        a = tmp_path / "a.bin"
        b = tmp_path / "b.bin"
        a.write_bytes(b"hello")
        b.write_bytes(b"world")
        assert file_sha256(a) == file_sha256(a)
        assert file_sha256(a) != file_sha256(b)

    def test_missing_returns_empty(self, tmp_path: Path) -> None:
        """读取失败返回空串（调用方回退路径比对）。"""
        assert file_sha256(tmp_path / "nope.bin") == ""


class TestSafeListdir:
    """安全列目录。"""

    def test_lists_entries(self, tmp_path: Path) -> None:
        (tmp_path / "x.txt").write_text("a", encoding="utf-8")
        assert "x.txt" in safe_listdir(tmp_path)

    def test_missing_returns_empty(self, tmp_path: Path) -> None:
        assert safe_listdir(tmp_path / "nope") == []
