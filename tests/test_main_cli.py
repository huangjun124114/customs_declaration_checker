"""``main.py`` CLI 入口回归锁（隐藏参数 ``--self-test`` / ``--check-env``）。

实战背景（2026-09-16，exe 冒烟）：
  这两个参数是**现场排错的主路径**，但此前没有任何测试覆盖，于是三个缺陷
  全部只在打好 exe 之后才暴露：

  1. ``--self-test`` 打印 ``✅`` 时抛 ``UnicodeEncodeError``（流未重配为 UTF-8），
     断言全过却返回退出码 1 —— 误导排错方向最严重的一个；
  2. ``--check-env`` 用 ``__file__.parent / "tools"`` 找脚本，onefile 打包态下
     ``tools/`` 不在 ``__file__`` 旁边 → ``ImportError``；
  3. 窗口模式（``console=False``）下未捕获异常会弹**模态错误框**把进程挂死，
     现场表现为"命令敲下去没反应"，连报错都看不到。

本模块锁定「脚本定位」与「失败必须可读」两条。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import main as main_module
from infra import resources

#: 伪造的 check_env 模块：只提供 _run_env_check 真正用到的接口
_FAKE_CHECK_ENV = '''\
"""测试替身。"""


class _Report:
    fatal = False
    version_warnings = []

    def render(self) -> str:
        return "FAKE-CHECK-ENV-REPORT"


def run_check():
    return _Report()
'''


@pytest.fixture(autouse=True)
def _purge_check_env_module():
    """``_run_env_check`` 用 ``import check_env``，会污染 sys.modules 缓存。"""
    sys.modules.pop("check_env", None)
    yield
    sys.modules.pop("check_env", None)


class TestRunEnvCheckResolution:
    """``--check-env`` 的 tools/ 目录三级查找。"""

    def test_finds_tools_under_bundle_root(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """onefile 打包态：``tools/check_env.py`` 随包解到 ``bundle_root()``。"""
        bundle = tmp_path / "_MEI999999"
        (bundle / "tools").mkdir(parents=True)
        (bundle / "tools" / "check_env.py").write_text(_FAKE_CHECK_ENV, encoding="utf-8")

        monkeypatch.setattr(resources, "bundle_root", lambda: bundle)
        monkeypatch.setattr(resources, "app_base_dir", lambda: tmp_path / "nowhere")

        assert main_module._run_env_check() == 0
        assert "FAKE-CHECK-ENV-REPORT" in capsys.readouterr().out

    def test_finds_tools_beside_executable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """目录形式 / 现场手放：``tools/`` 与 exe 同目录（``app_base_dir()``）。"""
        app_dir = tmp_path / "app"
        (app_dir / "tools").mkdir(parents=True)
        (app_dir / "tools" / "check_env.py").write_text(_FAKE_CHECK_ENV, encoding="utf-8")

        monkeypatch.setattr(resources, "bundle_root", lambda: tmp_path / "nowhere")
        monkeypatch.setattr(resources, "app_base_dir", lambda: app_dir)

        assert main_module._run_env_check() == 0
        assert "FAKE-CHECK-ENV-REPORT" in capsys.readouterr().out

    def test_reports_readably_when_tools_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """找不到脚本时必须**可读地失败**，而不是抛异常（窗口模式下会挂死人）。"""
        monkeypatch.setattr(resources, "bundle_root", lambda: tmp_path / "nope1")
        monkeypatch.setattr(resources, "app_base_dir", lambda: tmp_path / "nope2")
        monkeypatch.setattr(main_module, "__file__", str(tmp_path / "elsewhere" / "main.py"))

        assert main_module._run_env_check() == 1
        out = capsys.readouterr().out
        assert "未找到 tools/check_env.py" in out
        # 必须列出尝试过的路径，便于现场判断该把文件放哪里
        assert "nope1" in out and "nope2" in out
