import { useMutation, useQuery } from "@tanstack/react-query";
import { SegmentedControl } from "glisse";
import {
  RiArrowDownSLine,
  RiCheckLine,
  RiDatabase2Line,
  RiDownload2Line,
  RiErrorWarningLine,
  RiExternalLinkLine,
  RiInformationLine,
  RiLoader4Line,
  RiPlayLine,
  RiRouteLine,
  RiSearchLine,
  RiShieldLine,
  RiShuffleLine,
  RiStopCircleLine,
} from "@remixicon/react";
import { useEffect, useId, useMemo, useState, type ComponentProps, type FormEvent, type ReactNode } from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { Switch } from "@/components/ui/switch";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { ApiError, api, type DiscoveryCandidate, type DiscoveryPreviewRequest } from "@/shared/api/client";

import { useLaunchJob } from "./LaunchJobProvider";

const DEFAULT_OUTPUT = "storage/exports/insights.csv";
const DEFAULT_UM_DATASETS = "data/um_dataset";
type DiscoveryMode = "catalog_funnel" | "random" | "manual";

export default function DiscoveryLaunchPage() {
  const launchJob = useLaunchJob();
  const [mode, setMode] = useState<DiscoveryMode>("catalog_funnel");
  const [focusQuery, setFocusQuery] = useState("");
  const [manualQuery, setManualQuery] = useState("");
  const [randomSeed, setRandomSeed] = useState("");
  const [publicationTypes, setPublicationTypes] = useState("");
  const [language, setLanguage] = useState("en");
  const [discoveryLimit, setDiscoveryLimit] = useState(500);
  const [processingLimit, setProcessingLimit] = useState(50);
  const [fromYear, setFromYear] = useState("");
  const [toYear, setToYear] = useState("");
  const [maxCost, setMaxCost] = useState(0.25);
  const [outputPath, setOutputPath] = useState(DEFAULT_OUTPUT);
  const [umDatasetsPath, setUmDatasetsPath] = useState(DEFAULT_UM_DATASETS);
  const [overwrite, setOverwrite] = useState(false);
  const [highThroughput, setHighThroughput] = useState(false);
  const [excludedIds, setExcludedIds] = useState<Set<string>>(new Set());
  const [selectedCandidate, setSelectedCandidate] = useState<DiscoveryCandidate | null>(null);

  const profileQuery = useQuery({
    queryKey: ["um-discovery-profile"],
    queryFn: api.umDiscoveryProfile,
    staleTime: 60_000,
  });
  const statusQuery = useQuery({
    queryKey: ["openalex-status"],
    queryFn: api.openAlexStatus,
    staleTime: 30_000,
    retry: false,
  });
  const previewMutation = useMutation({
    mutationFn: (request: DiscoveryPreviewRequest) => api.discoveryPreview(request),
    onSuccess: () => setExcludedIds(new Set()),
  });
  useEffect(() => {
    setProcessingLimit((current) => Math.min(current, discoveryLimit));
  }, [discoveryLimit]);

  const previewRequest = useMemo<DiscoveryPreviewRequest>(
    () => ({
      strategy_version: 3,
      mode,
      focus_query: mode === "catalog_funnel" ? focusQuery.trim() : "",
      manual_query: mode === "manual" ? manualQuery.trim() || null : null,
      random_seed: mode === "random" ? optionalNumber(randomSeed) : null,
      from_year: optionalNumber(fromYear),
      to_year: optionalNumber(toYear),
      publication_types: splitTerms(publicationTypes),
      language: language.trim().toLowerCase() || null,
      discovery_limit: discoveryLimit,
      processing_limit: processingLimit,
      max_cost_usd: maxCost,
    }),
    [discoveryLimit, focusQuery, fromYear, language, manualQuery, maxCost, mode, processingLimit, publicationTypes, randomSeed, toYear],
  );
  const previewIsCurrent =
    previewMutation.data !== undefined &&
    JSON.stringify(previewMutation.variables) === JSON.stringify(previewRequest);
  const preview = previewIsCurrent ? previewMutation.data : undefined;
  const queuedCandidates = preview?.candidates
    .filter((candidate) => candidate.pipeline_ready && !excludedIds.has(candidate.paper_id))
    .slice(0, processingLimit) ?? [];
  const profile = profileQuery.data;
  const openAlex = statusQuery.data;
  const blocked = !openAlex?.available || (mode === "catalog_funnel" && !profile?.dataset_count);
  const manualMissing = mode === "manual" && !manualQuery.trim();
  const estimatedCalls = profile?.phases?.reduce((sum, phase) => sum + phase.estimated_calls, 0);
  const estimatedCost = profile?.phases?.reduce((sum, phase) => sum + phase.estimated_cost_usd, 0);
  const simpleModeCalls = Math.ceil(discoveryLimit / 100);

  function chooseMode(nextMode: DiscoveryMode) {
    setMode(nextMode);
    previewMutation.reset();
  }

  function toggleCandidate(candidate: DiscoveryCandidate, include: boolean) {
    setExcludedIds((current) => {
      const next = new Set(current);
      if (include) next.delete(candidate.paper_id);
      else next.add(candidate.paper_id);
      return next;
    });
  }

  function launch(previewId: string, excluded: string[]) {
    launchJob.start({
      preview: preview?.preview_id === previewId ? preview : undefined,
      previewRequest,
      excludedCandidateIds: excluded,
      runRequest: {
        processing_limit: processingLimit,
        overwrite,
        um_datasets_path: umDatasetsPath.trim() || null,
        output_path: outputPath.trim() || DEFAULT_OUTPUT,
        strategy: highThroughput ? "high_throughput" : "standard",
        render_profile: "pruned",
      },
    });
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (preview) {
      launch(preview.preview_id, [...excludedIds]);
      return;
    }
    launchJob.start({
      preview: undefined,
      previewRequest,
      excludedCandidateIds: [],
      runRequest: {
        processing_limit: processingLimit,
        overwrite,
        um_datasets_path: umDatasetsPath.trim() || null,
        output_path: outputPath.trim() || DEFAULT_OUTPUT,
        strategy: highThroughput ? "high_throughput" : "standard",
        render_profile: "pruned",
      },
    });
  }

  return (
    <form className="@container mx-auto flex w-full min-w-0 max-w-6xl flex-col gap-6 pb-16" onSubmit={submit}>
      <section className="grid gap-3" aria-labelledby="strategy-heading">
        <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
          <h2 id="strategy-heading" className="text-2xl font-semibold tracking-tight @sm:text-[1.75rem]">Discovery strategy</h2>
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
            {openAlex?.available ? <span className="inline-flex items-center gap-1 text-status-success"><RiCheckLine className="size-3.5" aria-hidden="true" /> OpenAlex ready</span> : null}
            {statusQuery.isPending ? <span>Checking OpenAlex…</span> : null}
            {profile?.dataset_count ? <span>{profile.dataset_count.toLocaleString()} UM datasets</span> : null}
          </div>
        </div>
        <StrategySelector mode={mode} onChange={chooseMode} />
      </section>

      {statusQuery.isError || (openAlex && !openAlex.available) ? (
        <Alert variant="destructive">
          <RiShieldLine />
          <AlertTitle>OpenAlex connection needs attention</AlertTitle>
          <AlertDescription>{openAlex?.message ?? "The OpenAlex status could not be checked."}</AlertDescription>
        </Alert>
      ) : null}
      {mode === "catalog_funnel" && !profileQuery.isPending && !profile?.dataset_count ? (
        <Alert variant="destructive">
          <RiDatabase2Line />
          <AlertTitle>UM catalog unavailable</AlertTitle>
          <AlertDescription>{profileQuery.isError ? "The UM catalog could not be loaded." : "Import UM datasets to use Smart discovery."}</AlertDescription>
        </Alert>
      ) : null}

      <div className="grid items-start gap-6 border-t pt-5 @4xl:grid-cols-[minmax(0,1fr)_18rem] @4xl:gap-8">
        <div className="grid min-w-0 gap-5">
          <section aria-labelledby="scope-heading" className="grid gap-3">
            <h2 id="scope-heading" className="text-base font-semibold">Search settings</h2>
            <div className="grid gap-x-4 gap-y-3 @xs:grid-cols-2">
              <LabeledInput
                label={mode === "random" ? "Random sample size" : "Candidate pool cap"}
                hint="Eligible publications to collect after skipping previously processed papers. Includes papers without PDF links."
                type="number" min={1} max={1000} value={discoveryLimit} onChange={(event) => setDiscoveryLimit(Number(event.target.value))}
              />
              <LabeledInput
                label="Process now"
                hint="Target number of validated PDFs. Failed downloads are replaced by eligible papers from the reviewed pool."
                type="number" min={1} max={discoveryLimit} value={processingLimit} onChange={(event) => setProcessingLimit(Math.min(Number(event.target.value), discoveryLimit))}
              />
              {mode === "catalog_funnel" ? <LabeledInput className="@xs:col-span-2" label="Optional focus" hint="Applies to the final topic and keyword search." placeholder="e.g. health, education, FAIR data" value={focusQuery} onChange={(event) => setFocusQuery(event.target.value)} /> : null}
              {mode === "random" ? <LabeledInput className="@xs:col-span-2" label="Sample seed" hint="Reuse a seed to reproduce the same sample." type="number" min={1} max={2147483647} placeholder="Generate automatically" value={randomSeed} onChange={(event) => setRandomSeed(event.target.value)} /> : null}
              {mode === "manual" ? <LabeledInput className="@xs:col-span-2" label="Manual query" hint="Results are classified as Expanded evidence." required placeholder="e.g. longitudinal cohort data reuse" value={manualQuery} onChange={(event) => setManualQuery(event.target.value)} /> : null}
              <LabeledInput label="Publication types" hint="Enter comma-separated OpenAlex work types." placeholder="article, preprint" value={publicationTypes} onChange={(event) => setPublicationTypes(event.target.value)} />
              <LabeledInput label="Language" hint="Enter an OpenAlex language code. Clear the field to include every language." placeholder="en" maxLength={3} value={language} onChange={(event) => setLanguage(event.target.value)} />
              <LabeledInput label="From year" type="number" min={1800} max={3000} value={fromYear} onChange={(event) => setFromYear(event.target.value)} />
              <LabeledInput label="To year" type="number" min={1800} max={3000} value={toYear} onChange={(event) => setToYear(event.target.value)} />
            </div>
          </section>

          <div className="divide-y border-y">
            <AdvancedSettings
              highThroughput={highThroughput}
              maxCost={maxCost}
              outputPath={outputPath}
              overwrite={overwrite}
              umDatasetsPath={umDatasetsPath}
              onHighThroughput={setHighThroughput}
              onMaxCost={setMaxCost}
              onOutputPath={setOutputPath}
              onOverwrite={setOverwrite}
              onUmDatasetsPath={setUmDatasetsPath}
            />
            {mode === "catalog_funnel" && profile?.phases?.length ? (
              <SettingsDisclosure label="Smart discovery stages">
                <p className="text-sm text-muted-foreground">Discovery skips works listed in the UM catalog and likely source papers. It follows citations to find candidates for dataset reuse.</p>
                <ol className="mt-3 divide-y text-sm">
                  {profile.phases.map((phase) => (
                    <li key={phase.id} className="grid gap-x-3 gap-y-1 py-2 @sm:grid-cols-[minmax(0,1fr)_auto]">
                      <span className="font-medium">{phase.label}</span>
                      <span className="text-xs tabular-nums text-muted-foreground">
                        {phase.id === "source_citations" ? "Depends on source papers found" : `${phase.estimated_calls} calls · ≈ $${phase.estimated_cost_usd.toFixed(4)}`}
                      </span>
                      <span className="text-muted-foreground @sm:col-span-2">{phase.description}</span>
                    </li>
                  ))}
                </ol>
              </SettingsDisclosure>
            ) : null}
          </div>
        </div>

        <StrategySummary
          blocked={blocked || manualMissing}
          estimatedCalls={mode === "catalog_funnel" ? estimatedCalls : simpleModeCalls}
          estimatedCost={mode === "catalog_funnel" ? estimatedCost : simpleModeCalls * (mode === "manual" ? 0.001 : 0.0001)}
          queued={queuedCandidates.length}
          loading={previewMutation.isPending}
          preview={preview}
          mode={mode}
          onPreview={() => previewMutation.mutate(previewRequest)}
        />
      </div>

      {previewMutation.isError ? <PreviewError error={previewMutation.error} /> : null}
      {preview ? (
        <>
          <PhaseResults mode={mode} preview={preview} />
          <CandidatePreview mode={mode} previewId={preview.preview_id} candidates={preview.candidates} queuedCandidates={queuedCandidates} excludedIds={excludedIds} onSelect={setSelectedCandidate} onToggle={toggleCandidate} total={preview.candidate_count} />
        </>
      ) : null}

      <div className="app-glass pointer-events-none fixed inset-x-0 bottom-0 z-30 flex justify-end gap-2 border-t px-3 py-2 @4xl:static @4xl:border-0 @4xl:bg-transparent @4xl:bg-none @4xl:p-0 @4xl:shadow-none @4xl:backdrop-filter-none">
        {launchJob.phase === "discovering" ? (
          <Button className="pointer-events-auto h-10 rounded-full px-4" type="button" variant="outline" onClick={launchJob.cancel}>
            <RiStopCircleLine data-icon="inline-start" aria-hidden="true" />
            Cancel launch
          </Button>
        ) : null}
        <Button className="launch-primary pointer-events-auto h-10 min-w-36 rounded-full px-4" type="submit" disabled={launchJob.isPending || previewMutation.isPending || blocked || manualMissing}>
          {launchJob.isPending ? <RiLoader4Line className="animate-spin" data-icon="inline-start" aria-hidden="true" /> : <RiPlayLine data-icon="inline-start" />}
          {launchJob.phase === "starting" ? "Opening workspace…" : launchJob.phase === "discovering" ? mode === "random" ? "Drawing sample…" : "Preparing discovery…" : previewMutation.isPending ? mode === "random" ? "Drawing sample…" : "Preparing preview…" : "Run pipeline"}
        </Button>
      </div>

      {launchJob.error ? <PreviewError error={launchJob.error} /> : null}
      <CandidateSheet mode={mode} candidate={selectedCandidate} onClose={() => setSelectedCandidate(null)} />
    </form>
  );
}

function StrategySelector({ mode, onChange }: { mode: DiscoveryMode; onChange: (mode: DiscoveryMode) => void }) {
  const options: Array<{ mode: DiscoveryMode; label: string; description: string; icon: typeof RiRouteLine }> = [
    { mode: "catalog_funnel", label: "Smart", description: "Prioritize publications with evidence of a link to UM datasets.", icon: RiRouteLine },
    { mode: "random", label: "Random", description: "Draw a random sample from OpenAlex works that match your filters.", icon: RiShuffleLine },
    { mode: "manual", label: "Manual", description: "Search OpenAlex with a query you provide.", icon: RiSearchLine },
  ];
  return (
    <div className="grid justify-items-start gap-2">
      <SegmentedControl
        aria-labelledby="strategy-heading"
        className="strategy-control"
        value={mode}
        items={options.map(({ mode: value, label, icon: Icon }) => ({
          value,
          label: <span className="inline-flex translate-y-[3px] items-center gap-1 text-base leading-none"><Icon className="size-4" aria-hidden="true" />{label}</span>,
        }))}
        onValueChange={(value) => {
          const option = options.find((item) => item.mode === value);
          if (option && option.mode !== mode) onChange(option.mode);
        }}
      />
      <p className="text-center text-[0.9375rem] leading-6 text-muted-foreground" aria-live="polite">{options.find((option) => option.mode === mode)?.description}</p>
    </div>
  );
}

function StrategySummary({ blocked, estimatedCalls, estimatedCost, queued, loading, mode, preview, onPreview }: { blocked: boolean; estimatedCalls: number | undefined; estimatedCost: number | undefined; queued: number; loading: boolean; mode: DiscoveryMode; preview: Awaited<ReturnType<typeof api.discoveryPreview>> | undefined; onPreview: () => void }) {
  return (
    <section aria-labelledby="preview-heading" className="activity-surface grid gap-4 bg-card p-5 text-card-foreground @4xl:sticky @4xl:top-22">
      <div className="flex items-center gap-1">
        <h2 id="preview-heading" className="text-base font-semibold">Preview</h2>
        <HelpHint label="Preview" text={mode === "random" ? "Previewing uses OpenAlex credits. Leaving the seed blank draws a fresh sample." : "Previewing uses OpenAlex credits and leaves PDF processing for the pipeline run."} />
      </div>
      <div className="grid gap-2 text-sm tabular-nums">
        <dl className="grid grid-cols-[minmax(0,1fr)_auto] items-end gap-3">
          <div>
            <dt className="text-xs text-muted-foreground">Estimated OpenAlex cost</dt>
            <dd className="text-[1.75rem] leading-tight font-medium tracking-tight text-brand-ink">{estimatedCost === undefined ? "—" : `$${estimatedCost.toFixed(3)}`}</dd>
          </div>
          <div className="border-l pl-3">
            <dt className="text-xs text-muted-foreground">Est. API calls</dt>
            <dd className="text-lg font-medium text-blue-ink">{estimatedCalls?.toLocaleString() ?? "—"}</dd>
          </div>
        </dl>
        {preview ? (
          <div className="grid gap-2 border-t pt-2">
            <SummaryLine label="Candidates retained" value={String(preview.candidate_count)} />
            {Number(preview.metrics.unique_processed_works_skipped) > 0 ? <SummaryLine label="Previously processed papers skipped" value={String(preview.metrics.unique_processed_works_skipped)} /> : null}
            <SummaryLine label="Candidates with PDF URL" value={String(preview.ready_count)} />
            <SummaryLine label="Processing queue" value={String(queued)} />
            <SummaryLine label="No-PDF-URL watchlist" value={String(preview.watchlist_count)} />
            <SummaryLine label="Stopped because" value={formatText(preview.stop_reason)} />
            <SummaryLine label="Actual cost" value={`$${preview.actual_cost_usd.toFixed(4)}`} />
            {preview.random_seed ? <SummaryLine label="Random seed" value={String(preview.random_seed)} /> : null}
          </div>
        ) : null}
      </div>
      {preview?.warnings.length ? (
        <ul className="list-disc space-y-1 pl-4 text-xs text-status-warning">
          {preview.warnings.map((warning) => <li key={warning}>{warning}</li>)}
        </ul>
      ) : null}
      <Button type="button" variant="ghost" className="app-glass rounded-full border-(--glass-edge) text-blue-ink" onClick={onPreview} disabled={blocked || loading}>
        {loading ? "Previewing results…" : preview ? "Refresh results" : "Preview results"}
      </Button>
    </section>
  );
}

function PhaseResults({ mode, preview }: { mode: DiscoveryMode; preview: Awaited<ReturnType<typeof api.discoveryPreview>> }) {
  const title = mode === "random" ? "Sampling execution" : mode === "manual" ? "Query execution" : "Funnel execution";
  const description = mode === "random" ? "Seeded draws fill the pool with eligible papers that match your filters." : mode === "manual" ? "Discovery pages through the query results to fill the eligible candidate pool." : "Later phases run when the eligible candidate pool still has space.";
  return (
    <Card>
      <CardHeader><CardTitle>{title}</CardTitle><CardDescription>{description}</CardDescription></CardHeader>
      <CardContent className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        {Object.entries(preview.phase_results).map(([phase, result]) => (
          <div key={phase} className="rounded-lg border p-3 text-sm">
            <div className="flex items-center justify-between gap-2"><p className="font-medium capitalize">{formatText(phase)}</p><Badge variant={result.status === "completed" ? "secondary" : "outline"}>{String(result.status)}</Badge></div>
            <div className="mt-3 grid gap-1 text-xs text-muted-foreground">
              <SummaryLine label="Unique added" value={String(result.unique_added ?? 0)} />
              {Number(result.catalog_hits_skipped) > 0 && <SummaryLine label="Catalog or source-paper hits skipped" value={String(result.catalog_hits_skipped)} />}
              <SummaryLine label="Ready afterward" value={String(result.ready_after_phase ?? 0)} />
              <SummaryLine label="Calls" value={String(result.calls ?? 0)} />
            </div>
          </div>
        ))}
      </CardContent>
    </Card>
  );
}

function CandidatePreview({ mode, previewId, candidates, queuedCandidates, excludedIds, onSelect, onToggle, total }: { mode: DiscoveryMode; previewId: string; candidates: DiscoveryCandidate[]; queuedCandidates: DiscoveryCandidate[]; excludedIds: Set<string>; onSelect: (candidate: DiscoveryCandidate) => void; onToggle: (candidate: DiscoveryCandidate, include: boolean) => void; total: number }) {
  const queuedIds = new Set(queuedCandidates.map((candidate) => candidate.paper_id));
  return (
    <Card className="min-w-0">
      <CardHeader className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0"><CardTitle>Candidate review</CardTitle><CardDescription>{mode === "random" ? `${total} eligible publications retained in seeded random order. Random inclusion is not evidence of dataset use.` : `${total} candidates ranked by evidence—not predicted reuse probability.`}</CardDescription></div>
        <Button type="button" variant="outline" size="sm" className="shrink-0" disabled={!queuedCandidates.length} onClick={() => downloadCandidatePreviewCsv(previewId, mode, queuedCandidates)}><RiDownload2Line data-icon="inline-start" />Download selected CSV</Button>
      </CardHeader>
      <CardContent className="overflow-x-auto">
        <Table className="min-w-[940px]"><TableHeader><TableRow><TableHead>Include</TableHead><TableHead>Paper</TableHead><TableHead>{mode === "random" ? "Sampling basis" : "Evidence tier"}</TableHead><TableHead>Strength</TableHead><TableHead>{mode === "random" ? "Selection reason" : "Evidence"}</TableHead><TableHead>Access</TableHead></TableRow></TableHeader>
          <TableBody>{candidates.map((candidate) => {
            const included = queuedIds.has(candidate.paper_id);
            const eligible = candidate.pipeline_ready && !excludedIds.has(candidate.paper_id);
            return <TableRow key={candidate.paper_id}>
              <TableCell>
                <Switch aria-label={`Include ${candidate.title ?? candidate.paper_id}`} checked={eligible} disabled={!candidate.pipeline_ready} onCheckedChange={(checked) => onToggle(candidate, checked)} />
                {eligible && !included ? <span className="ml-2 text-xs">Reserve</span> : null}
              </TableCell>
              <TableCell className="max-w-[340px]"><button type="button" className="block w-full text-left" onClick={() => onSelect(candidate)}><span className="block truncate font-medium hover:underline">{candidate.title || "Untitled OpenAlex work"}</span><span className="block font-mono text-xs text-muted-foreground">{candidate.paper_id}</span></button></TableCell>
              <TableCell><Badge variant={candidate.evidence_tier === "direct" ? "secondary" : "outline"} className="capitalize">{mode === "random" ? "Random" : candidate.evidence_tier}</Badge></TableCell>
              <TableCell>{mode === "random" ? "Not scored" : `${Math.round(candidate.candidate_strength)}/100`}</TableCell>
              <TableCell className="max-w-[260px]"><div className="flex flex-wrap gap-1">{candidate.evidence_reasons.slice(0, 3).map((reason) => <Badge key={reason} variant="outline" className="capitalize">{formatText(reason)}</Badge>)}</div></TableCell>
              <TableCell><Badge variant={candidate.pipeline_ready ? "secondary" : "outline"}>{candidate.pipeline_ready ? "PDF URL found" : "Watchlist"}</Badge></TableCell>
            </TableRow>;
          })}</TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}

function CandidateSheet({ mode, candidate, onClose }: { mode: DiscoveryMode; candidate: DiscoveryCandidate | null; onClose: () => void }) {
  return <Sheet open={candidate !== null} onOpenChange={(open) => !open && onClose()}><SheetContent side="right" className="w-[94vw] max-w-[calc(100vw-1rem)] sm:max-w-xl"><SheetHeader><SheetTitle>{candidate?.title ?? "Candidate evidence"}</SheetTitle><SheetDescription>{candidate?.paper_id}</SheetDescription></SheetHeader>{candidate ? <div className="grid gap-4 overflow-y-auto px-4 pb-5"><div className="grid grid-cols-2 gap-3 rounded-lg border p-4"><Detail label={mode === "random" ? "Sampling basis" : "Evidence tier"} value={mode === "random" ? "Random" : candidate.evidence_tier} /><Detail label="Candidate strength" value={mode === "random" ? "Not scored" : `${Math.round(candidate.candidate_strength)}/100`} /><Detail label="Discovery readiness" value={candidate.pipeline_ready ? "PDF URL found; not yet validated" : "No-PDF-URL watchlist"} /><Detail label="Citations" value={candidate.cited_by_count ?? "—"} /></div><div className="rounded-lg border p-4"><p className="text-xs font-medium uppercase text-muted-foreground">{mode === "random" ? "Selection reason" : "Discovery evidence"}</p><div className="mt-2 flex flex-wrap gap-2">{candidate.evidence_reasons.map((reason) => <Badge key={reason} variant="outline">{formatText(reason)}</Badge>)}</div></div><div className="rounded-lg border p-4"><p className="text-xs font-medium uppercase text-muted-foreground">UM dataset IDs found in the publication metadata</p><p className="mt-2 break-words font-mono text-sm">{candidate.matched_um_dataset_ids.join(", ") || "No dataset ID can be safely attributed from the OpenAlex response."}</p></div>{candidate.source_url ? <Button asChild variant="outline"><a href={candidate.source_url} target="_blank" rel="noreferrer"><RiExternalLinkLine data-icon="inline-start" />Open in OpenAlex</a></Button> : null}</div> : null}</SheetContent></Sheet>;
}

function AdvancedSettings({ highThroughput, maxCost, outputPath, overwrite, umDatasetsPath, onHighThroughput, onMaxCost, onOutputPath, onOverwrite, onUmDatasetsPath }: { highThroughput: boolean; maxCost: number; outputPath: string; overwrite: boolean; umDatasetsPath: string; onHighThroughput: (value: boolean) => void; onMaxCost: (value: number) => void; onOutputPath: (value: string) => void; onOverwrite: (value: boolean) => void; onUmDatasetsPath: (value: string) => void }) {
  return (
    <SettingsDisclosure label="Advanced settings">
      <div className="grid gap-x-4 gap-y-3 @sm:grid-cols-2">
        <LabeledInput label="UM dataset metadata" hint="Catalog source used during pipeline processing." value={umDatasetsPath} onChange={(event) => onUmDatasetsPath(event.target.value)} />
        <LabeledInput label="Output CSV" value={outputPath} onChange={(event) => onOutputPath(event.target.value)} />
        <LabeledInput label="Maximum OpenAlex cost" hint="Spending limit in US dollars." type="number" min={0.01} max={5} step={0.01} value={maxCost} onChange={(event) => onMaxCost(Number(event.target.value))} />
        <div className="grid gap-3 @sm:col-span-2">
          <ToggleRow label="High-throughput mode" description="Process selected papers through run-scoped queues." checked={highThroughput} onCheckedChange={onHighThroughput} />
          <ToggleRow label="Overwrite artifacts" description="Replace existing generated files for matching papers." checked={overwrite} onCheckedChange={onOverwrite} />
        </div>
      </div>
    </SettingsDisclosure>
  );
}

function SettingsDisclosure({ label, children }: { label: string; children: ReactNode }) {
  return (
    <details className="group">
      <summary className="flex min-h-11 cursor-pointer list-none items-center justify-between gap-2 rounded-sm text-sm font-medium hover:bg-muted/35 focus-visible:outline-2 focus-visible:outline-ring [&::-webkit-details-marker]:hidden">
        {label}
        <RiArrowDownSLine className="size-4 text-muted-foreground group-open:rotate-180" aria-hidden="true" />
      </summary>
      <div className="pb-4 pt-1">{children}</div>
    </details>
  );
}

function LabeledInput({ label, hint, className, ...inputProps }: ComponentProps<typeof Input> & { label: string; hint?: string }) {
  const id = useId();
  return (
    <div className={cn("grid min-w-0 gap-1", className)}>
      <div className="flex min-h-6 items-center gap-1">
        <label htmlFor={id} className="text-sm font-medium">{label}</label>
        {hint ? <HelpHint label={label} text={hint} /> : null}
      </div>
      <Input id={id} aria-describedby={hint ? `${id}-hint` : undefined} {...inputProps} />
      {hint ? <span id={`${id}-hint`} hidden>{hint}</span> : null}
    </div>
  );
}

function HelpHint({ label, text }: { label: string; text: string }) {
  const [open, setOpen] = useState(false);
  return (
    <Tooltip open={open} onOpenChange={setOpen} delayDuration={200}>
      <TooltipTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="icon-xs"
          className="text-muted-foreground [&_svg]:size-3.5"
          aria-label={`About ${label}`}
          onClick={(event) => {
            // Keep the hint open on tap after Radix handles the pointer press.
            event.preventDefault();
            setOpen(true);
          }}
        >
          <RiInformationLine aria-hidden="true" />
        </Button>
      </TooltipTrigger>
      <TooltipContent sideOffset={4} collisionPadding={12} className="max-w-64">{text}</TooltipContent>
    </Tooltip>
  );
}

function ToggleRow({ label, description, checked, onCheckedChange }: { label: string; description: string; checked: boolean; onCheckedChange: (checked: boolean) => void }) { return <label className="flex items-start gap-3 rounded-lg border p-3"><Switch aria-label={label} checked={checked} onCheckedChange={onCheckedChange} /><span><span className="block text-sm font-medium">{label}</span><span className="block text-xs text-muted-foreground">{description}</span></span></label>; }
function SummaryLine({ label, value }: { label: string; value: string }) { return <div className="flex justify-between gap-3"><span className="text-muted-foreground">{label}</span><span className="text-right font-medium">{value}</span></div>; }
function Detail({ label, value }: { label: string; value: string | number }) { return <div><p className="text-xs text-muted-foreground">{label}</p><p className="mt-1 font-medium capitalize">{value}</p></div>; }
function PreviewError({ error }: { error: Error }) { return <Alert variant="destructive" aria-live="polite"><RiErrorWarningLine /><AlertTitle>Discovery could not be completed</AlertTitle><AlertDescription>{errorMessage(error)}</AlertDescription></Alert>; }
function errorMessage(error: Error) { if (error instanceof ApiError && typeof error.detail === "object" && error.detail) { const message = (error.detail as { message?: unknown }).message; if (typeof message === "string") return message; } return error.message; }
function optionalNumber(value: string) { const parsed = Number(value); return value.trim() && Number.isFinite(parsed) ? parsed : null; }
function splitTerms(value: string) { return value.split(/[;,|]/).map((term) => term.trim()).filter(Boolean); }
function formatText(value: string) { return value.replaceAll("_", " "); }

function downloadCandidatePreviewCsv(previewId: string, mode: DiscoveryMode, candidates: DiscoveryCandidate[]) {
  const discoveryMethod = mode === "catalog_funnel" ? "Smart discovery" : mode === "random" ? "Random" : "Manual query";
  const rows = [
    "id,title,discovery_method",
    ...candidates.map((candidate) => [candidate.paper_id, candidate.title ?? "", discoveryMethod].map(csvCell).join(",")),
  ];
  const blob = new Blob([`${rows.join("\r\n")}\r\n`], { type: "text/csv;charset=utf-8" });
  const objectUrl = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = objectUrl;
  anchor.download = `datasight-preview-${previewId.replace(/[^a-zA-Z0-9_-]/g, "")}-candidates.csv`;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(objectUrl);
}

function csvCell(value: string) {
  const spreadsheetSafeValue = /^(?:\s*[=+\-@]|[\t\r\n])/.test(value) ? `'${value}` : value;
  return `"${spreadsheetSafeValue.replaceAll('"', '""')}"`;
}
