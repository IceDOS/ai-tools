import anyio
import mcp.types as mtypes

try:
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:
    from mcp.server.fastmcp import FastMCP

from .service import RlmService


def build_app(service: RlmService) -> FastMCP:
    app = FastMCP("kernel")

    @app.tool()
    async def ipython(code: str) -> list[mtypes.TextContent | mtypes.ImageContent]:
        """Execute Python 3 in a persistent kernel. Variables, imports and functions survive across calls.
        The namespace provides rlm(prompt, context=None) -> str, which runs a recursive child agent with a
        clean context and returns its final answer, refine(instructions=None, global_=False), user_prompt,
        and show_image(path) to inspect images.
        Output: stdout (truncated at ~50KB/2000 lines), stderr, and the repr of a trailing expression.
        Keep large data in variables and print small slices."""
        result = await anyio.to_thread.run_sync(service.ipython, code)
        contents: list[mtypes.TextContent | mtypes.ImageContent] = [
            mtypes.TextContent(type="text", text=str(result))
        ]
        for img in getattr(result, "images", []):
            contents.append(
                mtypes.ImageContent(type="image", data=img["data"], mimeType=img["mime"])
            )
        return contents

    @app.tool()
    async def rlm_status() -> str:
        """Show the conversation, model for children, recursion depth, kernel state, snapshot and log paths."""
        return service.status()

    @app.tool()
    async def rlm_restart() -> str:
        """Kill the kernel. The next ipython call starts with an empty namespace."""
        return await anyio.to_thread.run_sync(service.restart)

    @app.tool()
    async def rlm_set_depth(depth: int) -> str:
        """Set the rlm() recursion limit for this session. 0 turns recursion off."""
        return service.set_depth(depth)

    @app.tool()
    async def refine(instructions: str = "", global_scope: bool = False) -> str:
        """Run a harness refinement now: a child model reviews this conversation and edits the persistent
        memories and policies injected into future sessions. global_scope=True targets the cross-session store."""
        return await anyio.to_thread.run_sync(service.refine_now, instructions or None, global_scope)

    @app.tool()
    async def refine_list() -> str:
        """List the current harness entries (global and this conversation's local ones)."""
        return service.refine_list()

    @app.tool()
    async def rlm_children() -> str:
        """List logged rlm() children of this conversation with parent, depth, state and log file path."""
        return service.children()

    return app
