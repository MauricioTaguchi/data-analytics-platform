import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { DeleteResourceDialog } from "./DeleteResourceDialog";

describe("deletion disclosure", () => {
  it("describes retention honestly and starts with destructive submission disabled", () => {
    const markup = renderToStaticMarkup(<DeleteResourceDialog target={{ scope: "dataset", id: 2, name: "orders.csv" }} onClose={() => undefined} onDelete={async () => undefined} />);
    expect(markup).toContain('aria-describedby="deletion-impact deletion-retention"');
    expect(markup).toContain("existing backups remain subject");
    expect(markup).toContain("not complete data erasure");
    expect(markup).toContain('type="submit" class="danger-button" disabled=""');
    expect(markup).toContain('data-dialog-initial-focus="true"');
    expect(markup).not.toContain('type="password"');
  });

  it("requires the current password for account closure without exposing it", () => {
    const markup = renderToStaticMarkup(<DeleteResourceDialog target={{ scope: "account", name: "owner@example.com" }} onClose={() => undefined} onDelete={async () => undefined} />);
    expect(markup).toContain('type="password" autoComplete="current-password"');
    expect(markup).toContain("all sessions will lose access");
    expect(markup).toContain("Delete account");
  });
});
