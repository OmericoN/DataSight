import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

type InsightRow = Record<string, unknown>;

function summarizeFeature(row: InsightRow, group: string, field: string): string | null {
  let features = row.features;
  if (typeof features === "string") {
    try {
      features = JSON.parse(features);
    } catch {
      return null;
    }
  }
  if (!features || typeof features !== "object" || Array.isArray(features)) return null;
  const section = (features as InsightRow)[group];
  if (!section || typeof section !== "object" || Array.isArray(section)) return null;
  const observation = (section as InsightRow)[field];
  if (!observation || typeof observation !== "object" || Array.isArray(observation)) return null;
  const { state, values } = observation as InsightRow;
  if (state === "conflicting") return "Conflicting reports";
  if (state === "unclear") return "Unclear";
  if (state !== "reported" || !Array.isArray(values)) return null;
  // The preview shows one value; the detail sheet retains the complete observation.
  for (const item of values) {
    if (!item || typeof item !== "object" || !("value" in item)) continue;
    const value: unknown = item.value;
    if (typeof value === "string" || typeof value === "number") return String(value).replaceAll("_", " ");
    if (value && typeof value === "object" && !Array.isArray(value)) {
      const claim = value as InsightRow;
      if (typeof claim.statement === "string") return claim.statement;
      if (typeof claim.name === "string") return claim.name;
    }
  }
  return null;
}

export function InsightSummaryList({
  rows,
  onSelect,
}: {
  rows: InsightRow[];
  onSelect: (row: InsightRow) => void;
}) {
  return <div className="divide-y" aria-label="Extracted dataset insights">
    {rows.map((row, index) => {
      const dataset = typeof row.dataset_name === "string" && row.dataset_name.trim() ? row.dataset_name : "Dataset not identified";
      const paper = typeof row.publication_title === "string" && row.publication_title.trim() ? row.publication_title : String(row.paper_id ?? "Publication not recorded");
      const role = summarizeFeature(row, "use", "roles") ?? (typeof row.dataset_role === "string" ? row.dataset_role.replaceAll("_", " ") : null);
      const use = summarizeFeature(row, "use", "description") ?? summarizeFeature(row, "use", "purposes");
      const access = summarizeFeature(row, "accessibility", "availability_statement") ?? summarizeFeature(row, "accessibility", "access_mode");
      const license = summarizeFeature(row, "reusability", "licenses");
      const match = typeof row.um_dataset_title === "string" && row.um_dataset_title.trim() ? row.um_dataset_title : null;
      const discovery = row.discovery_method ?? row.discovery_mode;
      return <article key={`${String(row.paper_id)}-${dataset}-${index}`} className="space-y-3 px-4 py-4 sm:px-5">
        <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0 flex-1">
            <h3 className="line-clamp-2 break-words text-sm font-semibold" title={dataset}>{dataset}</h3>
            <p className="mt-1 break-words text-xs text-muted-foreground">{paper}{row.publication_year ? ` · ${String(row.publication_year)}` : ""}</p>
            {discovery && discovery !== "unrecorded" ? <p className="mt-1 text-xs text-muted-foreground">Discovery: {String(discovery).replaceAll("_", " ")}</p> : null}
          </div>
          <div className="flex items-center gap-2">
            {role ? <Badge variant="secondary" className="capitalize">{role}</Badge> : null}
            <Button type="button" variant="outline" size="sm" onClick={() => onSelect(row)} aria-label={`View details for ${dataset} in ${paper}`}>View details</Button>
          </div>
        </div>
        {use || access || license || match ? <dl className="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-2 xl:grid-cols-4">
          {use ? <div className="min-w-0"><dt className="text-xs text-muted-foreground">How it was used</dt><dd className="mt-0.5 line-clamp-2 break-words">{use}</dd></div> : null}
          {access ? <div className="min-w-0"><dt className="text-xs text-muted-foreground">Access</dt><dd className="mt-0.5 line-clamp-2 break-words">{access}</dd></div> : null}
          {license ? <div className="min-w-0"><dt className="text-xs text-muted-foreground">License</dt><dd className="mt-0.5 line-clamp-2 break-words">{license}</dd></div> : null}
          {match ? <div className="min-w-0"><dt className="text-xs text-muted-foreground">Catalog match</dt><dd className="mt-0.5 line-clamp-2 break-words">{match}</dd></div> : null}
        </dl> : <p className="text-xs text-muted-foreground">No additional facts reported</p>}
      </article>;
    })}
  </div>;
}
