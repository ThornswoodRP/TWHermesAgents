"""Rule zero (Patrick, 1 Nov): "Fix the cause, no band-aids, ever" is in what
every Bot and Patrick's own Hermes always load, word for word, from this repo.

These fail if the rule goes missing from any of the places it is installed
from: the guard's prompt section (all six Bots), the thornswood-decisions
plugin's prompt section (my-hermes, and One's Hermes where it is linked), and
the two skills apply.py installs.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
GUARD_COPY = REPO / "thornswood_guard" / "rule_zero.md"
DECISIONS_COPY = REPO / "plugins" / "thornswood-decisions" / "rule_zero.md"
SKILLS = [REPO / "skills" / "thornswood" / n / "SKILL.md" for n in ("thornswood-guide", "thornswood-ops")]
BOTS = ("bramble", "rift", "sprocket", "echo", "magpie", "wisp")

# Lines that must be there, word for word, from the project's rule.
MUST = [
    "# Rule zero: fix the cause, no band-aids, ever",
    "- A band-aid is anything that gets past a failure without removing what causes it.",
    "  - re-running a failed check, job, download, install or setup script in the hope it passes;",
    "  - stepping around a rule, a safety check or a refusal, or asking someone else to do the refused step;",
    "  - a retry, a timer, a wait, an empty catch, a \"for now\", or anything that notices damage and undoes it.",
    "- Before you ship or offer any fix, write its cause in one sentence: what breaks and why.",
    "- Never offer Patrick a band-aid, not even as a second option next to the real fix.",
    "- When your own earlier change turns out to be a band-aid, say so, revert it, and record that on its issue.",
    "- Fix what's reported in the same reply. A write-up that changes nothing is not the job.",
]


class Ctx:
    def __init__(self, profile_name):
        self.profile_name = profile_name
        self.hooks = {}
        self.sections = {}
        self.tools = {}

    def register_hook(self, name, fn):
        self.hooks.setdefault(name, []).append(fn)

    def register_tool(self, name, toolset, schema, handler, **kw):
        self.tools[name] = toolset

    def register_system_prompt_section(self, sid, text, **kw):
        self.sections[sid] = (text, kw)


def load(test, plugin_dir: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, plugin_dir / "__init__.py",
                                                  submodule_search_locations=[str(plugin_dir)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    test.addCleanup(lambda: [sys.modules.pop(k) for k in list(sys.modules) if k == name or k.startswith(name + ".")])
    return module


class TheText(unittest.TestCase):
    def test_the_rule_is_there_word_for_word(self):
        text = GUARD_COPY.read_text(encoding="utf-8")
        for line in MUST:
            self.assertIn(line, text, line)
        self.assertTrue(text.startswith(MUST[0] + "\n"))

    def test_the_two_plugin_copies_are_the_same(self):
        self.assertEqual(GUARD_COPY.read_bytes(), DECISIONS_COPY.read_bytes())

    def test_it_fits_in_one_prompt_section(self):
        # Hermes caps a plugin section at 4,000 characters and cuts anything past it.
        self.assertLess(len(GUARD_COPY.read_text(encoding="utf-8")) + 400, 4000)

    def test_lf_and_no_em_dash(self):
        raw = GUARD_COPY.read_bytes()
        self.assertNotIn(b"\r", raw)
        self.assertNotIn("—", raw.decode("utf-8"))

    def test_both_skills_carry_it_before_anything_else(self):
        body = GUARD_COPY.read_text(encoding="utf-8").split("\n", 1)[1]
        for skill in SKILLS:
            text = skill.read_text(encoding="utf-8")
            self.assertIn("## Rule zero: fix the cause, no band-aids, ever", text, skill)
            self.assertIn(body, text, skill)
            first_section = text.index("\n## ")
            self.assertEqual(text.index("## Rule zero"), first_section + 1, f"{skill}: rule zero is not the first section")

    def test_bramble_rejects_band_aids_in_review(self):
        guide = SKILLS[0].read_text(encoding="utf-8")
        self.assertIn("Bramble rejects any worker PR, brief or card that is a band-aid under rule zero", guide)


class TheGuard(unittest.TestCase):
    def test_every_bot_gets_rule_zero_in_its_prompt(self):
        for i, bot in enumerate(BOTS):
            m = load(self, REPO / "thornswood_guard", f"_tw_rz_guard_{i}")
            ctx = Ctx(bot)
            m.register(ctx)
            text, kw = ctx.sections["thornswood.rule-zero"]
            for line in MUST:
                self.assertIn(line, text, bot)
            self.assertLessEqual(len(text), kw["max_chars"])
            self.assertEqual(kw["max_chars"], 4000)

    def test_only_bramble_gets_the_review_line(self):
        m = load(self, REPO / "thornswood_guard", "_tw_rz_guard_b")
        ctx = Ctx("bramble")
        m.register(ctx)
        self.assertIn("As Bramble you also reject", ctx.sections["thornswood.rule-zero"][0])
        m = load(self, REPO / "thornswood_guard", "_tw_rz_guard_w")
        ctx = Ctx("wisp")
        m.register(ctx)
        self.assertNotIn("As Bramble", ctx.sections["thornswood.rule-zero"][0])

    def test_the_gateway_profile_gets_it_too(self):
        m = load(self, REPO / "thornswood_guard", "_tw_rz_guard_g")
        ctx = Ctx("default")
        m.register(ctx)
        self.assertIn(MUST[1], ctx.sections["thornswood.rule-zero"][0])

    def test_without_rule_zero_the_guard_refuses_every_tool(self):
        tmp = Path(tempfile.mkdtemp()) / "thornswood_guard"
        self.addCleanup(shutil.rmtree, tmp.parent, True)
        shutil.copytree(REPO / "thornswood_guard", tmp, ignore=shutil.ignore_patterns("__pycache__"))
        (tmp / "rule_zero.md").unlink()
        m = load(self, tmp, "_tw_rz_guard_missing")
        ctx = Ctx("echo")
        m.register(ctx)
        out = ctx.hooks["pre_tool_call"][0](tool_name="read_file", args={"path": "README.md"})
        self.assertEqual(out["action"], "block")
        self.assertIn("rule zero", out["message"])
        self.assertNotIn("thornswood.rule-zero", ctx.sections)


class PatricksHermes(unittest.TestCase):
    def test_the_decisions_plugin_gives_rule_zero_and_nothing_else(self):
        m = load(self, REPO / "plugins" / "thornswood-decisions", "_tw_rz_decisions")
        ctx = Ctx("default")
        m.register(ctx)
        self.assertEqual(list(ctx.sections), ["thornswood.rule-zero"])
        text, kw = ctx.sections["thornswood.rule-zero"]
        self.assertEqual(text, DECISIONS_COPY.read_text(encoding="utf-8").strip())
        self.assertEqual(ctx.hooks, {})
        self.assertEqual(ctx.tools, {})


class Installed(unittest.TestCase):
    def test_apply_copies_rule_zero_with_the_guard(self):
        # apply.py copies the whole thornswood_guard package into each profile;
        # rule_zero.md must not be one of the files it leaves out.
        src = (REPO / "scripts" / "apply.py").read_text(encoding="utf-8")
        self.assertIn('shutil.copytree(REPO / "thornswood_guard"', src)
        self.assertNotIn("*.md", src.split('shutil.copytree(REPO / "thornswood_guard"', 1)[1].split("\n", 1)[0])


if __name__ == "__main__":
    unittest.main()
