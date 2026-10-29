"""Xun execution core: agent, loop, config, and display protocols.

No module-level import of `xun.displays` / `xun.tools` / `xun.supervisor` /
`xun.entrypoint` (pinned by test/test_layering.py); sole exception: the
deferred builtin-tool import in `ToolBox.with_defaults()`.
"""
