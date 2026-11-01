"""The agent half of the thornswood-decisions plugin. It registers nothing.

The plugin is a backend (dashboard/plugin_api.py) and a Desktop pane
(desktop/plugin.js). Hermes loads a plugin with a plugin.yaml through this
file, and plugin.yaml is what lets `hermes plugins enable thornswood-decisions`
find it, so this file is here and gives the agent no tools, hooks or commands.
"""


def register(ctx) -> None:
    return None
