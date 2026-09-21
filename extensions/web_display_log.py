"""
Console log for web display
"""

from xun import ExtensionContext, WebDisplay

def setup_extension(ctx: ExtensionContext) -> None:
    if not isinstance(ctx.agent.display, WebDisplay):
        return 
    
    def _print(msg: str):
        print(msg)
    
    agent_id = f"{ctx.agent.name}"
    
    ctx.agent.hooks.before_display_error.add(lambda args: _print(
        f"{agent_id} Error: {args.message}"
    ))