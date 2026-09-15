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


def test_cached_files_not_double_counted(monkeypatch, tmp_path):
    """缓存命中的文件只计一次（回归：双重计数导致 829/417）。"""
    import httpx

    monkeypatch.setattr(httpx, "Client", _FakeClient)
    tree = {
        "truncated": False,
        "tree": [
            {"type": "blob", "path": f"skills/x/f{i}.txt", "size": 10, "mode": "100644"}
            for i in range(3)
        ],
    }
    _patch_api(monkeypatch, tree)

    downloads = []
    done = []

    def fake_download(owner, repo, commit, path, dest, expected_size, retries, on_bytes=None):
        # 模拟真实 _download_one 的缓存命中行为：大小匹配时跳过早返回
        if os.path.isfile(dest) and (expected_size is None or os.path.getsize(dest) == expected_size):
            return
        downloads.append(path)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8") as f:
            f.write("0123456789")

    monkeypatch.setattr(gh_fetch, "_download_one", fake_download)

    # 第一次：预扫描无缓存，全部真实下载，每个文件 file_complete() 计一次
    gh_fetch.fetch_subpath(str(tmp_path), "o", "r", "main", "skills/x",
                           max_files=10, on_progress=lambda d, t: done.append((d, t)))
    assert downloads == [f"skills/x/f{i}.txt" for i in range(3)]
    assert done[-1] == (3, 3)  # 结束进度正好 3/3，无超计

    # 第二次：预扫描全部缓存命中（files=1 预置），_work 应跳过 file_complete()
    downloads.clear()
    done.clear()
    gh_fetch.fetch_subpath(str(tmp_path), "o", "r", "main", "skills/x",
                           max_files=10, on_progress=lambda d, t: done.append((d, t)))
    assert downloads == []  # 全部命中缓存，不再真实下载
    assert done[-1] == (3, 3)  # 仍是 3/3，没有变成 6/3
