"""Unit tests for the unified `pea` CLI, `pea.assets`, `pea.demo`, and config decoupling."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import pea
from pea import cli
from pea.config import PeaConfig, default_cache_dir
from pea.viewer import app as viewer_app

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_pea_cli_version_matches_package(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--version"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert pea.__version__ in captured.out


def test_pea_setup_check_passes_for_repo_assets_and_fails_for_empty_dir(tmp_path: Path) -> None:
    local_assets = REPO_ROOT / "local_assets"
    assert local_assets.is_dir(), "Expected repo local_assets directory"

    rc_ok = cli.main(
        ["setup", "--check", "--skip-browser", "--assets-dir", str(local_assets)]
    )
    assert rc_ok == 0

    empty_dir = tmp_path / "empty_assets"
    empty_dir.mkdir()
    rc_bad = cli.main(
        ["setup", "--check", "--skip-browser", "--assets-dir", str(empty_dir)]
    )
    assert rc_bad == 1


def test_fetch_assets_script_check_compatibility() -> None:
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "fetch_assets.py"), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"fetch_assets.py --check failed: {proc.stderr}"
    assert "katex, ocr, ruffle, fonts" in proc.stdout


def test_config_decouples_assets_to_user_cache_outside_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 1. Inside repo checkout -> <repo>/local_assets
    repo_cfg = PeaConfig.from_env({})
    assert repo_cfg.local_assets_dir == (REPO_ROOT / "local_assets").resolve()

    # 2. Outside repo checkout -> default_cache_dir() / "local_assets"
    fake_cwd = tmp_path / "external_workdir"
    fake_cwd.mkdir()
    fake_cache = tmp_path / "user_cache"

    monkeypatch.setattr("pea.config._discover_project_root", lambda: fake_cwd)
    monkeypatch.setattr("pea.config.default_cache_dir", lambda env=None: fake_cache)

    uvx_cfg = PeaConfig.from_env({})
    assert uvx_cfg.project_root == fake_cwd
    assert uvx_cfg.local_assets_dir == fake_cache / "local_assets"

    # 3. Explicit PEA_LOCAL_ASSETS_DIR always takes precedence
    explicit_assets = tmp_path / "custom_assets"
    override_cfg = PeaConfig.from_env({"PEA_LOCAL_ASSETS_DIR": str(explicit_assets)})
    assert override_cfg.local_assets_dir == explicit_assets.resolve()

    # Verify default_cache_dir honors XDG_CACHE_HOME cross-platform
    xdg_cache = default_cache_dir({"XDG_CACHE_HOME": str(tmp_path / "xdg")})
    assert xdg_cache == (tmp_path / "xdg").expanduser() / "pea"


def test_demo_assets_fixture_matches_test_fixture_verbatim() -> None:
    pkg_fixture = REPO_ROOT / "src" / "pea" / "demo_assets" / "html5_rc_sim" / "index.html"
    test_fixture = REPO_ROOT / "tests" / "fixtures" / "html5_rc_sim" / "index.html"
    assert pkg_fixture.is_file()
    assert test_fixture.is_file()
    assert pkg_fixture.read_bytes() == test_fixture.read_bytes()


def test_pea_demo_preflight_reports_helpful_setup_errors(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Missing assets dir -> clean exit code 1 mentioning `pea setup`
    monkeypatch.setenv("PEA_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("PEA_LOCAL_ASSETS_DIR", str(tmp_path / "missing_assets"))
    rc_missing_assets = cli.main(["demo", "--out", str(tmp_path / "out1"), "--no-viewer"])
    assert rc_missing_assets == 1
    err_assets = capsys.readouterr().err
    assert "pea setup" in err_assets

    # Valid assets, missing browser -> clean exit code 1 mentioning `pea setup`
    monkeypatch.setenv("PEA_LOCAL_ASSETS_DIR", str(REPO_ROOT / "local_assets"))
    monkeypatch.setattr("pea.demo.resolve_browser_executable", lambda: None)
    monkeypatch.setattr("pea.config.resolve_browser_executable", lambda env=None: None)
    rc_missing_browser = cli.main(["demo", "--out", str(tmp_path / "out2"), "--no-viewer"])
    assert rc_missing_browser == 1
    err_browser = capsys.readouterr().err
    assert "pea setup" in err_browser


def test_viewer_runs_dir_cli_option_and_loopback_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    custom_runs = tmp_path / "my_runs"
    custom_runs.mkdir()

    # Non-loopback host still rejected even with --runs-dir
    with pytest.raises(SystemExit) as bad_host_exc:
        viewer_app.main(["--host", "0.0.0.0", "--runs-dir", str(custom_runs)])
    assert bad_host_exc.value.code == 2

    captured: dict[str, Any] = {}

    def _fake_create_viewer_app(cfg: PeaConfig) -> object:
        captured["cfg"] = cfg
        return object()

    def _fake_uvicorn_run(app: object, *, host: str, port: int) -> None:
        captured["host"] = host
        captured["port"] = port

    monkeypatch.setattr(viewer_app, "create_viewer_app", _fake_create_viewer_app)
    monkeypatch.setattr("uvicorn.run", _fake_uvicorn_run)

    rc = viewer_app.main(["--host", "127.0.0.1", "--port", "5199", "--runs-dir", str(custom_runs)])
    assert rc == 0
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 5199
    assert captured["cfg"].runs_dir == custom_runs.resolve()
