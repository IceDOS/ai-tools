import { createReadToolDefinition, type ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

// pi's read returns images, but RLM mode and rlm() children run without it; this keeps vision there.
export default function viewImage(pi: ExtensionAPI): void {
  pi.registerTool({
    name: "view_image",
    label: "View Image",
    description: "Show a local image file (png, jpg, gif or webp) to you, for example a screenshot or a downloaded figure.",
    promptSnippet: "View a local image file",
    parameters: Type.Object({ path: Type.String({ description: "Image path, absolute or relative to the cwd" }) }),
    async execute(toolCallId, params, signal, onUpdate, ctx) {
      const result = await createReadToolDefinition(ctx.cwd).execute(toolCallId, { path: params.path }, signal, onUpdate, ctx);
      // Text files stay out: RLM mode reads files through ipython on purpose.
      if (!result.content.some((part) => part.type === "image")) throw new Error(`${params.path} is not a png, jpg, gif or webp image`);
      return result;
    },
  });
}
