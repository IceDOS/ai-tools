// Caps every model's contextWindow at a percentage of its native context.
// llamacpp models are excluded: the local server owns context, so the TUI must
// not cap it.

import { existsSync, readFileSync } from "node:fs";
import { getModels } from "@earendil-works/pi-ai";
import type {
  ExtensionAPI,
  ExtensionContext,
} from "@earendil-works/pi-coding-agent";

const PERCENT_RAW = "@contextWindowPercent@";
const PERCENT = PERCENT_RAW.startsWith("@") ? 30 : Number(PERCENT_RAW);
const EXCLUDED_PROVIDERS = ["llamacpp"];
// Native context per provider/id. Rebuilds clone model objects (new
// identities), so key by provider/id, never by object identity. Lives on
// globalThis: extensions load with moduleCache off, and a fresh map would read
// already-capped catalog objects as native and cap them twice.
type Capped = { provider: string; id: string; contextWindow?: number };
const NATIVE_KEY = Symbol.for("icedos.prime-agent.context-cap.native");
const nativeByKey: Map<string, number> = ((
  globalThis as unknown as Record<symbol, Map<string, number>>
)[NATIVE_KEY] ??= new Map());

function pinnedModelIds(modelsJsonPath: unknown): Set<string> {
  const pinned = new Set<string>();
  if (typeof modelsJsonPath !== "string" || !existsSync(modelsJsonPath)) {
    return pinned;
  }
  try {
    const parsed: {
      providers?: Record<string, { modelOverrides?: Record<string, unknown> }>;
    } = JSON.parse(readFileSync(modelsJsonPath, "utf-8"));
    for (const [provider, config] of Object.entries(parsed.providers ?? {})) {
      for (const [id, override] of Object.entries(config.modelOverrides ?? {})) {
        const ctx = (override as { contextWindow?: unknown }).contextWindow;
        if (typeof ctx === "number") {
          pinned.add(`${provider}/${id}`);
        }
      }
    }
  } catch {
    // Unreadable models.json pins nothing.
  }
  return pinned;
}

function nativeFor(
  provider: string,
  id: string,
  current: number,
): number {
  const key = `${provider}/${id}`;
  const recorded = nativeByKey.get(key);
  if (recorded !== undefined) {
    return recorded;
  }
  // The pi-ai catalog keeps the native values: capping only mutates the
  // session registry, never the catalog.
  let catalogNative: number | undefined;
  try {
    catalogNative = (getModels as (p: string) => { id: string; contextWindow?: number }[] | undefined)(provider)
      ?.find((m) => m.id === id)?.contextWindow;
  } catch {
    catalogNative = undefined;
  }
  const native =
    typeof catalogNative === "number" && catalogNative > 0
      ? catalogNative
      : current;
  nativeByKey.set(key, native);
  return native;
}

function capValue(model: Capped, pinned: Set<string>): number | undefined {
  if (EXCLUDED_PROVIDERS.includes(model.provider)) return undefined;
  if (pinned.has(`${model.provider}/${model.id}`)) return undefined;
  const current = model.contextWindow;
  if (typeof current !== "number" || current <= 0) return undefined;
  const native = nativeFor(model.provider, model.id, current);
  const cap = Math.round((PERCENT / 100) * native);
  if (cap <= 0 || cap >= native) return undefined;
  return cap;
}

// Prime-agent 0.9.8 deep-freezes part of the bundled catalog (the ollama
// models), so writing contextWindow throws on those. Replace frozen or
// non-writable entries with a shallow clone instead; the registry array itself
// stays writable.
function capRegistryEntry(models: Capped[], index: number, cap: number): void {
  const model = models[index];
  if (!model) return;
  const writable =
    !Object.isFrozen(model) &&
    Object.getOwnPropertyDescriptor(model, "contextWindow")?.writable !== false;
  if (writable) {
    model.contextWindow = cap;
    return;
  }
  models[index] = { ...model, contextWindow: cap };
}

// ctx.model and event.model are getter-only, so a frozen session model cannot
// be replaced. Cap it in place when writable and otherwise leave it alone; the
// registry entry with the same provider/id is capped either way.
function capModel(model: Capped | undefined, pinned: Set<string>): void {
  if (!model) return;
  const cap = capValue(model, pinned);
  if (cap === undefined) return;
  if (Object.isFrozen(model)) return;
  if (Object.getOwnPropertyDescriptor(model, "contextWindow")?.writable === false) return;
  model.contextWindow = cap;
}

// The session model is its own object: set_model and catalog refreshes rebuild
// the registry, leaving the session holding an uncapped copy the registry lost.
function capContexts(ctx: ExtensionContext, selected?: Capped): void {
  const registry = ctx.modelRegistry as unknown as {
    models: Capped[];
    modelsJsonPath?: string;
  };
  const pinned = pinnedModelIds(registry.modelsJsonPath);
  for (let index = 0; index < registry.models.length; index++) {
    const model = registry.models[index];
    const cap = capValue(model, pinned);
    if (cap === undefined) continue;
    capRegistryEntry(registry.models, index, cap);
  }
  capModel(ctx.model as Capped | undefined, pinned);
  capModel(selected, pinned);
}

export default function contextCap(pi: ExtensionAPI): void {
  pi.on("session_start", (_event, ctx) => capContexts(ctx));
  pi.on("session_tree", (_event, ctx) => capContexts(ctx));
  pi.on("model_select", (event, ctx) => capContexts(ctx, event.model));
  pi.on("before_agent_start", (_event, ctx) => capContexts(ctx));
  pi.on("turn_start", (_event, ctx) => capContexts(ctx));
}
