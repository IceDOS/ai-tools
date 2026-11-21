import anyio
import mcp.types as mtypes

try:
    from mcp.server.mcpserver import Context, MCPServer as FastMCP
except ImportError:
    from mcp.server.fastmcp import Context, FastMCP

from .service import RlmService


def _extract_session_id(ctx: Context | None) -> str | None:
    if ctx is not None:
        try:
            meta = ctx.request_context.meta
            if meta:
                return meta.get("ai.opencode/sessionID")
        except Exception:
            pass
    return None


def build_app(service: RlmService) -> FastMCP:
    app = FastMCP("kernel")

    @app.tool()
    async def ipython(
        code: str, ctx: Context | None = None
    ) -> list[mtypes.TextContent | mtypes.ImageContent]:
        """Execute Python 3 in a persistent kernel. Variables, imports and functions survive across calls.
        The namespace provides rlm(prompt, context=None) -> str, which runs a recursive child agent with a
        clean context and returns its final answer, refine(instructions=None, global_=False), user_prompt,
        and show_image(path) to inspect images.
        Output: stdout (truncated at ~50KB/2000 lines), stderr, and the repr of a trailing expression.
        Keep large data in variables and print small slices."""
        sid = _extract_session_id(ctx)
        result = await anyio.to_thread.run_sync(service.ipython, code, sid)
        contents: list[mtypes.TextContent | mtypes.ImageContent] = [
            mtypes.TextContent(type="text", text=str(result))
        ]
        for img in getattr(result, "images", []):
            contents.append(
                mtypes.ImageContent(type="image", data=img["data"], mimeType=img["mime"])
            )
        return contents

    @app.tool()
    async def rlm_status(ctx: Context | None = None) -> str:
        """Show the conversation, model for children, recursion depth, kernel state, snapshot and log paths."""
        return service.status(_extract_session_id(ctx))

    @app.tool()
    async def rlm_restart(ctx: Context | None = None) -> str:
        """Kill the kernel. The next ipython call starts with an empty namespace."""
        return await anyio.to_thread.run_sync(service.restart, _extract_session_id(ctx))

    @app.tool()
    async def rlm_set_depth(depth: int, ctx: Context | None = None) -> str:
        """Set the rlm() recursion limit for this session. 0 turns recursion off."""
        return service.set_depth(depth)

    @app.tool()
    async def refine(
        instructions: str = "", global_scope: bool = False, ctx: Context | None = None
    ) -> str:
        """Run a harness refinement now: a child model reviews this conversation and edits the persistent
        memories and policies injected into future sessions. global_scope=True targets the cross-session store."""
        return await anyio.to_thread.run_sync(
            service.refine_now, instructions or None, global_scope, _extract_session_id(ctx)
        )

    @app.tool()
    async def refine_list(ctx: Context | None = None) -> str:
        """List the current harness entries (global and this conversation's local ones)."""
        return service.refine_list(_extract_session_id(ctx))

    @app.tool()
    async def rlm_children(ctx: Context | None = None) -> str:
        """List this conversation's rlm() children as a table to print verbatim: state
        (running, lost, ok, failed), depth, elapsed time, turns or stream steps, tokens, model, parent,
        the last stream event while running, and a prompt preview, then each child's log file."""
        return service.children(_extract_session_id(ctx))

    return app
