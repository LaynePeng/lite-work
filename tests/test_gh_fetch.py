# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""gh_fetch 单元测试：阈值回退（TooManyFilesError）与判定纯函数。

不访问网络：httpx.Client 与 _api_get 全部 monkeypatch。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from litework.tools import gh_fetch  # noqa: E402


# ---------------------------------------------------------------- 纯函数

def test_should_use_resumable_boundaries():
    assert gh_fetch.should_use_resumable(1000, 1000) is True   # 恰好等于阈值 → 允许
    assert gh_fetch.should_use_resumable(1001, 1000) is False  # 超过 → 回退
    assert gh_fetch.should_use_resumable(0, 1000) is True
    assert gh_fetch.should_use_resumable(5, 4) is False


def test_resumable_max_files_env_override(monkeypatch):
    assert gh_fetch._resumable_max_files() == gh_fetch.DEFAULT_MAX_FILES
    monkeypatch.setenv("LITEWORK_RESUMABLE_MAX_FILES", "7")
    assert gh_fetch._resumable_max_files() == 7
    monkeypatch.setenv("LITEWORK_RESUMABLE_MAX_FILES", "not-a-number")
    assert gh_fetch._resumable_max_files() == gh_fetch.DEFAULT_MAX_FILES
    monkeypatch.setenv("LITEWORK_RESUMABLE_MAX_FILES", "-3")
    assert gh_fetch._resumable_max_files() == gh_fetch.DEFAULT_MAX_FILES


# ---------------------------------------------------------------- fetch_subpath（mock API）

class _FakeClient:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _make_tree(subpath: str, n: int):
    return {
        "truncated": False,
        "tree": [
            {"type": "blob", "path": f"{subpath}/f{i:05d}.txt", "size": 10, "mode": "100644"}
            for i in range(n)
        ],
    }


def _patch_api(monkeypatch, tree):
    def fake_api_get(client, url, token):
        if "/commits/" in url:
            return {"sha": "abcdef1234567890"}
        return tree

    monkeypatch.setattr(gh_fetch, "_api_get", fake_api_get)


def test_fetch_subpath_raises_too_many_files(monkeypatch, tmp_path):
    import httpx

    monkeypatch.setattr(httpx, "Client", _FakeClient)
    _patch_api(monkeypatch, _make_tree("skills/big", 5))
    with pytest.raises(gh_fetch.TooManyFilesError) as ei:
        gh_fetch.fetch_subpath(str(tmp_path), "o", "r", "main", "skills/big", max_files=4)
    assert ei.value.count == 5
    assert ei.value.max_files == 4
    # 阈值内不抛（进入下载阶段由 _download_one mock 兜住）
    calls = []

    def fake_download(owner, repo, commit, path, dest, expected_size, retries, on_bytes=None):
        calls.append(path)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8") as f:
            f.write("x")

    monkeypatch.setattr(gh_fetch, "_download_one", fake_download)
    result = gh_fetch.fetch_subpath(str(tmp_path), "o", "r", "main", "skills/big", max_files=10)
    assert result.endswith("skills/big")
    assert len(calls) == 5


def test_fetch_subpath_env_threshold(monkeypatch, tmp_path):
    import httpx

    monkeypatch.setattr(httpx, "Client", _FakeClient)
    monkeypatch.setenv("LITEWORK_RESUMABLE_MAX_FILES", "3")
    _patch_api(monkeypatch, _make_tree("skills/big", 4))
    with pytest.raises(gh_fetch.TooManyFilesError):
        gh_fetch.fetch_subpath(str(tmp_path), "o", "r", "main", "skills/big")
    monkeypatch.delenv("LITEWORK_RESUMABLE_MAX_FILES")
    # 参数显式优先于环境变量
    _patch_api(monkeypatch, _make_tree("skills/big", 4))
    monkeypatch.setattr(gh_fetch, "_download_one",
                        lambda *a, **k: None)
    gh_fetch.fetch_subpath(str(tmp_path), "o", "r", "main", "skills/big", max_files=10)


def test_too_many_files_error_message():
    err = gh_fetch.TooManyFilesError("own", "repo", "skills/x", 12480, 1000)
    assert "12480" in str(err)
    assert "1000" in str(err)
    assert err.count == 12480
    assert err.max_files == 1000
