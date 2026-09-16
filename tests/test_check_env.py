"""tools/check_env.py 环境体检测试（T01 验收要点 ⑨，R11 防线）。

用 mock 目录自测：伪造"目录存在但无 __init__.py 且无 dist-info"的空壳包，
断言脚本能检出并阻止打包（exit code 1）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 把 tools/ 加入 sys.path 以便 import check_env
TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import check_env  # noqa: E402


class TestShellPackageDetection:
    """空壳包 / 残缺包检出。"""

    def test_detects_bare_directory(self, tmp_path: Path) -> None:
        """目录存在但无 __init__.py / 无 dist-info / 无文件 → 「完全空目录」。"""
        bogus = tmp_path / "bogus_pkg"
        bogus.mkdir()
        issues = check_env.detect_shell_packages(tmp_path)
        names = {pkg.name for pkg in issues}
        assert "bogus_pkg" in names
        entry = next(pkg for pkg in issues if pkg.name == "bogus_pkg")
        assert entry.kind == "完全空目录"
        assert entry.file_count == 0

    def test_detects_broken_package_with_files(self, tmp_path: Path) -> None:
        """目录内有文件但无 __init__.py / 无 dist-info → 「残缺包」。

        复现本次 openpyxl 损坏形态：目录在、部分文件在、关键文件丢。
        """
        broken = tmp_path / "openpyxl"
        broken.mkdir()
        (broken / "pivot").mkdir()
        # 残留一个普通 .py，但没有 __init__.py，也没有 dist-info
        (broken / "compat.py").write_text("x = 1\n", encoding="utf-8")
        (broken / "pivot" / "table.py").write_text("y = 2\n", encoding="utf-8")
        issues = check_env.detect_shell_packages(tmp_path)
        entry = next((p for p in issues if p.name == "openpyxl"), None)
        assert entry is not None, "残缺包必须被检出"
        assert entry.kind == "残缺包"
        assert entry.file_count >= 1

    def test_ignores_normal_package(self, tmp_path: Path) -> None:
        """有 __init__.py 的正常包不被误报。"""
        normal = tmp_path / "normal_pkg"
        normal.mkdir()
        (normal / "__init__.py").write_text("", encoding="utf-8")
        issues = check_env.detect_shell_packages(tmp_path)
        assert all(pkg.name != "normal_pkg" for pkg in issues)

    def test_ignores_package_with_dist_info(self, tmp_path: Path) -> None:
        """有 dist-info 元数据的命名空间包不被误报（即使含子目录与文件）。"""
        ns = tmp_path / "ns_pkg"
        ns.mkdir()
        (ns / "sub.py").write_text("", encoding="utf-8")
        (ns / "subpkg").mkdir()
        (tmp_path / "ns_pkg-1.0.dist-info").mkdir()
        issues = check_env.detect_shell_packages(tmp_path)
        assert all(pkg.name != "ns_pkg" for pkg in issues)

    def test_ignores_dot_libs(self, tmp_path: Path) -> None:
        """``*.libs`` 这类正常二进制目录不被误报（含其内文件）。"""
        libs = tmp_path / "numpy.libs"
        libs.mkdir()
        (libs / "libopenblas.so").write_text("binary", encoding="utf-8")
        issues = check_env.detect_shell_packages(tmp_path)
        assert all(pkg.name != "numpy.libs" for pkg in issues)

    def test_ignores_shapely_libs(self, tmp_path: Path) -> None:
        """``shapely.libs`` 同样不被误报（多 libs 场景回归）。"""
        libs = tmp_path / "shapely.libs"
        libs.mkdir()
        (libs / "libgeos.dll").write_text("binary", encoding="utf-8")
        issues = check_env.detect_shell_packages(tmp_path)
        assert all(pkg.name != "shapely.libs" for pkg in issues)

    def test_directory_with_py_but_no_init_is_broken(self, tmp_path: Path) -> None:
        """目录含 .py 却无 __init__.py/dist-info → 归为「残缺包」（不放行）。

        真实 site-packages 中的独立单文件模块是 ``foo.py``（文件），
        而非目录；因残留下 ``foo/core.py`` 却丢了 ``__init__.py`` 的目录
        正是残缺包，必须被检出。
        """
        pkg = tmp_path / "half_pkg"
        pkg.mkdir()
        (pkg / "mod.py").write_text("x = 1\n", encoding="utf-8")
        issues = check_env.detect_shell_packages(tmp_path)
        entry = next((p for p in issues if p.name == "half_pkg"), None)
        assert entry is not None
        assert entry.kind == "残缺包"

    def test_ignores_py_typed(self, tmp_path: Path) -> None:
        """含 py.typed 的类型存根包不被误报。"""
        stub = tmp_path / "stub_pkg"
        stub.mkdir()
        (stub / "py.typed").write_text("", encoding="utf-8")
        issues = check_env.detect_shell_packages(tmp_path)
        assert all(p.name != "stub_pkg" for p in issues)

    def test_nonexistent_dir_returns_empty(self, tmp_path: Path) -> None:
        assert check_env.detect_shell_packages(tmp_path / "nope") == []

    # ──────────────────────────────────────────────────────────────
    #  PEP 420 命名空间包（回归锁）
    #  背景：``google/`` 由 ``protobuf`` 发行版提供（内含 ``google/protobuf/``
    #  与 ``google/_upb/``），按设计**没有** ``__init__.py``，其 dist-info 名为
    #  ``protobuf-*`` 而非 ``google-*``。旧版判据只比「目录名 == 发行版名」，
    #  于是把健康的干净环境误报为「残缺包」→ ``build_exe.ps1`` **拒绝打包**。
    # ──────────────────────────────────────────────────────────────

    def test_ignores_namespace_package_owned_by_other_dist(self, tmp_path: Path) -> None:
        """``google/`` 由 ``protobuf-*.dist-info/RECORD`` 声明 → 不得误报。"""
        check_env._dist_owned_top_dirs_cached.cache_clear()

        # 命名空间包：无 __init__.py，内容在子目录
        (tmp_path / "google" / "protobuf").mkdir(parents=True)
        (tmp_path / "google" / "protobuf" / "__init__.py").write_text("", encoding="utf-8")
        (tmp_path / "google" / "_upb").mkdir(parents=True)
        (tmp_path / "google" / "_upb" / "_message.pyd").write_bytes(b"\x00")

        # 提供者发行版的 RECORD（注意：发行版名是 protobuf，不是 google）
        dist = tmp_path / "protobuf-7.36.1.dist-info"
        dist.mkdir()
        (dist / "RECORD").write_text(
            "google/protobuf/__init__.py,sha256=aaa,0\n"
            "google/_upb/_message.pyd,sha256=bbb,1\n",
            encoding="utf-8",
        )

        issues = check_env.detect_shell_packages(tmp_path)
        assert all(p.name != "google" for p in issues), (
            f"命名空间包被误报为损坏包：{[(p.name, p.kind) for p in issues]}"
        )

    def test_namespace_package_detected_when_provider_absent(self, tmp_path: Path) -> None:
        """反向对照：**没有**任何 RECORD 声明拥有该目录 → 仍须判为残缺包。

        这是 R11 原病例（``openpyxl`` 安装中断时 dist-info 一并缺失）的
        检出能力保障：第 5 步不得成为"放行一切无 __init__.py 目录"的后门。
        """
        check_env._dist_owned_top_dirs_cached.cache_clear()

        (tmp_path / "orphan").mkdir()
        (tmp_path / "orphan" / "sub").mkdir()
        (tmp_path / "orphan" / "sub" / "mod.py").write_text("x = 1\n", encoding="utf-8")

        # 另一个无关发行版的 RECORD，**未**声明拥有 orphan/
        dist = tmp_path / "unrelated-1.0.dist-info"
        dist.mkdir()
        (dist / "RECORD").write_text("unrelated/__init__.py,sha256=ccc,0\n", encoding="utf-8")

        issues = check_env.detect_shell_packages(tmp_path)
        entry = next((p for p in issues if p.name == "orphan"), None)
        assert entry is not None, "无发行版声明拥有的孤儿目录必须被检出"
        assert entry.kind == "残缺包"

    def test_egg_info_sources_txt_also_marks_ownership(self, tmp_path: Path) -> None:
        """``*.egg-info/SOURCES.txt``（非 CSV）同样可作为拥有关系依据。"""
        check_env._dist_owned_top_dirs_cached.cache_clear()

        (tmp_path / "legacy_ns").mkdir()
        (tmp_path / "legacy_ns" / "child").mkdir()
        (tmp_path / "legacy_ns" / "child" / "m.py").write_text("y = 2\n", encoding="utf-8")

        egg = tmp_path / "provider-2.0.egg-info"
        egg.mkdir()
        (egg / "SOURCES.txt").write_text(
            "legacy_ns/child/m.py\nprovider/__init__.py\n", encoding="utf-8"
        )
        (tmp_path / "provider").mkdir()
        (tmp_path / "provider" / "__init__.py").write_text("", encoding="utf-8")

        issues = check_env.detect_shell_packages(tmp_path)
        assert all(p.name != "legacy_ns" for p in issues)

    def test_multiple_shell_packages(self, tmp_path: Path) -> None:
        for name in ("shell_a", "shell_b", "shell_c"):
            (tmp_path / name).mkdir()
        issues = check_env.detect_shell_packages(tmp_path)
        assert {p.name for p in issues} == {"shell_a", "shell_b", "shell_c"}

    def test_self_test_mixed_env_assertion(self, tmp_path: Path) -> None:
        """自测锁定：空壳 + 残缺 同时命中，且 libs 目录放行（防回归）。"""
        # 完全空目录
        (tmp_path / "empty_pkg").mkdir()
        # 残缺包（有文件但缺 __init__.py/dist-info）
        broken = tmp_path / "broken_pkg"
        broken.mkdir()
        (broken / "core.py").write_text("a = 1\n", encoding="utf-8")
        # 正常 libs 目录（必须放行）
        libs = tmp_path / "numpy.libs"
        libs.mkdir()
        (libs / "x.so").write_text("bin", encoding="utf-8")
        # 正常包（必须放行）
        good = tmp_path / "good_pkg"
        good.mkdir()
        (good / "__init__.py").write_text("", encoding="utf-8")

        issues = check_env.detect_shell_packages(tmp_path)
        by_name = {pkg.name: pkg.kind for pkg in issues}
        assert by_name.get("empty_pkg") == "完全空目录"
        assert by_name.get("broken_pkg") == "残缺包"
        assert "numpy.libs" not in by_name
        assert "good_pkg" not in by_name


class TestExitCodes:
    """退出码语义（0 通过 / 1 致命 / 2 警告）。"""

    def test_fatal_when_shell_package_found(self, tmp_path: Path, capsys) -> None:
        (tmp_path / "rotten_pkg").mkdir()
        code = check_env.main(["--scan-dir", str(tmp_path), "--skip-imports"])
        assert code == 1
        out = capsys.readouterr().out
        assert "rotten_pkg" in out
        assert "禁止打包" in out

    def test_fatal_when_broken_package_found(self, tmp_path: Path, capsys) -> None:
        """残缺包（有文件但缺 __init__.py/dist-info）→ exit 1 且报告标注「残缺包」。"""
        broken = tmp_path / "openpyxl"
        broken.mkdir()
        (broken / "compat.py").write_text("x = 1\n", encoding="utf-8")
        code = check_env.main(["--scan-dir", str(tmp_path), "--skip-imports"])
        assert code == 1
        out = capsys.readouterr().out
        assert "openpyxl" in out
        assert "残缺包" in out
        assert "禁止打包" in out

    def test_libs_dir_does_not_fail(self, tmp_path: Path, capsys) -> None:
        """仅含 numpy.libs 的目录 → 不报错（exit 0）。"""
        libs = tmp_path / "numpy.libs"
        libs.mkdir()
        (libs / "lib.so").write_text("bin", encoding="utf-8")
        (tmp_path / "good_pkg").mkdir()
        (tmp_path / "good_pkg" / "__init__.py").write_text("", encoding="utf-8")
        code = check_env.main(["--scan-dir", str(tmp_path), "--skip-imports"])
        assert code == 0
        out = capsys.readouterr().out
        assert "numpy.libs" not in out

    def test_clean_fake_dir_passes(self, tmp_path: Path, capsys) -> None:
        """干净目录 + --skip-imports → 通过（exit 0）。"""
        (tmp_path / "good_pkg").mkdir()
        (tmp_path / "good_pkg" / "__init__.py").write_text("", encoding="utf-8")
        code = check_env.main(["--scan-dir", str(tmp_path), "--skip-imports"])
        assert code == 0

    def test_quiet_suppresses_output_on_pass(self, tmp_path: Path, capsys) -> None:
        """--quiet：通过时无输出（供 build_exe.ps1 调用）。"""
        (tmp_path / "good_pkg").mkdir()
        (tmp_path / "good_pkg" / "__init__.py").write_text("", encoding="utf-8")
        code = check_env.main(["--scan-dir", str(tmp_path), "--skip-imports", "--quiet"])
        assert code == 0
        assert capsys.readouterr().out.strip() == ""


class TestReportRendering:
    """报告渲染。"""

    def test_render_contains_sections(self) -> None:
        report = check_env.CheckReport(site_packages="/fake/site-packages")
        report.ok_versions.append("numpy 2.1.3 (图像数组)")
        text = report.render()
        assert "环境体检报告" in text
        assert "空壳包检测" in text
        assert "numpy 2.1.3" in text

    def test_render_shows_kind_and_source(self) -> None:
        """报告须区分「完全空目录」与「残缺包」并给出文件数。"""
        report = check_env.CheckReport(site_packages="/fake/site-packages")
        report.issues.append(
            check_env.ShellPackage(name="a", path="/a", kind="完全空目录", file_count=0)
        )
        report.issues.append(
            check_env.ShellPackage(name="b", path="/b", kind="残缺包", file_count=3)
        )
        text = report.render()
        assert "完全空目录 1" in text
        assert "残缺包 1" in text
        assert "[完全空目录]" in text
        assert "[残缺包]" in text
        assert "3 个文件" in text

    def test_fatal_property_true_on_issues(self) -> None:
        report = check_env.CheckReport()
        report.issues.append(check_env.ShellPackage(name="x", path="/x"))
        assert report.fatal is True

    def test_fatal_property_true_on_import_failure(self) -> None:
        report = check_env.CheckReport()
        report.import_failures.append(("numpy", "ImportError: boom"))
        assert report.fatal is True

    def test_fatal_false_when_clean(self) -> None:
        assert check_env.CheckReport().fatal is False


class TestVersionRange:
    """版本区间判定。"""

    @pytest.mark.parametrize(
        "version,lower,upper,expected",
        [
            ("2.1.3", "2.0", "3.0", True),
            ("1.9.0", "2.0", "3.0", False),
            ("3.0.0", "2.0", "3.0", False),
            ("6.9.3", "6.9", "6.10", True),
            ("6.8.0", "6.9", "6.10", False),
            ("1.4.0", "1.3", "", True),
            ("1.2.0", "1.3", "", False),
        ],
    )
    def test_ranges(self, version: str, lower: str, upper: str, expected: bool) -> None:
        ok, _note = check_env._version_in_range(version, lower, upper)
        assert ok is expected

    def test_empty_version_fails(self) -> None:
        ok, note = check_env._version_in_range("", "1.0", "2.0")
        assert ok is False
        assert "无法获取" in note


class TestCriticalDepsDeclared:
    """关键依赖清单与架构设计 13.6 一致。"""

    def test_ocr_uses_rapidocr_not_onnxruntime_suffix(self) -> None:
        """v1.2：OCR 封装库必须是 rapidocr（非 rapidocr_onnxruntime）。"""
        names = {pkg for pkg, *_ in check_env.CRITICAL_DEPS}
        assert "rapidocr" in names
        assert "rapidocr_onnxruntime" not in names

    def test_headless_opencv_used(self) -> None:
        names = {pkg for pkg, *_ in check_env.CRITICAL_DEPS}
        assert "opencv-python-headless" in names

    def test_pyside6_locked_range(self) -> None:
        entry = next(e for e in check_env.CRITICAL_DEPS if e[0] == "PySide6")
        assert entry[2] == "6.9"
        assert entry[3] == "6.10"


class TestR11HardeningCoverage:
    """R11 防线加固回归锁：曾被污染/曾漏检的包必须登记在案。

    历史（累计 7 次）：numpy / omegaconf / onnxruntime / colorlog /
    flatbuffers / openpyxl / charset_normalizer。
    这些包一旦再次残缺，check_env 必须能拦住。
    """

    def test_openpyxl_in_critical_deps(self) -> None:
        """openpyxl 必须进入 CRITICAL_DEPS（本次漏检根因）。"""
        names = {pkg for pkg, *_ in check_env.CRITICAL_DEPS}
        assert "openpyxl" in names

    def test_openpyxl_locked_range(self) -> None:
        entry = next(e for e in check_env.CRITICAL_DEPS if e[0] == "openpyxl")
        assert entry[1] == "openpyxl"  # import 名
        assert entry[2] == "3.1"
        assert entry[3] == "4.0"

    @pytest.mark.parametrize(
        "dist_name,import_name",
        [
            ("openpyxl", "openpyxl"),
            ("requests", "requests"),
            ("certifi", "certifi"),
            ("charset-normalizer", "charset_normalizer"),
            ("antlr4-python3-runtime", "antlr4"),
            ("flatbuffers", "flatbuffers"),
            ("six", "six"),
            ("tqdm", "tqdm"),
            ("colorlog", "colorlog"),
        ],
    )
    def test_transitive_deps_registered(self, dist_name: str, import_name: str) -> None:
        """rapidocr/onnxruntime 传递依赖必须全部登记。"""
        table = {pkg: imp for pkg, imp, *_ in check_env.CRITICAL_DEPS}
        assert dist_name in table
        assert table[dist_name] == import_name

    def test_deep_import_paths_present(self) -> None:
        """深路径子模块必须在 IMPORT_CHECKS 中（防"包在但子模块丢"）。"""
        checks = set(check_env.IMPORT_CHECKS)
        assert "openpyxl" in checks
        assert "openpyxl.workbook" in checks
        assert "openpyxl.pivot.table" in checks
        assert "charset_normalizer" in checks
        assert "rapidocr" in checks

    def test_import_checks_cover_all_critical_deps(self) -> None:
        """每个关键依赖的 import 名都必须在 IMPORT_CHECKS 里至少出现一次。"""
        checks = set(check_env.IMPORT_CHECKS)
        top_levels = {name.split(".")[0] for name in checks}
        for _dist, import_name, *_ in check_env.CRITICAL_DEPS:
            assert import_name in top_levels, f"{import_name} 未纳入 import 验证"


class TestFrozenRuntime:
    """打包态（PyInstaller）适配回归锁。

    实战背景（2026-09-16，exe 冒烟）：
      * onefile 会把依赖平铺解到 ``sys._MEIPASS``，该目录语义上等价 site-packages；
        若不优先识别，``find_site_packages`` 会顺着 ``sys.path`` 摸到**宿主机**的
        site-packages，体检结论与本次交付毫无关系。
    """

    def test_meipass_takes_priority(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """存在 ``sys._MEIPASS`` 时必须返回它，而不是宿主机 site-packages。"""
        fake_bundle = tmp_path / "_MEI123456"
        fake_bundle.mkdir()
        monkeypatch.setattr(sys, "_MEIPASS", str(fake_bundle), raising=False)
        assert check_env.find_site_packages() == fake_bundle

    def test_empty_meipass_is_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``_MEIPASS`` 指向不存在的目录时不得返回它（回退到常规探测）。"""
        monkeypatch.setattr(sys, "_MEIPASS", "/nonexistent/_MEI999", raising=False)
        assert check_env.find_site_packages() != Path("/nonexistent/_MEI999")

    def test_explicit_arg_still_wins(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """显式传入的目录优先级最高（测试注入伪造目录依赖此语义）。"""
        fake_bundle = tmp_path / "_MEI123456"
        fake_bundle.mkdir()
        monkeypatch.setattr(sys, "_MEIPASS", str(fake_bundle), raising=False)
        explicit = tmp_path / "explicit_sp"
        explicit.mkdir()
        assert check_env.find_site_packages(str(explicit)) == explicit


class TestFrozenScanNotApplicable:
    """打包态下第 3 节「空壳包检测」必须**明说不适用**，而不是误报。

    实战背景（2026-09-16，exe 冒烟第二轮）：``exe --check-env`` 修好脚本定位后，
    立刻把 exe 自己的 ``_MEIPASS`` 扫成 13 个"残缺包"（``PySide6`` / ``numpy`` /
    ``PIL`` / ``rules`` / ``ui`` …）并 exit 1 —— 全是误报。

    根因：PyInstaller 把纯 Python 模块编译进 **PYZ 归档**，``_MEIPASS`` 里只落地
    二进制扩展与数据文件，于是「有文件但无 ``__init__.py``」这条判据在打包态
    **根本不成立**。必须靠 ``sys.frozen`` 区分运行形态。
    """

    def test_reason_recorded_when_frozen(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        report = check_env.run_check(site_packages=None, skip_imports=True)
        assert report.scan_skipped_reason, "打包态必须记录跳过的原因"
        assert not report.fatal, "打包态跳过扫描不得被判为致命"
        assert "PYZ" in report.scan_skipped_reason or "打包态" in report.scan_skipped_reason

    def test_reason_absent_in_dev_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delattr(sys, "frozen", raising=False)
        report = check_env.run_check(site_packages=None, skip_imports=True)
        assert report.scan_skipped_reason == ""

    def test_explicit_scan_dir_is_still_honored(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """显式传目录时（测试注入伪造目录）不受打包态影响，仍真扫。"""
        (tmp_path / "bogus_pkg").mkdir()
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        report = check_env.run_check(str(tmp_path), skip_imports=True)
        assert report.scan_skipped_reason == ""
        assert [pkg.name for pkg in report.issues] == ["bogus_pkg"]

    def test_render_shows_skip_marker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        report = check_env.run_check(site_packages=None, skip_imports=True)
        text = report.render()
        assert "SKIP" in text
        assert "不适用" in text
        assert "打包态" in text


class TestFrozenVersionUnreadable:
    """打包态「版本不可读但包在」必须降级为 OK，不能报 WARN。

    实战背景（2026-09-16，exe 冒烟第三轮）：PyInstaller 不复制 dist-info，
    于是 ``importlib.metadata`` 取不到 ``antlr4-python3-runtime`` / ``colorlog``
    的版本 —— 但两个包都 import 成功。若照旧报 WARN，
    ``exe --check-env`` 会**永远**返回 exit 2。

    危害不在"多一行黄字"，而在于：现场工程师见它永远报警，就会学会忽略体检，
    R11 防线随之失效 —— 这比缺包本身更危险。
    """

    def test_degrades_to_ok_when_module_importable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(check_env, "is_frozen", lambda: True)
        monkeypatch.setattr(check_env, "_version_of", lambda *a, **kw: "")
        monkeypatch.setattr(check_env, "_module_importable", lambda name: True)

        report = check_env.CheckReport()
        check_env.check_versions(report)

        assert report.version_warnings == [], "打包态不得产生版本警告"
        assert len(report.ok_versions) == len(check_env.CRITICAL_DEPS)
        assert any("版本不可读" in item for item in report.ok_versions)
        assert not report.fatal

    def test_still_warns_when_module_missing_in_frozen(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """包真的不在时（import 失败）仍须报警 —— 降级不得掩盖真问题。"""
        monkeypatch.setattr(check_env, "is_frozen", lambda: True)
        monkeypatch.setattr(check_env, "_version_of", lambda *a, **kw: "")
        monkeypatch.setattr(check_env, "_module_importable", lambda name: False)

        report = check_env.CheckReport()
        check_env.check_versions(report)

        assert len(report.version_warnings) == len(check_env.CRITICAL_DEPS)

    def test_dev_mode_still_warns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """源码/开发态行为不变：取不到版本照旧 WARN（那里应当有 dist-info）。"""
        monkeypatch.setattr(check_env, "is_frozen", lambda: False)
        monkeypatch.setattr(check_env, "_version_of", lambda *a, **kw: "")

        report = check_env.CheckReport()
        check_env.check_versions(report)

        assert len(report.version_warnings) == len(check_env.CRITICAL_DEPS)
