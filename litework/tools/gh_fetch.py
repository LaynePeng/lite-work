# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""GitHub 内容抓取：**可续传**（断点续传）的按文件下载 + 本地缓存。

为什么不用 zipball / git clone：
- GitHub 的 zipball/tarball（codeload）实测**不支持 HTTP Range**（请求带
  `Range` 仍返回 200、无 `Accept-Ranges`）——一次网络抖动就得整包重下；
- `git clone` 也不支持传输中续传（中断后临时 pack 被丢弃，重来一遍）。

但 `raw.githubusercontent.com` **支持 Range**（返回 206）。因此社区的
「子目录安装」（`tree/{branch}/{dir}`）改为：
1. Git Trees API 一次性列出目标子路径下全部文件（含大小/mode）；
2. 逐文件从 raw（**按不可变的 commit SHA 固定**）下载，支持 Range 断点续传；
3. 落在持久缓存目录（`<cache>/github/<owner>__<repo>__<sha>/...`）——
   已完整下载的文件直接跳过，**跨重试 / 跨重启复用**，即真正的「可续」。

整仓安装（无子目录）仍回退 zipball；本模块只服务有明确子路径的场景。

**文件数上限（阈值回退）**：按文件下载的请求数 = 文件数。大技能（如
ppt-master 可达 12000+ 文件）首次安装会发起上万次 raw 请求，得不偿失——
超过阈值（`DEFAULT_MAX_FILES`，可用环境变量 `LITEWORK_RESUMABLE_MAX_FILES`
覆盖）时抛 `TooManyFilesError`，由调用方回退整包路径（技能 → git clone、
插件 → zipball）：单 pack 请求数是常数级，代价是不可续传（靠重试兜底）。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote

from .install_progress import STAGE_CONNECT, STAGE_DOWNLOAD, STAGE_LIST, get_progress

logger = logging.getLogger("litework.gh_fetch")

_TIMEOUT = None  # httpx.Timeout，惰性构造（避免模块导入即依赖 httpx 细节）
_RAW_BASE = "https://raw.githubusercontent.com"
_API_BASE = "https://api.github.com"

# 按文件续传的文件数上限：超过则回退 git clone / zipball（见模块 docstring）
DEFAULT_MAX_FILES = 1000


def _resumable_max_files() -> int:
    """读取生效的文件数上限（环境变量 LITEWORK_RESUMABLE_MAX_FILES 可覆盖）。"""
    raw = os.environ.get("LITEWORK_RESUMABLE_MAX_FILES", "").strip()
    if not raw:
        return DEFAULT_MAX_FILES
    try:
        n = int(raw)
    except ValueError:
        return DEFAULT_MAX_FILES
    return n if n > 0 else DEFAULT_MAX_FILES


class TooManyFilesError(Exception):
    """子路径文件数超过按文件续传阈值：应改用整包（git clone / zipball）。"""

    def __init__(self, owner: str, repo: str, subpath: str, count: int, max_files: int) -> None:
        self.count = count
        self.max_files = max_files
        super().__init__(
            f"{owner}/{repo} 的 {subpath} 共 {count} 个文件，超过可续传阈值 {max_files}"
            f"（按文件下载需发起 {count}+ 次 raw 请求）"
        )


def should_use_resumable(file_count: int, max_files: int = DEFAULT_MAX_FILES) -> bool:
    """纯函数：文件数是否适合按文件续传（独立出来便于单测）。"""
    return file_count <= max_files


def default_cache_root(config_dir: Optional[str] = None) -> str:
    """Git 抓取缓存根目录（默认 ~/.lite-work/cache/github，随 config_dir 可覆盖）。"""
    base = config_dir or os.path.join(str(Path.home()), ".lite-work")
    return os.path.join(base, "cache", "github")


def _timeout():
    import httpx

    global _TIMEOUT
    if _TIMEOUT is None:
        # connect 快速失败（慢网不干等），read 给单文件传输留足余量
        _TIMEOUT = httpx.Timeout(connect=8.0, read=30.0, write=10.0, pool=8.0)
    return _TIMEOUT


def _token() -> str:
    return (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()


def _api_get(client, url: str, token: str) -> Dict:
    """GET GitHub API（带速率限制的可读报错）。"""
    headers = {"User-Agent": "lite-work-agent", "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = client.get(url, headers=headers)
    if resp.status_code == 403 and resp.headers.get("X-RateLimit-Remaining") == "0":
        raise ValueError(
            "GitHub API 速率限制已用尽（未配置 GITHUB_TOKEN）；请稍后重试，"
            "或设置环境变量 GITHUB_TOKEN 提升配额"
        )
    if resp.status_code >= 400:
        raise ValueError(f"GitHub API 请求失败（HTTP {resp.status_code}）: {url}")
    return resp.json()


def _download_one(owner: str, repo: str, commit: str, path: str, dest: str,
                  expected_size: Optional[int], retries: int,
                  on_bytes: Optional[Callable[[int], None]] = None) -> None:
    """下载单个文件到 dest，支持断点续传（`.part` 累积 + Range）。

    - 目标已完整（大小匹配）→ 直接跳过；
    - 失败按指数退避重试，重试时从已下载字节数继续（raw 支持 Range）。
    """
    import httpx

    url = f"{_RAW_BASE}/{owner}/{repo}/{commit}/{quote(path)}"
    part = dest + ".part"
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.isfile(dest) and (expected_size is None or os.path.getsize(dest) == expected_size):
        return  # 缓存命中

    last_exc: Optional[BaseException] = None
    for attempt in range(1, retries + 1):
        try:
            _resume_download(url, part, expected_size, on_bytes)
            if os.path.isfile(part):
                os.replace(part, dest)
            elif not os.path.isfile(dest):
                # 0 字节文件：_resume_download 在 offset == expected_size 时
                # 直接返回，未创建 .part 文件。显式创建空 dest 确保后续替换成功。
                open(dest, "wb").close()
            return
        except Exception as exc:  # 网络/超时/不完整 → 退避重试
            last_exc = exc
            if attempt < retries:
                time.sleep(min(2 ** attempt, 10))
    raise IOError(f"下载失败（已重试 {retries} 次）: {path}：{last_exc}")


def _resume_download(url: str, part: str, expected_size: Optional[int],
                     on_bytes: Optional[Callable[[int], None]] = None) -> None:
    import httpx

    offset = os.path.getsize(part) if os.path.isfile(part) else 0
    if expected_size is not None and offset >= expected_size:
        return
    # 必须显式关掉压缩协商：否则 Range 偏移会落在 gzip 压缩流上，续传拼出的
    # 文件在解压时报 "incorrect header check"。identity 保证偏移即真实文件字节。
    headers = {"User-Agent": "lite-work-agent", "Accept-Encoding": "identity"}
    if offset > 0:
        headers["Range"] = f"bytes={offset}-"
    with httpx.Client(timeout=_timeout(), follow_redirects=True, headers=headers) as client:
        with client.stream("GET", url) as resp:
            if resp.status_code == 416:
                return  # 请求区间越界：视为已下载完
            if resp.status_code >= 400:
                raise IOError(f"HTTP {resp.status_code}: {url}")
            if offset > 0 and resp.status_code == 200:
                offset = 0  # 服务端忽略 Range → 从头写
            mode = "ab" if offset > 0 else "wb"
            with open(part, mode) as f:
                for chunk in resp.iter_bytes(chunk_size=64 * 1024):
                    f.write(chunk)
                    if on_bytes is not None:
                        on_bytes(len(chunk))
    if expected_size is not None and os.path.getsize(part) != expected_size:
        raise IOError(
            f"下载不完整: {url}（{os.path.getsize(part)}/{expected_size}）"
        )


class _Tracker:
    """并发下载的进度聚合（文件数 + 字节数），节流上报避免刷爆轮询。"""

    def __init__(self, files_total: int, bytes_total: int,
                 emit: Optional[Callable[[Dict[str, Any]], None]]) -> None:
        self._lock = threading.Lock()
        self.files_total = files_total
        self.files_done = 0
        self.bytes_total = bytes_total
        self.bytes_done = 0
        self._emit = emit
        self._last = 0.0

    def preset(self, nbytes: int, files: int = 0) -> None:
        with self._lock:
            self.bytes_done += max(0, int(nbytes))
            self.files_done += files

    def add_bytes(self, n: int) -> None:
        with self._lock:
            self.bytes_done += max(0, int(n))
            now = time.time()
            if now - self._last < 0.2:
                return
            self._last = now
        self._emit_progress()

    def file_complete(self) -> None:
        with self._lock:
            self.files_done += 1
        self._emit_progress()

    def emit(self) -> None:
        self._emit_progress()

    def _emit_progress(self) -> None:
        with self._lock:
            done, bdone = self.files_done, self.bytes_done
            total, btotal = self.files_total, self.bytes_total
        ctx = {
            "step": STAGE_DOWNLOAD,
            "message": f"下载文件 {done}/{total}",
            "files_done": done,
            "files_total": total,
            "bytes_done": bdone,
            "bytes_total": btotal,
        }
        if self._emit is not None:
            try:
                self._emit(ctx)
            except Exception:
                pass


def fetch_subpath(cache_root: str, owner: str, repo: str, ref: str, subpath: str,
                  *, retries: int = 4, max_workers: int = 8,
                  max_files: Optional[int] = None,
                  on_progress: Optional[Callable[[int, int], None]] = None) -> str:
    """把仓库子目录抓到缓存，返回缓存中该子目录的本地路径。

    真正「可续」：文件级缓存 + 单文件 Range 续传，跨重试/重启不重复下载。
    过程中通过 install_progress 上报「列出/下载」阶段与字节进度。

    max_files：按文件下载的文件数上限（None → 环境变量/默认值）；
    超过抛 TooManyFilesError，调用方应回退 git clone / zipball 整包下载。
    """
    import httpx

    # 在后台线程入口捕获回调，显式传给下载 worker（contextvar 不跨线程）
    emit = get_progress()
    subpath = subpath.strip("/")
    if emit is not None:
        emit({"step": STAGE_CONNECT, "message": "连接仓库…",
              "bytes_done": 0, "bytes_total": 0})
    token = _token()
    with httpx.Client(timeout=_timeout(), follow_redirects=True) as client:
        commit = _api_get(client, f"{_API_BASE}/repos/{owner}/{repo}/commits/{ref or 'HEAD'}", token).get("sha", "")
        if not commit:
            raise ValueError(f"无法解析 {owner}/{repo}@{ref} 的 commit")
        tree = _api_get(
            client,
            f"{_API_BASE}/repos/{owner}/{repo}/git/trees/{commit}?recursive=1",
            token,
        )
    if tree.get("truncated"):
        raise ValueError("仓库过大，Git Trees API 返回被截断；请改用 zipball 全量下载")
    entries: List[Dict] = [
        e for e in tree.get("tree", [])
        if e.get("type") == "blob"
        and e.get("mode") != "120000"  # 跳过符号链接（避免越界写入）
        and (e.get("path") == subpath or str(e.get("path", "")).startswith(subpath + "/"))
    ]
    if not entries:
        raise ValueError(f"仓库 {owner}/{repo}@{ref} 中未找到路径: {subpath}")
    effective_max = max_files if (max_files is not None and max_files > 0) else _resumable_max_files()
    if not should_use_resumable(len(entries), effective_max):
        raise TooManyFilesError(owner, repo, subpath, len(entries), effective_max)

    base = os.path.join(cache_root, f"{owner}__{repo}__{commit[:12]}")
    target_root = os.path.join(base, subpath)
    os.makedirs(target_root, exist_ok=True)

    work_items: List = []
    for entry in entries:
        rel = entry["path"][len(subpath) + 1:] if entry["path"] != subpath else os.path.basename(entry["path"])
        work_items.append((entry, os.path.join(target_root, *rel.split("/"))))
    total_bytes = sum(int(e.get("size") or 0) for e, _ in work_items)
    total_files = len(work_items)

    # 进度聚合：预置已命中缓存 / 断点续传的字节，续存后进度不回退
    tracker = _Tracker(total_files, total_bytes, emit)
    for entry, dest in work_items:
        if os.path.isfile(dest):
            tracker.preset(int(entry.get("size") or 0), files=1)
        elif os.path.isfile(dest + ".part"):
            tracker.preset(os.path.getsize(dest + ".part"))
    if emit is not None:
        emit({"step": STAGE_LIST, "message": f"列出 {total_files} 个文件",
              "files_done": tracker.files_done, "files_total": total_files,
              "bytes_done": tracker.bytes_done, "bytes_total": total_bytes})
    tracker.emit()

    def _work(item) -> None:
        entry, dest = item
        # 预扫描已计入完整缓存文件数（preset files=1），此处
        # 跳过 file_complete() 避免双重计数（双倍计 829/417）。
        already_cached = os.path.isfile(dest) and (
            entry.get("size") is None or os.path.getsize(dest) == int(entry.get("size"))
        )
        _download_one(owner, repo, commit, entry["path"], dest, entry.get("size"), retries,
                      on_bytes=tracker.add_bytes)
        if entry.get("mode") == "100755":
            try:
                os.chmod(dest, 0o755)
            except OSError:
                pass
        if not already_cached:
            tracker.file_complete()
        if on_progress is not None:
            try:
                on_progress(tracker.files_done, tracker.files_total)
            except Exception:
                pass

    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        futures = [pool.submit(_work, item) for item in work_items]
        for fut in as_completed(futures):
            fut.result()  # 任一失败即抛出（缓存保留，重试可从断点续）
    return target_root
