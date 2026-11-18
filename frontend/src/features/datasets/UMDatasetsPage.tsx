import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  RiCheckLine,
  RiDatabase2Line,
  RiErrorWarningLine,
  RiExternalLinkLine,
  RiRefreshLine,
  RiSearchLine,
} from "@remixicon/react";
import { useMemo, useState, type FormEvent } from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty";
import { Input } from "@/components/ui/input";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  api,
  type UMDatasetSummary,
  type UMDatasetVerificationIssue,
} from "@/shared/api/client";

const PAGE_SIZE = 50;
const selectClassName =
  "h-9 min-w-0 rounded-md border border-input bg-background px-2.5 text-sm shadow-xs outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50";

export default function UMDatasetsPage() {
  const queryClient = useQueryClient();
  const [searchInput, setSearchInput] = useState("");
  const [search, setSearch] = useState("");
  const [repository, setRepository] = useState("");
  const [year, setYear] = useState("");
  const [offset, setOffset] = useState(0);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const catalogQuery = useQuery({
    queryKey: ["um-datasets", search, repository, year, offset],
    queryFn: () =>
      api.umDatasets({
        q: search || undefined,
        repository: repository || undefined,
        year: year ? Number(year) : undefined,
        offset,
        limit: PAGE_SIZE,
      }),
  });
  const verificationQuery = useQuery({
    queryKey: ["um-datasets-verification"],
    queryFn: api.verifyUmDatasets,
    staleTime: Number.POSITIVE_INFINITY,
  });
  const detailQuery = useQuery({
    queryKey: ["um-dataset", selectedId],
    queryFn: () => api.umDataset(selectedId as string),
    enabled: selectedId !== null,
  });
  const importMutation = useMutation({
    mutationFn: api.importUmDatasets,
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["um-datasets"] }),
        queryClient.invalidateQueries({ queryKey: ["um-datasets-verification"] }),
        queryClient.invalidateQueries({ queryKey: ["um-discovery-profile"] }),
      ]);
    },
  });

  const catalog = catalogQuery.data;
  const verification = verificationQuery.data;
  const catalogNeedsImport = verification?.status === "not_imported";
  const issueById = useMemo(
    () => new Map(verification?.issues.map((issue) => [issue.um_dataset_id, issue]) ?? []),
    [verification?.issues],
  );
  const start = catalog && catalog.total ? catalog.offset + 1 : 0;
  const end = catalog ? Math.min(catalog.offset + catalog.items.length, catalog.total) : 0;

  function submitSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setOffset(0);
    setSearch(searchInput.trim());
  }

  function checkAgain() {
    void queryClient.invalidateQueries({ queryKey: ["um-datasets-verification"] });
  }

  return (
    <div className="mx-auto flex w-full min-w-0 max-w-7xl flex-col gap-5">
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <HealthCard label="Stored datasets" value={verification?.stored_count ?? catalog?.total} loading={!verification && verificationQuery.isLoading} />
        <HealthCard label="Authoritative source" value={verification?.source_count} loading={verificationQuery.isLoading} fallback="Unavailable" />
        <HealthCard label="Verified records" value={verification?.verified_count} loading={verificationQuery.isLoading} />
        <Card size="sm">
          <CardHeader>
            <CardDescription>Catalog integrity</CardDescription>
            <CardTitle>{verificationQuery.isLoading ? "Checking…" : integrityLabel(verification?.status ?? "unchecked")}</CardTitle>
            <CardAction>
              <IntegrityBadge status={verification?.status ?? "unchecked"} />
            </CardAction>
          </CardHeader>
        </Card>
      </div>

      {verification?.status === "unavailable" ? (
        <Alert variant="destructive">
          <RiErrorWarningLine />
          <AlertTitle>Catalog verification unavailable</AlertTitle>
          <AlertDescription>{verification.message || `Could not read ${verification.source_path}.`}</AlertDescription>
        </Alert>
      ) : null}
      {verification?.status === "mismatch" ? (
        <Alert variant="destructive">
          <RiErrorWarningLine />
          <AlertTitle>Stored catalog differs from the authoritative source</AlertTitle>
          <AlertDescription>{verification.message || `${verification.issues.length} record issue${verification.issues.length === 1 ? "" : "s"} found. Review the complete list below.`}</AlertDescription>
        </Alert>
      ) : null}
      {importMutation.isError ? (
        <Alert variant="destructive">
          <RiErrorWarningLine />
          <AlertTitle>Catalog import failed</AlertTitle>
          <AlertDescription>{importMutation.error.message}</AlertDescription>
        </Alert>
      ) : null}
      {verification?.status === "verified" ? (
        <Alert>
          <RiCheckLine />
          <AlertTitle>Catalog verified</AlertTitle>
          <AlertDescription>Every record in {verification.source_path} matches its stored representation.</AlertDescription>
        </Alert>
      ) : null}
      {verification?.warnings.length ? (
        <details className="rounded-lg border bg-card px-4 py-3 text-sm text-card-foreground">
          <summary className="cursor-pointer font-medium outline-none focus-visible:ring-3 focus-visible:ring-ring/50">
            Source import notes ({verification.warnings.length})
          </summary>
          <ul className="mt-3 grid gap-2 pl-5 text-muted-foreground">
            {verification.warnings.map((warning) => <li key={warning} className="list-disc">{warning}</li>)}
          </ul>
        </details>
      ) : null}

      <Card className="min-w-0">
        <CardHeader>
          <CardTitle>UM datasets</CardTitle>
          <CardDescription>{catalogNeedsImport ? "Import the validated local source before exploring UM datasets." : "Search and inspect the complete stored Maastricht University dataset catalog."}</CardDescription>
          <CardAction>
            <Button type="button" variant="outline" size="sm" onClick={checkAgain} disabled={verificationQuery.isFetching}>
              <RiRefreshLine data-icon="inline-start" />
              {verificationQuery.isFetching ? "Checking…" : "Check again"}
            </Button>
          </CardAction>
        </CardHeader>
        <CardContent className="flex min-w-0 flex-col gap-4">
          {!catalogNeedsImport ? <form className="grid gap-3 lg:grid-cols-[minmax(240px,1fr)_240px_150px_auto]" onSubmit={submitSearch}>
            <div className="relative">
              <RiSearchLine className="pointer-events-none absolute left-2.5 top-2.5 size-4 text-muted-foreground" />
              <Input aria-label="Search UM datasets" className="pl-8" value={searchInput} onChange={(event) => setSearchInput(event.target.value)} placeholder="Search ID, title, DOI, creator, or keyword" />
            </div>
            <select aria-label="Filter by repository" className={selectClassName} value={repository} onChange={(event) => { setRepository(event.target.value); setOffset(0); }}>
              <option value="">All repositories</option>
              {catalog?.repositories.map((value) => <option key={value} value={value}>{value}</option>)}
            </select>
            <select aria-label="Filter by year" className={selectClassName} value={year} onChange={(event) => { setYear(event.target.value); setOffset(0); }}>
              <option value="">All years</option>
              {catalog?.years.map((value) => <option key={value} value={value}>{value}</option>)}
            </select>
            <Button type="submit">Search</Button>
          </form> : null}

          {catalogQuery.isError ? (
            <Alert variant="destructive"><RiErrorWarningLine /><AlertTitle>Could not load UM datasets</AlertTitle><AlertDescription>{catalogQuery.error.message}</AlertDescription></Alert>
          ) : null}
          {catalogQuery.isLoading ? <CatalogSkeleton /> : null}
          {catalogNeedsImport || (!catalogQuery.isLoading && catalog && !catalog.items.length) ? (
            <Empty>
              <EmptyHeader>
                <EmptyMedia variant="icon"><RiDatabase2Line /></EmptyMedia>
                <EmptyTitle>{catalogNeedsImport ? "Catalog setup required" : "No datasets found"}</EmptyTitle>
                <EmptyDescription>{catalogNeedsImport ? `The authoritative source contains ${verification?.source_count?.toLocaleString() ?? "available"} records. Importing is atomic and refuses an empty source.` : search || repository || year ? "Try clearing or changing the catalog filters." : "No stored datasets are available."}</EmptyDescription>
              </EmptyHeader>
              {catalogNeedsImport && verification ? (
                <Button
                  type="button"
                  disabled={importMutation.isPending}
                  onClick={() => importMutation.mutate(verification.source_path)}
                >
                  <RiDatabase2Line data-icon="inline-start" />
                  {importMutation.isPending ? "Importing catalog…" : "Import authoritative catalog"}
                </Button>
              ) : null}
            </Empty>
          ) : null}
          {!catalogNeedsImport && catalog?.items.length ? (
            <div className="min-w-0 overflow-x-auto rounded-lg border">
              <Table className="min-w-[920px]">
                <TableHeader><TableRow><TableHead>Status</TableHead><TableHead>Dataset</TableHead><TableHead>Year</TableHead><TableHead>Repository</TableHead><TableHead>Creators</TableHead><TableHead>DOI</TableHead></TableRow></TableHeader>
                <TableBody>
                  {catalog.items.map((dataset) => (
                    <DatasetRow key={dataset.um_dataset_id} dataset={dataset} issue={issueById.get(dataset.um_dataset_id)} verificationStatus={verification?.status} onSelect={() => setSelectedId(dataset.um_dataset_id)} />
                  ))}
                </TableBody>
              </Table>
            </div>
          ) : null}

          {!catalogNeedsImport && catalog && catalog.total > 0 ? (
            <div className="flex flex-wrap items-center justify-between gap-3 text-sm text-muted-foreground">
              <p>Showing {start}–{end} of {catalog.total}</p>
              <div className="flex gap-2">
                <Button type="button" variant="outline" size="sm" disabled={offset === 0 || catalogQuery.isFetching} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>Previous</Button>
                <Button type="button" variant="outline" size="sm" disabled={offset + PAGE_SIZE >= catalog.total || catalogQuery.isFetching} onClick={() => setOffset(offset + PAGE_SIZE)}>Next</Button>
              </div>
            </div>
          ) : null}
        </CardContent>
      </Card>

      {verification?.issues.length ? <VerificationIssues issues={verification.issues} onSelect={setSelectedId} /> : null}

      <DatasetDetailSheet selectedId={selectedId} onClose={() => setSelectedId(null)} loading={detailQuery.isLoading} error={detailQuery.error} dataset={detailQuery.data} />
    </div>
  );
}

function HealthCard({ label, value, loading, fallback = "—" }: { label: string; value: number | null | undefined; loading: boolean; fallback?: string }) {
  return <Card size="sm"><CardHeader><CardDescription>{label}</CardDescription><CardTitle>{loading ? <Skeleton className="h-7 w-20" /> : value === null || value === undefined ? fallback : value.toLocaleString()}</CardTitle></CardHeader></Card>;
}

function DatasetRow({ dataset, issue, verificationStatus, onSelect }: { dataset: UMDatasetSummary; issue?: UMDatasetVerificationIssue; verificationStatus?: string; onSelect: () => void }) {
  const status = issue?.status ?? (verificationStatus === "verified" || verificationStatus === "mismatch" ? "verified" : "unchecked");
  return (
    <TableRow>
      <TableCell><IntegrityBadge status={status} /></TableCell>
      <TableCell className="max-w-[360px]"><button type="button" className="block w-full text-left" onClick={onSelect}><span className="block truncate font-medium hover:underline">{dataset.title}</span><span className="block font-mono text-xs text-muted-foreground">{dataset.um_dataset_id}</span></button></TableCell>
      <TableCell>{dataset.year ?? "—"}</TableCell>
      <TableCell className="max-w-[180px] truncate" title={dataset.repository ?? undefined}>{dataset.repository ?? "—"}</TableCell>
      <TableCell className="max-w-[240px] truncate" title={dataset.creators.join(", ")}>{dataset.creators.join(", ") || "—"}</TableCell>
      <TableCell className="max-w-[190px] truncate">{dataset.doi ? <a className="hover:underline" href={`https://doi.org/${dataset.doi}`} target="_blank" rel="noreferrer">{dataset.doi}</a> : "—"}</TableCell>
    </TableRow>
  );
}

function IntegrityBadge({ status }: { status: string }) {
  const tone = status === "verified" ? "success" : status === "unchecked" ? "muted" : status === "not_imported" ? "warning" : "error";
  return <Badge variant="outline" data-status-tone={tone}>{integrityLabel(status)}</Badge>;
}

function integrityLabel(status: string) {
  if (status === "not_imported") return "Not imported";
  return status.charAt(0).toUpperCase() + status.slice(1);
}

function VerificationIssues({ issues, onSelect }: { issues: UMDatasetVerificationIssue[]; onSelect: (id: string) => void }) {
  return (
    <Card className="min-w-0">
      <CardHeader><CardTitle>Integrity issues</CardTitle><CardDescription>All differences between the authoritative source and stored catalog.</CardDescription></CardHeader>
      <CardContent className="min-w-0 overflow-x-auto">
        <Table className="min-w-[720px]"><TableHeader><TableRow><TableHead>Status</TableHead><TableHead>Dataset</TableHead><TableHead>Changed fields</TableHead></TableRow></TableHeader>
          <TableBody>{issues.map((issue) => <TableRow key={`${issue.status}-${issue.um_dataset_id}`}><TableCell><IntegrityBadge status={issue.status} /></TableCell><TableCell><button type="button" className="text-left hover:underline disabled:no-underline" disabled={issue.status === "missing"} onClick={() => onSelect(issue.um_dataset_id)}><span className="block font-medium">{issue.title}</span><span className="font-mono text-xs text-muted-foreground">{issue.um_dataset_id}</span></button></TableCell><TableCell>{issue.changed_fields.join(", ") || "—"}</TableCell></TableRow>)}</TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}

function DatasetDetailSheet({ selectedId, onClose, loading, error, dataset }: { selectedId: string | null; onClose: () => void; loading: boolean; error: Error | null; dataset: Awaited<ReturnType<typeof api.umDataset>> | undefined }) {
  return (
    <Sheet open={selectedId !== null} onOpenChange={(open) => { if (!open) onClose(); }}>
      <SheetContent side="right" className="min-h-0 w-[94vw] max-w-[calc(100vw-1rem)] overflow-hidden sm:max-w-3xl">
        <SheetHeader className="shrink-0"><SheetTitle>{dataset?.title ?? "UM dataset details"}</SheetTitle><SheetDescription>{selectedId}</SheetDescription></SheetHeader>
        <ScrollArea className="min-h-0 flex-1 overflow-hidden px-4 pb-4">
          {loading ? <CatalogSkeleton /> : null}
          {error ? <Alert variant="destructive"><RiErrorWarningLine /><AlertTitle>Could not load dataset details</AlertTitle><AlertDescription>{error.message}</AlertDescription></Alert> : null}
          {dataset ? (
            <div className="grid gap-4 pb-4">
              <div className="grid gap-3 rounded-lg border p-4 sm:grid-cols-2">
                <DetailValue label="UM dataset ID" value={dataset.um_dataset_id} mono />
                <DetailValue label="Year" value={dataset.year} />
                <DetailValue label="Repository" value={dataset.repository} />
                <DetailValue label="DOI" value={dataset.doi} />
                <DetailValue label="Created" value={formatDate(dataset.created_at)} />
                <DetailValue label="Last updated" value={formatDate(dataset.updated_at)} />
              </div>
              {dataset.url ? <Button asChild variant="outline"><a href={dataset.url} target="_blank" rel="noreferrer"><RiExternalLinkLine data-icon="inline-start" />Open source record</a></Button> : null}
              <DetailList label="Creators" values={dataset.creators} />
              <DetailList label="Aliases" values={dataset.aliases} />
              <DetailList label="Keywords" values={dataset.keywords} />
              <div className="rounded-lg border p-4"><p className="mb-2 text-xs font-medium uppercase text-muted-foreground">Raw metadata</p><pre className="max-h-[520px] overflow-auto whitespace-pre-wrap break-words rounded-md bg-muted/50 p-3 font-mono text-xs [overflow-wrap:anywhere]">{JSON.stringify(dataset.raw, null, 2)}</pre></div>
            </div>
          ) : null}
        </ScrollArea>
      </SheetContent>
    </Sheet>
  );
}

function DetailValue({ label, value, mono = false }: { label: string; value: string | number | null | undefined; mono?: boolean }) {
  return <div className="min-w-0"><p className="text-xs font-medium uppercase text-muted-foreground">{label}</p><p className={`mt-1 break-words text-sm ${mono ? "font-mono" : ""}`}>{value ?? "—"}</p></div>;
}

function DetailList({ label, values }: { label: string; values: string[] }) {
  return <div className="rounded-lg border p-4"><p className="mb-2 text-xs font-medium uppercase text-muted-foreground">{label}</p>{values.length ? <div className="flex flex-wrap gap-2">{values.map((value) => <Badge key={value} variant="outline" className="h-auto whitespace-normal text-left">{value}</Badge>)}</div> : <p className="text-sm text-muted-foreground">None recorded</p>}</div>;
}

function CatalogSkeleton() {
  return <div className="grid gap-2">{Array.from({ length: 5 }, (_, index) => <Skeleton key={index} className="h-12 w-full" />)}</div>;
}

function formatDate(value: string | null) {
  if (!value) return "Not recorded";
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(value));
}
