"""Load the guard plugin in a Python where yaml and ruamel cannot be imported,
as in Hermes's own runtime, and print what it says about `git status`."""

import builtins
import importlib.util
import sys

real_import = builtins.__import__


def no_yaml(name, *args, **kwargs):
    if name.split(".")[0] in ("yaml", "ruamel"):
        raise ImportError(name)
    return real_import(name, *args, **kwargs)


builtins.__import__ = no_yaml

plugin = sys.argv[1]
spec = importlib.util.spec_from_file_location("probe_guard", plugin + "/__init__.py",
                                              submodule_search_locations=[plugin])
module = importlib.util.module_from_spec(spec)
sys.modules["probe_guard"] = module
spec.loader.exec_module(module)


class Ctx:
    profile_name = "rift"

    def __init__(self):
        self.hooks = {}

    def register_hook(self, name, fn):
        self.hooks[name] = fn

    def register_tool(self, *args, **kwargs):
        pass

    def register_system_prompt_section(self, *args, **kwargs):
        pass


ctx = Ctx()
module.register(ctx)
print(ctx.hooks["pre_tool_call"](tool_name="terminal", args={"command": "git status"}))
