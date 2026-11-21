/** @jsxImportSource @opentui/solid */
// alimit usage bar at the bottom of opencode TUI (slot "app").
import { Plugin } from "@opencode/plugin/tui"
import { execFile } from "node:child_process"
import { createEffect, createSignal, For, Show } from "solid-js"

const ALIMIT = "@alimit@"
const PROVIDERS: Record<string, string> = {
  "opencode-go": "opencode-go",
}
const REFRESH_MS = 60_000

export default Plugin.define({
  id: "icedos.alimit",
  setup(context) {
    const [lines, setLines] = createSignal<string[]>([])

    const refresh = (providerID?: string) => {
      const p = providerID ? PROVIDERS[providerID] : undefined
      if (!p) {
        setLines([])
        return
      }
      execFile(
        ALIMIT,
        ["bar", "--provider", p],
        { timeout: 5000, env: { ...process.env, NO_COLOR: "1" } },
        (err, stdout) => {
          const text = err ? "" : stdout.trimEnd()
          setLines(text && !text.includes("alimit: no usage") ? text.split(/\r?\n/) : [])
        },
      )
    }

    context.ui.slot({
      append: "app",
      render: () => (
        <Show when={lines().length > 0}>
          <box flexDirection="column">
            <For each={lines()}>
              {(line) => <text fg={context.theme.text.muted}>{line}</text>}
            </For>
          </box>
        </Show>
      ),
    })

    createEffect(() => {
      const model = context.ui.model.current()
      refresh(model?.providerID)
    })

    const offStatus = context.data.on("session.status", (e) => {
      if (e.data.status.type === "idle") {
        const model = context.ui.model.current()
        refresh(model?.providerID)
      }
    })

    const timer = setInterval(() => {
      const model = context.ui.model.current()
      refresh(model?.providerID)
    }, REFRESH_MS)

    return () => {
      offStatus()
      clearInterval(timer)
    }
  },
})
