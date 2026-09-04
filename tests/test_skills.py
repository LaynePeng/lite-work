import asyncio
import io
import json
import zipfile

import pytest

from litework.core.system_prompt import SystemPromptBuilder
from litework.tools.skills import (
    SkillsTools,
    parse_frontmatter,
)


def _make_skill(base, name, description="Review workflow", triggers=None, body="Run tests first."):
    skill_dir = base / ".agents" / "skills" / name
    skill_dir.mkdir(parents=True)
    fm = "---\nname: {n}\ndescription: {d}\n".format(n=name, d=description)
    if triggers:
        fm += "triggers: {t}\n".format(t=triggers)
    fm += "---\n\n# " + name + "\n\n" + body
    (skill_dir / "SKILL.md").write_text(fm, encoding="utf-8")
    return skill_dir


def test_skill_index_and_load(tmp_path):
    skill = tmp_path / ".agents" / "skills" / "review" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("description: Review workflow\n\nRun tests first.", encoding="utf-8")
    tools = SkillsTools(str(tmp_path))

    assert "review: Review workflow" in tools.index()
    result = asyncio.run(tools.execute("load_skill", {"skillName": "review"}))
    assert "Run tests first." in result
    # load_skill 附带技能目录绝对路径：Agent 工作区不是技能目录，缺路径只能瞎找
    assert "技能目录：" in result
    assert str(tmp_path / ".agents" / "skills" / "review") in result


def test_builtin_skills_visible_in_any_workspace(tmp_path):
    """产品内置技能随包分发：任意 workspace（含用户新建的空项目）都能发现。

    项目是动态创建/切换的，内置技能不能只在 lite-work 仓库下可见。
    """
    from litework.tools.skills import _builtin_skills_dir

    builtin = _builtin_skills_dir()
    # 开发态必然存在仓库 skills/；打包态存在 _internal/skills/
    assert builtin is not None, "内置技能目录未找到（开发态=仓库 skills/，打包态=_internal/skills/）"

    # 任意空 workspace（模拟用户新建的项目目录）也能发现内置技能
    empty_ws = tmp_path / "用户的新项目"
    empty_ws.mkdir()
    tools = SkillsTools(str(empty_ws))
    names = {s["name"] for s in tools.list_skills()}
    assert "diagram-to-office" in names
    assert "weekly-report" in names
    # scope 标记为 builtin
    scopes = {s["name"]: s["scope"] for s in tools.list_skills()}
    assert scopes["diagram-to-office"] == "builtin"

    # load_skill 可加载内置技能
    result = asyncio.run(tools.execute("load_skill", {"skillName": "diagram-to-office"}))
    assert "图表转 Office" in result


def test_workspace_skill_overrides_builtin(tmp_path, monkeypatch):
    """优先级：workspace > user > builtin（同名技能用户可覆盖内置版）。"""
    from litework.tools import skills as skills_mod

    builtin = skills_mod._builtin_skills_dir()
    assert builtin is not None

    # workspace 下放同名技能 diagram-to-office
    ws_skill = tmp_path / ".agents" / "skills" / "diagram-to-office" / "SKILL.md"
    ws_skill.parent.mkdir(parents=True)
    ws_skill.write_text("---\nname: diagram-to-office\ndescription: 工作区定制版\n---\n定制内容", encoding="utf-8")

    tools = SkillsTools(str(tmp_path))
    matched = [s for s in tools.list_skills() if s["name"] == "diagram-to-office"]
    assert len(matched) == 1
    assert matched[0]["scope"] == "workspace"
    assert matched[0]["description"] == "工作区定制版"


def test_project_instructions_support_claude_uppercase(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("Use the repository style.", encoding="utf-8")
    prompt = SystemPromptBuilder.build(str(tmp_path), [])
    assert "Use the repository style." in prompt
    assert "load_skill" in prompt


# ---------------------------------------------------------------- frontmatter

def test_parse_frontmatter_flat_and_nested():
    text = "---\nname: my-skill\ndescription: 测试技能\nlicense: MIT\nmetadata:\n  audience: devs\n---\nbody"
    meta = parse_frontmatter(text)
    assert meta["name"] == "my-skill"
    assert meta["description"] == "测试技能"
    assert meta["license"] == "MIT"
    assert meta["metadata"] == {"audience": "devs"}


def test_parse_frontmatter_absent():
    assert parse_frontmatter("no frontmatter here") == {}


# ---------------------------------------------------------------- 列表与读取

def test_list_skills_with_scope_and_writable(tmp_path, monkeypatch):
    # 隔离用户主目录：技能发现范围含 ~/.claude/skills 等用户级目录，
    # 不隔离会把开发机上的真实用户技能扫进来（测试必须封闭）
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows 家目录
    monkeypatch.setenv("HOME", str(tmp_path))
    _make_skill(tmp_path, "review")
    tools = SkillsTools(str(tmp_path))
    skills = tools.list_skills()
    # 内置技能（builtin scope）在任何环境都可见，不能断言总数
    by_name = {s["name"]: s for s in skills}
    assert "review" in by_name
    s = by_name["review"]
    assert s["scope"] == "workspace"
    assert s["writable"] is True
    assert s["description"] == "Review workflow"
    # 内置技能只读
    assert by_name["weekly-report"]["scope"] == "builtin"
    assert by_name["weekly-report"]["writable"] is False


def test_workspace_none_user_scope_only(tmp_path, monkeypatch):
    """桌面版未开项目：workspace=None 时用户级与内置技能可见。"""
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows 家目录
    monkeypatch.setenv("HOME", str(tmp_path))
    _make_skill(tmp_path, "user-skill", description="User level")
    tools = SkillsTools(None)
    assert tools.workspace is None
    skills = tools.list_skills()
    by_name = {s["name"]: s for s in skills}
    assert "user-skill" in by_name
    assert by_name["user-skill"]["scope"] == "user"
    # 未开项目也能看到产品内置技能
    assert by_name["diagram-to-office"]["scope"] == "builtin"


def test_read_skill_and_match_triggers(tmp_path):
    _make_skill(tmp_path, "review", triggers="审查, review, code review")
    tools = SkillsTools(str(tmp_path))
    assert "Run tests first." in tools.read_skill("review")
    assert [s["name"] for s in tools.match_skills("帮我 review 一下这段代码")] == ["review"]
    assert [s["name"] for s in tools.match_skills("审查这个 PR")] == ["review"]
    assert tools.match_skills("无关任务") == []


# ---------------------------------------------------------------- 创建/删除

def test_create_and_delete_skill(tmp_path):
    tools = SkillsTools(str(tmp_path))
    r = tools.create_skill("my-new-skill", "临时技能", "workspace")
    assert r["ok"] is True
    assert (tmp_path / ".agents" / "skills" / "my-new-skill" / "SKILL.md").is_file()
    names = [s["name"] for s in tools.list_skills()]
    assert "my-new-skill" in names
    d = tools.delete_skill("my-new-skill", "workspace")
    assert d["ok"] is True
    assert "my-new-skill" not in [s["name"] for s in tools.list_skills()]


def test_delete_readonly_scope_rejected(tmp_path):
    """第三方目录（.claude/skills）只读：删除必须拒绝。"""
    claude_dir = tmp_path / ".claude" / "skills" / "third-party"
    claude_dir.mkdir(parents=True)
    (claude_dir / "SKILL.md").write_text("---\nname: third-party\ndescription: x\n---\n", encoding="utf-8")
    tools = SkillsTools(str(tmp_path))
    with pytest.raises(ValueError):
        tools.delete_skill("third-party", "workspace")


def test_create_rejects_duplicate_and_bad_name(tmp_path):
    _make_skill(tmp_path, "review")
    tools = SkillsTools(str(tmp_path))
    with pytest.raises(ValueError):
        tools.create_skill("review", "dup", "workspace")
    with pytest.raises(ValueError):
        tools.create_skill("../escape", "bad", "workspace")
    with pytest.raises(ValueError):
        tools.create_skill("a/b", "bad", "workspace")


# ---------------------------------------------------------------- 导入

def test_import_from_local_dir(tmp_path, tmp_path_factory):
    src_base = tmp_path_factory.mktemp("src")
    src = src_base / "my-tool"
    src.mkdir()
    (src / "SKILL.md").write_text("---\nname: imported-skill\ndescription: 导入测试\n---\n正文", encoding="utf-8")
    tools = SkillsTools(str(tmp_path))
    results = tools.import_skill(str(src), "workspace")
    assert len(results) == 1
    assert results[0]["name"] == "imported-skill"
    assert (tmp_path / ".agents" / "skills" / "imported-skill" / "SKILL.md").is_file()


def test_import_zip_three_layouts(tmp_path):
    tools = SkillsTools(str(tmp_path))

    def _zip_bytes(files: dict) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for fname, content in files.items():
                zf.writestr(fname, content)
        return buf.getvalue()

    skill_md = "---\nname: zipped\ndescription: zip 导入\n---\nZIPBODY"
    # 布局1：SKILL.md 在 zip 根
    r1 = tools.import_zip_bytes(_zip_bytes({"SKILL.md": skill_md}), "workspace")
    assert r1[0]["name"] == "zipped"
    # 布局2：单顶层目录
    r2 = tools.import_zip_bytes(
        _zip_bytes({"my-skill/SKILL.md": "---\nname: layout2\ndescription: zip 导入\n---\nZIPBODY"}),
        "workspace")
    assert r2[0]["name"] == "layout2"
    # 布局3：多技能仓库
    r3 = tools.import_zip_bytes(_zip_bytes({
        "repo-main/skills-a/SKILL.md": "---\nname: skills-a\ndescription: A\n---\nA",
        "repo-main/skills-b/SKILL.md": "---\nname: skills-b\ndescription: B\n---\nB",
        "repo-main/README.md": "readme",
    }), "workspace")
    assert {r["name"] for r in r3} == {"skills-a", "skills-b"}


def test_import_zip_slip_blocked(tmp_path):
    tools = SkillsTools(str(tmp_path))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../evil.txt", "malicious")
        zf.writestr("SKILL.md", "---\nname: evil\ndescription: x\n---\n")
    with pytest.raises(ValueError):
        tools.import_zip_bytes(buf.getvalue(), "workspace")
    assert not (tmp_path / "evil.txt").exists()


def test_import_duplicate_rejected(tmp_path):
    _make_skill(tmp_path, "review")
    src = tmp_path / "src"
    src.mkdir()
    (src / "SKILL.md").write_text("---\nname: review\ndescription: dup\n---\n", encoding="utf-8")
    tools = SkillsTools(str(tmp_path))
    with pytest.raises(ValueError):
        tools.import_skill(str(src), "workspace")


def test_skill_extra_injected_into_system_prompt_only(tmp_path):
    """/skill 与 triggers 命中注入 system prompt，不进会话历史。"""
    _make_skill(tmp_path, "review", triggers="审查, review")
    from types import SimpleNamespace

    from litework.server.tasks import TaskManager
    tm = TaskManager.__new__(TaskManager)  # 不走完整依赖，仅测解析函数
    tm.app = SimpleNamespace(
        workspace=str(tmp_path),
        skill_permission=lambda name: "allow",  # 无权限规则时全部放行
    )
    extra, names, ask_names = TaskManager._resolve_skill_extra(tm, "帮我 review 这段代码")
    assert extra is not None and "Run tests first." in extra
    assert names == ["review"]
    assert ask_names == []
