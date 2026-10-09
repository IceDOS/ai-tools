import { createHash, randomUUID } from "node:crypto";
import type {
  ExtensionAPI,
  ExtensionContext,
} from "@earendil-works/pi-coding-agent";

// models.json cannot mint an id per run (Nix eval has no RNG), so the opencode
// headers live here instead. @opencodeVersion@ is substituted at build time.
const USER_AGENT = "opencode/@opencodeVersion@";

// Both zen backends reject an unidentified request; zen/go reports itself as
// "Console Go" and needs the same headers as zen/v1.
const PROVIDERS = ["opencode", "opencode-go"];

const BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz";

// Mirrors opencode's Identifier.descending("session") (packages/opencode/src/id/id.ts),
// seeded from the pi session id so a resumed session keeps the same zen id.
function opencodeSessionId(piSessionId: string): string {
  const hex = piSessionId.replace(/-/g, "");
  // uuidv7 carries its creation time in the first 48 bits.
  const timestamp = hex[12] === "7" ? parseInt(hex.slice(0, 12), 16) : Date.now();
  const now = ~(BigInt(timestamp) * BigInt(0x1000) + BigInt(1));
  const time = Buffer.alloc(6);
  for (let i = 0; i < 6; i++) {
    time[i] = Number((now >> BigInt(40 - 8 * i)) & BigInt(0xff));
  }
  const digest = createHash("sha256").update(piSessionId).digest();
  let suffix = "";
  for (let i = 0; i < 14; i++) suffix += BASE62[digest[i] % 62];
  return `ses_${time.toString("hex")}${suffix}`;
}

export default function zenSession(pi: ExtensionAPI) {
  let current = "";

  // registerProvider replaces the stored provider request config wholesale, so
  // the User-Agent has to be resent alongside every new session id.
  function useSessionId(sessionId: string): void {
    if (!sessionId || sessionId === current) return;
    current = sessionId;
    for (const provider of PROVIDERS) {
      pi.registerProvider(provider, {
        headers: {
          "User-Agent": USER_AGENT,
          "x-opencode-session": opencodeSessionId(sessionId),
        },
      });
    }
  }

  // Queued until the runner binds, so this covers a request made before the
  // first session_start.
  useSessionId(randomUUID());

  // session_tree catches the switches and forks that reassign the id mid-process.
  const track = (_event: unknown, ctx: ExtensionContext) => {
    useSessionId(ctx.sessionManager.getSessionId());
  };

  pi.on("session_start", track);
  pi.on("session_tree", track);
}
