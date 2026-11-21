import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

// Substituted at build time; the literal keeps the unbuilt source runnable.
const IDLE_RAW = "@idleGlyph@";
const IDLE = IDLE_RAW.startsWith("@") ? "◍" : IDLE_RAW;

function formatDuration(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  if (total < 60) return `${total}s`;
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return m > 0 ? `${h}h ${m}m` : `${h}h`;
  return s > 0 ? `${m}m ${s}s` : `${m}m`;
}

function formatTime(date: Date = new Date(Date.now())): string {
  let hours = date.getHours();
  const minutes = date.getMinutes();
  const period = hours >= 12 ? "PM" : "AM";
  hours = hours % 12;
  if (hours === 0) hours = 12;
  const minuteStr = minutes < 10 ? `0${minutes}` : `${minutes}`;
  return `${hours}:${minuteStr} ${period}`;
}

function compose(elapsedSeconds: number, date: Date = new Date(Date.now()), glyph = IDLE): string {
  return `${glyph} Worked for ${formatDuration(elapsedSeconds)} · done ${formatTime(date)}`;
}

export const lib = { IDLE, compose, formatDuration, formatTime };

export default function turnTimer(pi: ExtensionAPI): void {
  let startedAt: number | undefined;

  pi.on("agent_start", () => {
    startedAt = Date.now();
  });

  pi.on("agent_end", (_event, ctx) => {
    if (startedAt === undefined) return;
    const elapsedSeconds = Math.max(0, Math.round((Date.now() - startedAt) / 1000));
    startedAt = undefined;
    if (!ctx.hasUI) return;
    ctx.ui.notify(compose(elapsedSeconds, new Date(Date.now())));
  });

  pi.on("session_start", () => {
    startedAt = undefined;
  });
}
