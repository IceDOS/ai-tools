import { truncateToWidth } from "@earendil-works/pi-tui";

// pi wraps string-array widgets, so a long row eats the screen; a component keeps one line per row.
export class WidgetRows {
  constructor(private rows: string[]) {}

  invalidate(): void {}

  render(width: number): string[] {
    const w = Math.max(1, width);
    return this.rows.map((row) => truncateToWidth(row, w, "…", true));
  }
}
