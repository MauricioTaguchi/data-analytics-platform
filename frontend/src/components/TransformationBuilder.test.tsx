import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { TransformationBuilder } from "./TransformationBuilder";
import { DEMO_OPERATIONS, emptyOperationDraft } from "../operation-contracts";
const props = { live: true, busy: false, columns: ["id", "total"], operation: "drop_duplicates", draft: emptyOperationDraft(), engine: "pandas", catalog: DEMO_OPERATIONS, catalogError: "", preview: null, onOperation: () => undefined, onDraft: () => undefined, onEngine: () => undefined, onRetryCatalog: () => undefined, onPreview: () => undefined, onApply: () => undefined };
describe("operation catalog rendering", () => {
  it("does not offer local fallback execution when a live catalog fails", () => {
    const markup = renderToStaticMarkup(<TransformationBuilder {...props} catalog={[]} catalogError="The API is unavailable." />);
    expect(markup).toContain("Retry catalog");
    expect(markup).not.toContain("Apply transformation");
  });
  it("shows operations supplied by the catalog and an editor for new schemas", () => {
    const custom = { ...DEMO_OPERATIONS[0], operation: "custom_operation", parameters_schema: { type: "object", properties: { count: { type: "integer" } } } };
    const markup = renderToStaticMarkup(<TransformationBuilder {...props} operation="custom_operation" catalog={[custom]} />);
    expect(markup).toContain("custom operation");
    expect(markup).toContain("Parameters JSON");
    expect(markup).not.toContain("Remove duplicates");
  });
  it("provides typed scalar controls for missing values", () => {
    const markup = renderToStaticMarkup(<TransformationBuilder {...props} operation="fill_nulls" />);
    expect(markup).toContain("Value type");
    expect(markup).toContain("Boolean");
    expect(markup).toContain("Add column parameter");
  });
});
