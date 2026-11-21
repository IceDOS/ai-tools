// Pure helpers for tool timer formatting, shared by tool-timer and terminal-title.

/** Duration as pi's own tool rows print it: `4.2s`, `1m 30s`, `2h 3m 4s`. */
export function formatToolDuration(ms: number): string {
  const seconds = Math.max(0, ms) / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const total = Math.floor(seconds);
  const minutes = Math.floor(total / 60);
  if (minutes < 60) return `${minutes}m ${total % 60}s`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m ${total % 60}s`;
}

/** `mcp__server__tool` as `tool`: the tool row already names the server. */
export function shortTool(name: string): string {
  return name.startsWith("mcp__") ? name.slice(name.lastIndexOf("__") + 2) : name;
}

/** Format the running tool timer suffix for the terminal title. */
export function composeToolTimer(tool: string, elapsedMs: number): string {
  return `⏱ ${shortTool(tool)} ${formatToolDuration(elapsedMs)}`;
}
