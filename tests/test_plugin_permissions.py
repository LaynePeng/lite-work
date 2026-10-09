# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""插件能力声明与权限差异测试（W4）。

覆盖：
1. 归一化：合法/非法类别、bool/list/str 输入形态；
2. 源码提取：frontmatter / 类属性 / 找不到声明；
3. diff：新增类别 / 扩展 scope / 收窄 / 移除 / 无变化；
4. 显示：permissions_for_display 的人类可读行；
5. 集成：record_installed 记录权限，import_source 返回 permission_diff。
"""
from __future__ import annotations

import json

import pytest

from litework.core.plugin_permissions import (
    CATEGORY_LABELS,
    diff_permissions,
    extract_permissions_from_code,
    normalize_permissions,
    permissions_for_display,
    read_plugin_permissions,
)


# ---------------------------------------------------------------- 归一化

def test_normalize_valid():
    assert normalize_permissions({"exec": ["*"], "network": ["api.example.com"]}) == {
        "exec": ["*"], "network": ["api.example.com"],
    }


def test_normalize_bool_form():
    assert normalize_permissions({"exec": True, "fs_write": False}) == {
        "exec": ["*"], "fs_write": [],
    }


def test_normalize_ignores_unknown_category():
    assert normalize_permissions({"teleport": ["*"], "exec": ["*"]}) == {
        "exec": ["*"],
    }


def test_normalize_non_dict_returns_empty():
    assert normalize_permissions(None) == {}
    assert normalize_permissions("exec") == {}
    assert normalize_permissions([1, 2]) == {}


# ---------------------------------------------------------------- 源码提取

def test_extract_from_class_attribute():
    source = '''
class MyPlugin:
    permissions = {"exec": ["pip install"], "network": ["pypi.org"]}
'''
    perms = extract_permissions_from_code(source)
    assert perms.get("exec") == ["pip install"]
    assert perms.get("network") == ["pypi.org"]


def test_extract_not_found():
    assert extract_permissions_from_code("no permissions here") == {}
    assert extract_permissions_from_code("") == {}


# ---------------------------------------------------------------- diff

def test_diff_no_change():
    old = {"exec": ["pip"], "network": ["pypi.org"]}
    new = {"exec": ["pip"], "network": ["pypi.org"]}
    d = diff_permissions(old, new)
    assert not d["widened"] and not d["narrowed"]
    assert d["summary"] == "权限无变化"


def test_diff_added_category():
    old = {"network": ["pypi.org"]}
    new = {"network": ["pypi.org"], "exec": ["*"]}
    d = diff_permissions(old, new)
    assert d["widened"]
    assert d["added"] == {"exec": ["*"]}
    assert "执行命令" in d["summary"]


def test_diff_expanded_scope():
    old = {"network": ["pypi.org"]}
    new = {"network": ["pypi.org", "github.com"]}
    d = diff_permissions(old, new)
    assert d["widened"]
    assert d["expanded"] == {"network": ["github.com"]}


def test_diff_removed_and_shrunk():
    old = {"exec": ["*"], "network": ["pypi.org", "github.com"]}
    new = {"network": ["pypi.org"]}
    d = diff_permissions(old, new)
    assert d["narrowed"]
    assert not d["widened"]
    assert d["removed"] == {"exec": ["*"]}
    assert d["shrunk"] == {"network": ["github.com"]}


def test_diff_empty_to_empty():
    d = diff_permissions({}, {})
    assert not d["widened"] and not d["narrowed"]


# ---------------------------------------------------------------- 显示

def test_display_lines():
    lines = permissions_for_display({"exec": ["*"], "network": ["a.com", "b.com"]})
    assert any("⚠" in l and "执行命令" in l for l in lines)
    assert any("联网" in l and "a.com" in l for l in lines)


def test_display_empty():
    assert permissions_for_display({}) == ["（无特殊权限声明）"]


# ---------------------------------------------------------------- 读插件目录

def test_read_from_manifest_json(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps({
        "permissions": {"exec": ["pip install"]},
    }), encoding="utf-8")
    perms = read_plugin_permissions(str(tmp_path))
    assert perms.get("exec") == ["pip install"]


def test_read_from_plugin_py(tmp_path):
    (tmp_path / "plugin.py").write_text(
        'permissions = {"network": ["api.example.com"]}', encoding="utf-8")
    perms = read_plugin_permissions(str(tmp_path))
    assert perms.get("network") == ["api.example.com"]


def test_read_empty_dir(tmp_path):
    assert read_plugin_permissions(str(tmp_path)) == {}


def test_read_manifest_takes_priority(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps({
        "permissions": {"exec": ["from-manifest"]},
    }), encoding="utf-8")
    (tmp_path / "plugin.py").write_text(
        'permissions = {"exec": ["from-code"]}', encoding="utf-8")
    perms = read_plugin_permissions(str(tmp_path))
    assert perms["exec"] == ["from-manifest"]


# ---------------------------------------------------------------- 集成：installed.json 记录

def test_record_installed_stores_permissions(tmp_path):
    from litework.tools.plugin_loader import read_installed, record_installed

    config_dir = str(tmp_path / ".lite-work")
    plugin_dir = tmp_path / ".lite-work" / "plugins" / "test-plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.py").write_text(
        'permissions = {"exec": ["npm install"]}', encoding="utf-8")

    record_installed(config_dir, "test-plugin", "1.0.0", "test")

    data = read_installed(config_dir)
    entry = data.get("test-plugin", {})
    assert entry.get("permissions", {}).get("exec") == ["npm install"]
