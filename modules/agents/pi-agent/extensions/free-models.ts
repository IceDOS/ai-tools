// The one description of opencode Zen's free models: the provider table and the
// cost-footer's rate-limit pools, both of which come from the live catalog.
//
// Nothing is compiled in: there is no seed list, so until a catalog fetch succeeds the
// manifest is empty and the provider serves no models. loadCatalog() only reads the last
// successful fetch from the cache; resolveCatalog() performs the fetch, caches it and
// reports failure so the launch path can show it.

import * as fs from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import type { ThinkingLevelMap } from "@earendil-works/pi-ai/compat";

export const PROVIDER_ID = "opencode-free";
export const BASE_URL = process.env.PI_OPENCODE_FREE_BASE_URL ?? "https://opencode.ai/inference/openai/v1";
// pkgs.opencode's version, substituted at build time; unbuilt sources (tests) fall back to "latest".
const RAW_OPENCODE_VERSION = "@opencodeVersion@" as string;
export const OPENCODE_VERSION = RAW_OPENCODE_VERSION.startsWith("@") ? "latest" : RAW_OPENCODE_VERSION;
export const ZEN_USER_AGENT = process.env.PI_OPENCODE_FREE_UA ?? `opencode/latest/${OPENCODE_VERSION}/cli`;
/** Public list of every model Zen serves, paid and free, with cost/limits/efforts. */
export const CATALOG_URL = `${BASE_URL}/models`;
/** opencode builds `variants` from these models.dev reasoning options. */
export const EFFORT_LEVELS = ["minimal", "low", "medium", "high", "xhigh", "max"] as const;
export type EffortLevel = (typeof EFFORT_LEVELS)[number];
export type VariantId = "none" | EffortLevel;
export type FreeModel = {
  id: string;
  name: string;
  contextWindow: number;
  maxTokens: number;
  /** pi has no video/audio modality, so anything else the catalog lists is dropped. */
  input: ("text" | "image")[];
  /** opencode's `variants[].id`, which is the exact `reasoning_effort` value to send.
   *  Empty means the model has no variants, i.e. opencode only offers its "default". */
  variants: VariantId[];
};

/**
 * opencode builds `variants` from models.dev reasoning options: one variant per effort value,
 * plus "none" when the model has an explicit off. A list without "none" leaves pi's "off"
 * unmapped, so picking it sends no reasoning parameters at all - which is what opencode's
 * synthetic "default" entry does.
 */
export function toThinkingLevelMap(variants: VariantId[]): ThinkingLevelMap {
  const map: ThinkingLevelMap = {};
  for (const level of EFFORT_LEVELS) map[level] = variants.includes(level) ? level : null;
  if (variants.includes("none")) map.off = "none";
  return map;
}

/**
 * Rate-limit buckets for the free models: one bucket per list, measured 2026-08-31 as a single
 * ~1000 req / 5h allowance per (IP x opencode-UA-class). A model that is not listed here is its
 * own bucket, so an unmeasured grouping over-reports nothing - it only under-counts.
 *
 * Only measured members belong here: when opencode retires a model, drop it from its pool
 * rather than pointing the pool at a successor version, because the successor's bucket is not
 * known until it is burned. `stalePools()` reports members that the resolved catalog no longer
 * serves, and the extension surfaces that as a warning.
 */
export const FREE_MODEL_POOLS: Record<string, string[]> = {
  shared: ["big-pickle"],
  nemotron: ["nemotron-3.5-lightning-free"],
};

export const poolOf = (model: string): string =>
  Object.entries(FREE_MODEL_POOLS).find(([, models]) => models.includes(model))?.[0] ?? model;
export const poolModels = (pool: string): string[] => FREE_MODEL_POOLS[pool] ?? [pool];

type ServedModel = {
  id?: unknown;
  name?: unknown;
  type?: unknown;
  modalities?: { input?: unknown };
  capabilities?: { reasoning?: { effort?: unknown } | null } | null;
  limit?: { context?: unknown; output?: unknown } | null;
  cost?: { text?: { input?: { price?: unknown } | null; output?: { price?: unknown } | null } | null } | null;
};

const priceOf = (model: ServedModel, kind: "input" | "output"): unknown =>
  model.cost?.text?.[kind]?.price;

const num = (value: unknown): number | null =>
  typeof value === "number" && Number.isFinite(value) && value > 0 ? value : null;

/** Catalog effort ids -> pi variants: known levels in pi's order, "none" first. */
function variantsOf(model: ServedModel): VariantId[] {
  const effort = model.capabilities?.reasoning?.effort;
  if (!Array.isArray(effort)) return [];
  const variants: VariantId[] = EFFORT_LEVELS.filter((level) => effort.includes(level));
  return effort.includes("none") ? ["none", ...variants] : variants;
}

function inputOf(model: ServedModel): ("text" | "image")[] {
  const listed = Array.isArray(model.modalities?.input) ? model.modalities.input : [];
  const input: ("text" | "image")[] = [];
  if (listed.includes("text")) input.push("text");
  if (listed.some((kind) => typeof kind === "string" && kind.startsWith("image"))) input.push("image");
  return input.length > 0 ? input : ["text"];
}

/**
 * Reads the Zen catalog (`{ data: [...] }`) and keeps the free chat models, in the shape the
 * provider table and the cost-footer both consume. This is the only source of model
 * metadata: names, limits, modalities and reasoning variants all come from the catalog.
 */
export function parseServedModels(payload: unknown): FreeModel[] {
  const list = Array.isArray(payload)
    ? payload
    : Array.isArray((payload as { data?: unknown } | null)?.data)
      ? ((payload as { data: unknown[] }).data)
      : [];
  const models: FreeModel[] = [];
  const seen = new Set<string>();
  for (const entry of list as ServedModel[]) {
    if (typeof entry?.id !== "string" || seen.has(entry.id)) continue;
    if (entry.type !== "chat") continue;
    // Free means free on both sides: a model that charges for either direction is not ours.
    if (priceOf(entry, "input") !== 0 || priceOf(entry, "output") !== 0) continue;
    const contextWindow = num(entry.limit?.context);
    const maxTokens = num(entry.limit?.output);
    if (contextWindow === null || maxTokens === null) continue;
    seen.add(entry.id);
    const name = typeof entry.name === "string" && entry.name.trim() ? entry.name.trim() : entry.id;
    models.push({ id: entry.id, name, contextWindow, maxTokens, input: inputOf(entry), variants: variantsOf(entry) });
  }
  return models.sort((a, b) => a.id.localeCompare(b.id));
}

export const dataDir = (): string =>
  process.env.PI_CODING_AGENT_DIR ?? join(homedir(), ".config", "pi-agent");
export const catalogFile = (dir = dataDir()): string => join(dir, "opencode-free", "models.json");

export type CatalogSource = "cache" | "none";
export type Catalog = {
  /** Empty until a resolve succeeds and writes the cache; there is no compiled-in table. */
  models: FreeModel[];
  source: CatalogSource;
  /** When the cache was written, or null when nothing usable was cached. */
  fetchedAt: number | null;
  /** True when the table must be resolved now: nothing cached, or older than the TTL. */
  expired: boolean;
};

export const catalogDisabled = (): boolean => process.env.PI_OPENCODE_FREE_CATALOG === "off";
export const catalogTtlMs = (): number => {
  const raw = Number(process.env.PI_OPENCODE_FREE_CATALOG_TTL_MS);
  return Number.isFinite(raw) && raw >= 0 ? raw : 12 * 3600 * 1000;
};

type CatalogCache = { fetchedAt?: unknown; models?: unknown };

const isFreeModel = (m: any): m is FreeModel =>
  typeof m?.id === "string" &&
  typeof m.name === "string" &&
  num(m.contextWindow) !== null &&
  num(m.maxTokens) !== null &&
  Array.isArray(m.input) &&
  Array.isArray(m.variants);

/**
 * The last successfully fetched catalog, or an empty manifest when there is nothing usable
 * cached. Never throws: a broken cache must not crash the launch. An expired cache is still
 * served (with `expired: true`) because it is the newest table we actually saw - the caller
 * resolves over it, and a failed resolve leaves it in place.
 */
export function loadCatalog(options: { dir?: string; ttlMs?: number; now?: number } = {}): Catalog {
  const none: Catalog = { models: [], source: "none", fetchedAt: null, expired: true };
  if (catalogDisabled()) return none;
  let cache: CatalogCache;
  try {
    cache = JSON.parse(fs.readFileSync(catalogFile(options.dir), "utf8"));
  } catch {
    return none;
  }
  const fetchedAt = typeof cache.fetchedAt === "number" ? cache.fetchedAt : null;
  // The file may be hand-edited or from an older shape; drop entries toChatModel would crash on.
  const models = (Array.isArray(cache.models) ? cache.models : []).filter(isFreeModel);
  if (models.length === 0) return none;
  const age = (options.now ?? Date.now()) - (fetchedAt ?? 0);
  return {
    models,
    source: "cache",
    fetchedAt,
    expired: age > (options.ttlMs ?? catalogTtlMs()),
  };
}

export function writeCatalogCache(models: FreeModel[], options: { dir?: string; now?: number } = {}): void {
  const file = catalogFile(options.dir);
  const tmp = `${file}.${process.pid}.tmp`;
  try {
    fs.mkdirSync(dirname(file), { recursive: true });
    fs.writeFileSync(tmp, JSON.stringify({ fetchedAt: options.now ?? Date.now(), models }, null, 1));
    fs.renameSync(tmp, file);
  } catch {
    try {
      fs.unlinkSync(tmp);
    } catch {}
  }
}

export type RefreshResult =
  | { ok: true; models: FreeModel[] }
  | { ok: false; error: string };

/**
 * Fetches the live catalog and caches it. Anything that goes wrong (offline, timeout,
 * non-2xx, malformed payload, empty result) leaves the existing cache untouched and returns
 * the error, so the caller can decide between the last known table and no models at all.
 */
export async function refreshCatalog(options: { dir?: string; fetchImpl?: typeof fetch; timeoutMs?: number } = {}): Promise<RefreshResult> {
  const doFetch = options.fetchImpl ?? fetch;
  try {
    const response = await doFetch(process.env.PI_OPENCODE_FREE_MODELS_URL ?? CATALOG_URL, {
      headers: { accept: "application/json", "user-agent": ZEN_USER_AGENT },
      signal: AbortSignal.timeout(options.timeoutMs ?? 15_000),
    });
    if (!response.ok) return { ok: false, error: `catalog responded ${response.status}` };
    const models = parseServedModels(await response.json());
    if (models.length === 0) return { ok: false, error: "catalog returned no free models" };
    writeCatalogCache(models, { dir: options.dir });
    return { ok: true, models };
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : String(error) };
  }
}

/** What makes two resolved tables the same table: ids, limits, modalities, variants. */
export const modelSignature = (models: FreeModel[]): string =>
  models
    .map((model) => [model.id, model.contextWindow, model.maxTokens, model.variants.join("|"), model.input.join("|")].join(":"))
    .join(",");

export function describeModelChange(before: FreeModel[], after: FreeModel[]): string {
  if (before.length === 0) return `${after.length} models`;
  const oldIds = new Set(before.map((model) => model.id));
  const newIds = new Set(after.map((model) => model.id));
  const added = [...newIds].filter((id) => !oldIds.has(id));
  const removed = [...oldIds].filter((id) => !newIds.has(id));
  const parts = [
    added.length > 0 ? `added ${added.join(", ")}` : "",
    removed.length > 0 ? `removed ${removed.join(", ")}` : "",
  ].filter(Boolean);
  return parts.length > 0 ? parts.join("; ") : "model details changed";
}

/** Pool members (`pool:model`) that the resolved table does not serve, i.e. retired models. */
export function stalePools(models: FreeModel[]): string[] {
  const served = new Set(models.map((model) => model.id));
  return Object.entries(FREE_MODEL_POOLS).flatMap(([pool, members]) =>
    members.filter((model) => !served.has(model)).map((model) => `${pool}:${model}`),
  );
}

export type Resolution =
  | { status: "updated"; models: FreeModel[]; change: string }
  | { status: "unchanged" }
  | { status: "failed"; error: string };

/**
 * Resolves the catalog once: on success the table is published through `onModels` (the cache is
 * already written by then), on failure `onError` gets a message meant for the user. A failure
 * never falls back to a compiled-in list - if nothing is published yet, the caller keeps serving
 * no models, which is why the message says so.
 */
export async function resolveCatalog(options: {
  dir?: string;
  fetchImpl?: typeof fetch;
  timeoutMs?: number;
  /** The table currently published, used to decide whether this resolve changed anything. */
  current: FreeModel[];
  onModels: (models: FreeModel[]) => void;
  onError: (message: string) => void;
}): Promise<Resolution> {
  const result = await refreshCatalog({ dir: options.dir, fetchImpl: options.fetchImpl, timeoutMs: options.timeoutMs });
  if (!result.ok) {
    const message =
      `could not fetch the free model catalog (${result.error}).` +
      (options.current.length === 0
        ? " No free models are available until it resolves."
        : " Keeping the last known model list.");
    options.onError(message);
    return { status: "failed", error: message };
  }
  if (modelSignature(result.models) === modelSignature(options.current)) return { status: "unchanged" };
  options.onModels(result.models);
  return { status: "updated", models: result.models, change: describeModelChange(options.current, result.models) };
}
