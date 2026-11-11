import type { ToolDef } from "./tools.ts";

export interface McpOptions {
  name: string;
  instructions: string;
  tools: ToolDef[];
  experimental?: Record<string, object>;
  call(name: string, args: Record<string, unknown>): string;
}

export class McpServer {
  opts: McpOptions;
  onInitialized?: () => void;

  constructor(opts: McpOptions) {
    this.opts = opts;
  }

  notify(method: string, params: object): string {
    return JSON.stringify({ jsonrpc: "2.0", method, params });
  }

  handle(line: string): string[] {
    let m: { id?: number | string; method?: string; params?: any };
    try {
      m = JSON.parse(line);
    } catch {
      return [JSON.stringify({ jsonrpc: "2.0", id: null, error: { code: -32700, message: "parse error" } })];
    }
    const reply = (result: object) => [JSON.stringify({ jsonrpc: "2.0", id: m.id, result })];
    if (m.id === undefined) {
      if (m.method === "notifications/initialized") this.onInitialized?.();
      return [];
    }
    switch (m.method) {
      case "initialize":
        return reply({
          protocolVersion: m.params?.protocolVersion ?? "2025-06-18",
          capabilities: { tools: {}, ...(this.opts.experimental ? { experimental: this.opts.experimental } : {}) },
          serverInfo: { name: this.opts.name, version: "0.1.0" },
          instructions: this.opts.instructions,
        });
      case "ping":
        return reply({});
      case "tools/list":
        return reply({ tools: this.opts.tools });
      case "tools/call":
        try {
          return reply({ content: [{ type: "text", text: this.opts.call(m.params?.name, m.params?.arguments ?? {}) }] });
        } catch (e) {
          return reply({ content: [{ type: "text", text: (e as Error).message }], isError: true });
        }
      default:
        return [JSON.stringify({ jsonrpc: "2.0", id: m.id, error: { code: -32601, message: `method not found: ${m.method}` } })];
    }
  }
}
