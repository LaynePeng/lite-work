# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""插件升级生效的两层缓存（真实事故回归）。

事故现象：覆盖安装 astock-plugin 后版本号已是新的、日志说「已加载」、无任何报错，
但工具行为仍是上一版——**只有重启 Core 才生效**。两层缓存各挡一次：

① `.pyc` 字节码：CPython 判新鲜只看源文件的「mtime（秒级）+ 大小」。覆盖安装写出的
   新文件常与旧文件大小相同、又落在同一秒，旧 .pyc 因此被判为新鲜 → import 执行旧
   字节码。插件自己在入口里清了 sys.modules 也救不回来，因为它清不掉字节码层。
② `sys.modules` 兄弟模块：宿主重载只 exec 入口文件，插件内部的 `import astock_*`
   走普通缓存，拿到的是旧模块对象。

本文件的用例都按「等长改写 + 同秒写盘」构造——用普通的 write_text 改写（长度不同）
是复现不出来的，测试会假通过。
"""
from __future__ import annotations

import os

from litework.tools import plugin_loader


PLUGIN_BODY = '''
from litework.core.types import ToolDefinition
from litework.tools.plugin import ToolPlugin


class CacheProbe(ToolPlugin):
    name = "cache-probe"
    description = "d"
    version = "1.0.0"

    def __init__(self):
        pass

    def get_tools(self):
        return [ToolDefinition(name="cache_probe_{ver}", description="d", parameters={{}})]

    async def execute(self, name, args):
        return "{ver}"
'''

SIBLING_OLD = "def marker():\n    return 'OLD'\n"
SIBLING_NEW = "def marker():\n    return 'NEW'\n"  # 与 SIBLING_OLD 等长

#: 入口把兄弟模块的返回值当工具名，这样一眼能看出跑的是哪一版
ENTRY_OLD = PLUGIN_BODY.format(ver="OLD")
ENTRY_NEW = PLUGIN_BODY.format(ver="NEW")  # 与 ENTRY_OLD 等长


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _make_plugin(tmp_path) -> str:
    """建一个最小「目录插件」：入口 + 一个兄弟模块。"""
    pdir = os.path.join(str(tmp_path), "plugins", "cache-probe")
    _write(os.path.join(pdir, "plugin.py"), ENTRY_OLD)
    _write(os.path.join(pdir, "cp_sibling.py"), SIBLING_OLD)
    return pdir


def _assert_same_second_overwrite(path: str, text: str, old_text: str) -> None:
    """等长 + 同秒改写（这正是骗过 CPython 的组合，测试必须真的构造出来）。"""
    assert len(text) == len(old_text), "用例前提：新旧内容等长"
    st0 = os.stat(path)
    _write(path, text)
    st1 = os.stat(path)
    assert st1.st_size == st0.st_size, "用例前提：改写后大小不变"
    assert int(st1.st_mtime) == int(st0.st_mtime), "用例前提：改写落在同一秒内"


def test_stale_bytecode_is_dropped_when_source_changes(tmp_path):
    """同秒等长改写：字节码必须被换掉，而非被 CPython 判为新鲜。"""
    pdir = _make_plugin(tmp_path)
    entry = os.path.join(pdir, "plugin.py")

    mod1 = plugin_loader._load_module(entry, "litework_plugin_cache_probe")
    assert mod1 is not None
    assert mod1.CacheProbe().get_tools()[0].name == "cache_probe_OLD"

    _assert_same_second_overwrite(entry, ENTRY_NEW, ENTRY_OLD)

    # 宿主重载走同一条路径：它自己会丢弃入口模块的旧引用并重新 exec
    mod2 = plugin_loader._load_module(entry, "litework_plugin_cache_probe")
    assert mod2 is not None
    assert mod2.CacheProbe().get_tools()[0].name == "cache_probe_NEW", \
        "源码已变更，取到的仍是旧字节码 → 升级不生效"


def test_sibling_modules_are_reimported_when_source_changes(tmp_path):
    """入口里的 `import cp_sibling` 必须拿到新对象，不能命中 sys.modules 旧模块。"""
    pdir = os.path.join(str(tmp_path), "plugins", "cache-probe")
    _write(os.path.join(pdir, "cp_sibling.py"), SIBLING_OLD)
    _write(os.path.join(pdir, "plugin.py"),
           "import cp_sibling\n"
           "from litework.tools.plugin import ToolPlugin\n"
           "\n"
           "class SiblingProbe(ToolPlugin):\n"
           "    name = 'sibling-probe'\n"
           "    version = '1.0.0'\n"
           "    def get_tools(self):\n"
           "        return []\n"
           "    async def execute(self, name, args):\n"
           "        return cp_sibling.marker()\n")
    entry = os.path.join(pdir, "plugin.py")

    mod1 = plugin_loader._load_module(entry, "litework_plugin_sibling_probe")
    assert mod1 is not None
    assert mod1.cp_sibling.marker() == "OLD"

    sibling = os.path.join(pdir, "cp_sibling.py")
    _assert_same_second_overwrite(sibling, SIBLING_NEW, SIBLING_OLD)

    mod2 = plugin_loader._load_module(entry, "litework_plugin_sibling_probe")
    assert mod2 is not None
    assert mod2.cp_sibling.marker() == "NEW", \
        "兄弟模块仍在 sys.modules 里命中旧对象 → 插件行为不更新"


def test_unchanged_source_keeps_bytecode(tmp_path):
    """没改源码就不该删 .pyc（加载路径不能被这个修复拖慢）。"""
    pdir = _make_plugin(tmp_path)
    data = [
        ("entry", os.path.join(pdir, "plugin.py"), "litework_plugin_cache_probe"),
        ("sibling", os.path.join(pdir, "cp_sibling.py"), "cp_sibling_probe"),
    ]
    # 只在首次见到的目录上判「变了」
    plugin_loader._refresh_plugin_bytecode(pdir)
    plugin_loader._load_module(data[0][1], data[0][2])
    plugin_loader._load_module(data[1][1], data[1][2])
    pycache = os.path.join(pdir, "__pycache__")
    before = sorted(n for n in os.listdir(pycache) if n.endswith(".pyc"))
    assert before, "首次 import 应产生字节码"

    assert plugin_loader._refresh_plugin_bytecode(pdir) is False
    after = sorted(n for n in os.listdir(pycache) if n.endswith(".pyc"))
    assert after == before, "源码未变却删了字节码缓存"


def test_watch_source_loader_refreshes_bytecode(tmp_path):
    """盯梢源有同一个字节码坑（它连兄弟模块清理都没有，更隐蔽）。"""
    from litework.watch.registry import WatchSourceRegistry

    pdir = os.path.join(str(tmp_path), "plugins", "watch-probe")
    body = (
        "from litework.watch.registry import WatchSource, ProbeResult\n"
        "\n"
        "class Probe(WatchSource):\n"
        "    name = 'watch-probe'\n"
        "    source_kind = 'probe-kind-{ver}'\n"
        "    display_name = 'd'\n"
        "    async def probe(self, cfg, ctx):\n"
        "        return ProbeResult(snapshot='{ver}')\n"
    )
    old, new = body.format(ver="OLD"), body.format(ver="NEW")
    entry = os.path.join(pdir, "plugin.py")
    _write(entry, old)

    reg1 = WatchSourceRegistry()
    assert reg1.load_from_dir(os.path.join(str(tmp_path), "plugins")) == 1
    assert reg1.get("probe-kind-OLD") is not None

    _assert_same_second_overwrite(entry, new, old)

    reg2 = WatchSourceRegistry()
    assert reg2.load_from_dir(os.path.join(str(tmp_path), "plugins")) == 1
    assert reg2.get("probe-kind-NEW") is not None, \
        "盯梢源取到旧字节码 → 升级后仍在跑旧探针"
