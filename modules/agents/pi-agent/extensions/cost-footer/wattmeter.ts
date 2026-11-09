// Reads the kde wattmeter service (kde/modules/wattmeter), which meters local-model energy itself.

export const WATTMETER_NOW = "/run/wattmeter/now.json";
// One file per in-flight request; the service counts GPU draw above idle while any is fresh.
export const LEASE_DIR = "/run/wattmeter/ai";

export type AiReading = { currency: string; watts: number; windows: [string, number][] };

// null when the snapshot is unreadable, has no ai block, or is older than 3 service intervals.
export const aiReading = (text: string, nowMs: number): AiReading | null => {
  try {
    const d = JSON.parse(text);
    // Written as a negation so a missing ts or interval (NaN) also counts as stale.
    if (!(nowMs / 1000 - d.ts <= d.interval * 3)) return null;
    if (typeof d.ai !== "object" || d.ai === null) return null;
    return {
      currency: String(d.currency ?? ""),
      watts: Number(d.ai.watts) || 0,
      windows: (Array.isArray(d.ai.windows) ? d.ai.windows : []).map(
        (w: any) => [String(w.label), Number(w.cost) || 0] as [string, number],
      ),
    };
  } catch {
    return null;
  }
};
