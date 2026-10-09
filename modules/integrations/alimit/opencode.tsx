// alimit's usage windows in opencode's sidebar. opencode compiles local .tsx plugins itself.
// @alimit@ is substituted at build time.

import type { TuiPlugin, TuiPluginModule } from "@opencode-ai/plugin/tui"
import { createEffect, createSignal, For, Show } from "solid-js"

const ALIMIT = "@alimit@"
// alimit's own timer refreshes the DB; this only re-reads it.
const REFRESH_MS = 30_000
const BAR_WIDTH = 10

type Window = {
  bar_label: string
  util: number
  reset_ts: number | null
  will_exhaust_before_reset: boolean
}

// Mirrors cli.py fmt_dur.
function fmtDur(ms: number | null): string {
  if (ms === null) return "—"
  let s = Math.max(0, Math.floor(ms / 1000))
  const d = Math.floor(s / 86400)
  s -= d * 86400
  const h = Math.floor(s / 3600)
  s -= h * 3600
  const m = Math.floor(s / 60)
  if (d) return `${d}d${h}h`
  if (h) return `${h}h${m}m`
  return m ? `${m}m` : `${s}s`
}

function bar(util: number): string {
  const filled = Math.max(0, Math.min(BAR_WIDTH, Math.round((util / 100) * BAR_WIDTH)))
  return "█".repeat(filled) + "░".repeat(BAR_WIDTH - filled)
}

// opencode provider id to alimit provider; anything else has no quota to show.
const PROVIDERS: Record<string, string> = {
  anthropic: "claude",
  "opencode-go": "opencode-go",
  ollama: "ollama",
  "ollama-cloud": "ollama",
  openrouter: "openrouter",
}

function readWindows(provider: string): Promise<Window[]> {
  // Node built-ins load lazily; the TUI plugin runtime rejects them at the top level.
  const { execFile } = require("node:child_process")
  return new Promise((resolve) => {
    execFile(
      ALIMIT,
      ["status", "--json", "--provider", provider],
      { timeout: 5_000 },
      (err: unknown, stdout: string) => {
        try {
          resolve(err ? [] : (JSON.parse(stdout).windows ?? []))
        } catch {
          resolve([])
        }
      },
    )
  })
}

const tui: TuiPlugin = async (api) => {
  const [windows, setWindows] = createSignal<Window[]>([])
  const [now, setNow] = createSignal(Date.now())
  let provider: string | undefined

  // The session's latest model, else the configured default.
  const providerOf = (sessionID: string): string | undefined => {
    const messages = api.state.session.messages(sessionID)
    for (let i = messages.length - 1; i >= 0; i--) {
      const m: any = messages[i]
      const id = m.providerID ?? m.model?.providerID
      if (id) return PROVIDERS[id]
    }
    const model = api.state.config.model
    return model ? PROVIDERS[model.split("/")[0]] : undefined
  }

  const refresh = async () => {
    const p = provider
    const rows = p ? await readWindows(p) : []
    if (p === provider) setWindows(rows)
    setNow(Date.now())
  }

  const timer = setInterval(refresh, REFRESH_MS)
  api.lifecycle.onDispose(() => clearInterval(timer))

  api.slots.register({
    order: 600,
    slots: {
      sidebar_content(_ctx, props) {
        createEffect(() => {
          const p = providerOf(props.session_id)
          if (p === provider) return
          provider = p
          setWindows([])
          void refresh()
        })
        const theme = api.theme.current
        const color = (w: Window) =>
          w.will_exhaust_before_reset || w.util >= 80 ? theme.error : w.util >= 50 ? theme.warning : theme.success
        const width = () => Math.max(0, ...windows().map((w) => w.bar_label.length))
        return (
          <Show when={windows().length > 0}>
            <box>
              <text fg={theme.text}>
                <b>Usage</b>
              </text>
              <For each={windows()}>
                {(w) => (
                  <box flexDirection="row" columnGap={1}>
                    <text fg={theme.textMuted}>{w.bar_label.padEnd(width())}</text>
                    <text fg={color(w)}>{bar(w.util)}</text>
                    <text fg={theme.text}>{`${Math.round(w.util)}%`.padStart(4)}</text>
                    <text fg={theme.textMuted}>{w.reset_ts ? fmtDur(w.reset_ts - now()) : ""}</text>
                  </box>
                )}
              </For>
            </box>
          </Show>
        )
      },
    },
  })
}

const plugin: TuiPluginModule & { id: string } = {
  id: "alimit",
  tui,
}

export default plugin
