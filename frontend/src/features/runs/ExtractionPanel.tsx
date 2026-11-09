import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { api, isActiveRun, type ExtractionEvidence, type Observation, type PipelineRunSummary } from "@/shared/api/client";

const label = (value: string) => value.replaceAll("_", " ");
const errorText = (error: unknown) => error instanceof Error ? error.message : "Request failed";

function EvidenceLinks({ ids }: { ids: string[] }) {
  return <span className="inline-flex flex-wrap gap-2">{ids.map(id => <a key={id} className="text-xs underline underline-offset-2" href={`#evidence-${id}`}>{id}</a>)}</span>;
}

function Observations({ values }: { values: Record<string, Observation> }) {
  return <dl className="divide-y text-sm">{Object.entries(values).map(([name, observed]) => <div key={name} className="py-2">
    <dt className="font-medium capitalize">{label(name)}</dt>
    <dd className="mt-1 space-y-1 break-words text-muted-foreground">
      {observed.state === "not_reported" ? "Not reported" : <>
        {observed.state !== "reported" ? <Badge variant="outline">{label(observed.state)}</Badge> : null}
        {observed.values.map((item, index) => <div key={index}>
          <p>{typeof item.value === "object" ? JSON.stringify(item.value) : String(item.value)}</p>
          <EvidenceLinks ids={item.evidence_ids} />
        </div>)}
      </>}
    </dd>
  </div>)}</dl>;
}

function Passage({ evidence }: { evidence: ExtractionEvidence }) {
  return <div id={`evidence-${evidence.evidence_id}`} className="scroll-mt-4 space-y-1 border-t py-3">
    <p className="text-xs text-muted-foreground">{evidence.evidence_id} · {evidence.section_heading || evidence.placement} · characters {evidence.char_start}–{evidence.char_end}</p>
    <blockquote className="whitespace-pre-wrap break-words text-sm">{evidence.quote}</blockquote>
  </div>;
}

export function ExtractionPanel({ run }: { run: PipelineRunSummary }) {
  const cache = useQueryClient();
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<number | null>(null);
  const poll = isActiveRun(run.status) || run.status === "interrupted" ? 5000 : false;
  const summaries = useQuery({ queryKey: ["extractions", run.id, offset], queryFn: () => api.extractions(run.id, offset), refetchInterval: poll });
  const detail = useQuery({ queryKey: ["extraction", run.id, selected], queryFn: () => api.extraction(run.id, selected!), enabled: selected !== null, refetchInterval: poll });
  const status = useQuery({ queryKey: ["llm-status"], queryFn: api.llmStatus, staleTime: 30000, retry: false });
  const resume = useMutation({ mutationFn: () => api.resumeExtractions(run.id), onSuccess: () => {
    void cache.invalidateQueries({ queryKey: ["run", run.id] });
    void cache.invalidateQueries({ queryKey: ["runs"] });
    void cache.invalidateQueries({ queryKey: ["extractions", run.id] });
    void cache.invalidateQueries({ queryKey: ["run-insights", run.id] });
  } });
  const data = detail.data;
  return <section aria-labelledby="extraction-heading" className="mt-5 overflow-hidden rounded-xl border bg-card">
    <div className="flex flex-wrap items-start justify-between gap-3 border-b p-4">
      <div><h2 id="extraction-heading" className="font-heading font-medium">Dataset extraction</h2>
        <p className="mt-1 text-xs text-muted-foreground">Publication evidence for dataset identity and UM catalogue matching.</p>
        {status.data ? <p className="mt-1 text-xs text-muted-foreground">{status.data.message}</p> : null}
      </div>
      <div className="flex flex-wrap gap-2"><Button size="sm" variant="outline" disabled={status.isFetching} onClick={() => void status.refetch()}>Check model</Button>
      {!isActiveRun(run.status) ? <Button size="sm" variant="outline" disabled={resume.isPending || status.data?.available === false} onClick={() => resume.mutate()}>
        {resume.isPending ? "Queueing…" : "Resume extraction"}
      </Button> : null}</div>
    </div>
    {resume.isError ? <Alert variant="destructive"><AlertDescription>{errorText(resume.error)}</AlertDescription></Alert> : null}
    {summaries.isError ? <Alert variant="destructive"><AlertDescription>Extraction outcomes could not be loaded: {errorText(summaries.error)}</AlertDescription></Alert> : null}
    {summaries.isPending ? <p role="status" className="p-4 text-sm text-muted-foreground">Loading extraction outcomes…</p> : null}
    {summaries.data ? <>
      <div className="overflow-x-auto"><Table><TableHeader><TableRow><TableHead>Publication</TableHead><TableHead>Extraction</TableHead><TableHead>Datasets</TableHead></TableRow></TableHeader>
        <TableBody>{summaries.data.items.map(item => <TableRow key={item.paper_id}>
          <TableCell className="min-w-56 max-w-md whitespace-normal">
            {item.id !== null ? <Button variant="link" className="h-auto max-w-full justify-start whitespace-normal p-0 text-left" onClick={() => setSelected(item.id)}>{item.title}</Button> : item.title}
            <p className="mt-1 text-xs text-muted-foreground">{item.paper_id}</p>
            {item.error ? <p className="mt-1 text-xs text-destructive">{item.error}</p> : null}
          </TableCell>
          <TableCell><Badge variant={item.status === "failed" ? "destructive" : "outline"}>{item.status === "successful" ? item.dataset_count ? "Completed with datasets" : "Completed · zero datasets" : label(item.status)}</Badge>
            {item.progress ? <p className="mt-1 text-xs text-muted-foreground">{label(item.progress.phase)} · {item.progress.attempts_this_invocation}/{item.progress.call_limit} calls · {Math.round(item.progress.elapsed_seconds)}s / {item.progress.time_limit_seconds}s</p> : null}
            {item.error_category === 'extraction_budget_exceeded' ? <p className="text-xs">Processing budget reached. Saved checkpoints can be resumed.</p> : null}
          </TableCell>
          <TableCell>{item.status === "successful" ? item.dataset_count : "—"}</TableCell>
        </TableRow>)}</TableBody></Table></div>
      {!summaries.data.items.length ? <p className="p-4 text-sm text-muted-foreground">No selected publications.</p> : null}
      <div className="flex items-center justify-between gap-2 border-t p-3 text-xs text-muted-foreground">
        <span>{summaries.data.total} publications</span><div className="flex gap-2">
          <Button size="sm" variant="outline" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 25))}>Previous</Button>
          <Button size="sm" variant="outline" disabled={offset + 25 >= summaries.data.total} onClick={() => setOffset(offset + 25)}>Next</Button>
        </div>
      </div>
    </> : null}
    <Sheet open={selected !== null} onOpenChange={open => { if (!open) setSelected(null); }}>
      <SheetContent className="overflow-y-auto sm:max-w-2xl">
        <SheetHeader><SheetTitle>Extraction evidence</SheetTitle><SheetDescription>Exact publication passages and reported dataset features. Reported claims have not been externally verified.</SheetDescription></SheetHeader>
        <div className="space-y-5 px-4 pb-6">
          {detail.isPending ? <p role="status">Loading evidence…</p> : null}
          {detail.isError ? <Alert variant="destructive"><AlertDescription>{errorText(detail.error)}</AlertDescription></Alert> : null}
          {data?.error ? <Alert variant="destructive"><AlertDescription>{data.error}</AlertDescription></Alert> : null}
          {data ? <details className="rounded-lg border p-3 text-xs"><summary className="cursor-pointer font-medium">Provenance and processing</summary>
            <dl className="mt-2 space-y-2 break-all"><dt>Status</dt><dd>{data.status}</dd><dt>Source SHA-256</dt><dd>{data.source_sha256 || "Source not prepared"}</dd>
              <dt>Model and extraction versions</dt><dd>{JSON.stringify(data.config)}</dd><dt>Coverage</dt><dd>{JSON.stringify(data.coverage)}</dd><dt>Requests</dt><dd>{data.requests.length}</dd></dl>
            {data.requests.filter(request => request.error).map(request => <p key={request.work_key} className="mt-2 text-destructive">{request.status}: {request.error}</p>)}
          </details> : null}
          {data?.result?.datasets.map(dataset => <article key={dataset.dataset_local_id} className="space-y-3 border-t pt-4">
            <h3 className="font-heading font-medium">{dataset.display_name}</h3>
            {dataset.review_reasons.length ? <Alert><AlertDescription>{dataset.review_reasons.join("; ")}</AlertDescription></Alert> : null}
            {(["identity", "use"] as const).map(group => <details key={group} className="border-b pb-2"><summary className="cursor-pointer text-sm font-medium capitalize">{label(group)}</summary><Observations values={dataset[group]} /></details>)}
            <details><summary className="cursor-pointer text-sm font-medium">Reference occurrences ({dataset.occurrences.length})</summary>
              {dataset.occurrences.map(occurrence => <div key={occurrence.occurrence_id} className="mt-3 space-y-2 border-t pt-3">
                <p className="text-xs">{occurrence.bibliography_only ? "Bibliography only · does not establish reuse" : occurrence.occurrence_id}</p><EvidenceLinks ids={occurrence.evidence_ids} />
                <Observations values={occurrence.use} /><Observations values={occurrence.citation} />
                {occurrence.citation_links.map((link, index) => <p key={index} className="break-words text-xs">[{link.marker}] · {link.status} · {link.raw_reference || "Reference unresolved"}</p>)}
              </div>)}
            </details>
          </article>)}
          {data?.result && data.result.datasets.length === 0 ? <p className="text-sm">Complete extraction found no identifiable datasets.</p> : null}
          {data?.result?.unresolved_mentions.length ? <div><h3 className="font-medium">Ambiguous passages for review</h3><EvidenceLinks ids={data.result.unresolved_mentions} /></div> : null}
          {data?.result ? <div><h3 className="font-heading font-medium">Source passages</h3>{data.result.evidence.map(evidence => <Passage key={evidence.evidence_id} evidence={evidence} />)}</div> : null}
        </div>
      </SheetContent>
    </Sheet>
  </section>;
}
