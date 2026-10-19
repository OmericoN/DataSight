import { useMutation, useQuery } from "@tanstack/react-query";
import {
  RiCheckLine,
  RiDatabase2Line,
  RiDownload2Line,
  RiErrorWarningLine,
  RiExternalLinkLine,
  RiLoader4Line,
  RiPlayLine,
  RiPulseLine,
  RiRouteLine,
  RiSearchLine,
  RiSettings3Line,
  RiShieldLine,
  RiShuffleLine,
  RiStopCircleLine,
} from "@remixicon/react";
import { useEffect, useMemo, useState, type FormEvent, type ReactNode } from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
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
      strategy_version: 2,
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
  const profile = profileQuery.data;
  const openAlex = statusQuery.data;
  const blocked = !openAlex?.available || (mode === "catalog_funnel" && !profile?.dataset_count);
  const manualMissing = mode === "manual" && !manualQuery.trim();
  const estimatedCalls = profile?.phases?.reduce((sum, phase) => sum + phase.estimated_calls, 0) ?? 0;
  const estimatedCost = profile?.phases?.reduce((sum, phase) => sum + phase.estimated_cost_usd, 0) ?? 0;
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
    <form className="mx-auto flex w-full max-w-7xl flex-col gap-5 pb-16 xl:pb-0" onSubmit={submit}>
      <Card>
        <CardHeader>
          <CardTitle id="strategy-heading">Choose a discovery strategy</CardTitle>
          <CardDescription>
            Choose how OpenAlex should build the starting pool. Every option uses the same filters, budget guardrail, candidate review, and downstream confirmation pipeline.
          </CardDescription>
        </CardHeader>
        <CardContent><StrategySelector mode={mode} onChange={chooseMode} /></CardContent>
      </Card>

      {mode === "catalog_funnel" ? (
        <Card>
          <CardHeader><CardTitle>Adaptive funnel stages</CardTitle><CardDescription>Direct evidence runs first; broader stages run only when the PDF-link target is still short. Download validation happens later.</CardDescription></CardHeader>
          <CardContent className="grid gap-3 md:grid-cols-4">
            {profileQuery.isLoading
              ? Array.from({ length: 4 }, (_, index) => <Skeleton key={index} className="h-28 w-full" />)
              : profile?.phases?.map((phase, index) => (
                  <div key={phase.id} className="rounded-lg border p-4">
                    <div className="flex items-center gap-2"><span className="flex size-6 items-center justify-center rounded-full bg-muted text-xs font-medium">{index + 1}</span><p className="font-medium">{phase.label}</p></div>
                    <p className="mt-2 text-sm text-muted-foreground">{phase.description}</p>
                    <p className="mt-3 text-xs text-muted-foreground">{phase.estimated_calls} estimated calls · ≈ ${phase.estimated_cost_usd.toFixed(4)}</p>
                  </div>
                ))}
          </CardContent>
        </Card>
      ) : null}

      <ReadinessStrip mode={mode} processingLimit={processingLimit} profile={profile} loading={profileQuery.isLoading} status={openAlex} />
      {statusQuery.isError || (openAlex && !openAlex.available) ? (
        <Alert variant="destructive">
          <RiShieldLine />
          <AlertTitle>OpenAlex connection needs attention</AlertTitle>
          <AlertDescription>{openAlex?.message ?? "The OpenAlex status could not be checked."}</AlertDescription>
        </Alert>
      ) : null}

      <div className="grid items-start gap-5 xl:grid-cols-[minmax(0,1fr)_360px]">
        <div className="grid gap-5">
          <Card>
            <CardHeader>
              <CardTitle>Scope and capacity</CardTitle>
              <CardDescription>
                The process-now target controls the stop rule. The larger pool retains extra leads and a no-PDF watchlist.
              </CardDescription>
            </CardHeader>
            <CardContent className="grid gap-4 sm:grid-cols-2">
              <LabeledInput label={mode === "random" ? "Random sample size" : "Candidate pool cap"} hint={mode === "random" ? "Works drawn uniformly from OpenAlex before eligibility checks" : "Maximum retained candidates, including watchlist leads"}>
                <Input aria-label={mode === "random" ? "Random sample size" : "Candidate pool cap"} type="number" min={1} max={1000} value={discoveryLimit} onChange={(event) => setDiscoveryLimit(Number(event.target.value))} />
              </LabeledInput>
              <LabeledInput label="Process now" hint={mode === "random" ? "Works with a PDF URL, taken in the sample's random order" : "PDF-link target that stops further expansion; links are validated during download"}>
                <Input aria-label="Process now" type="number" min={1} max={discoveryLimit} value={processingLimit} onChange={(event) => setProcessingLimit(Math.min(Number(event.target.value), discoveryLimit))} />
              </LabeledInput>
              {mode === "catalog_funnel" ? <LabeledInput label="Optional focus" hint="Applied only to the final topic/keyword expansion phase"><Input aria-label="Optional focus" placeholder="e.g. health, education, FAIR data" value={focusQuery} onChange={(event) => setFocusQuery(event.target.value)} /></LabeledInput> : null}
              {mode === "random" ? <LabeledInput label="Sample seed" hint="Optional. Reuse a seed to reproduce the same sample"><Input aria-label="Sample seed" type="number" min={1} max={2147483647} placeholder="Generate automatically" value={randomSeed} onChange={(event) => setRandomSeed(event.target.value)} /></LabeledInput> : null}
              {mode === "manual" ? <LabeledInput label="Manual query" hint="Required. Results are classified as Expanded evidence"><Input aria-label="Manual query" placeholder="e.g. longitudinal cohort data reuse" value={manualQuery} onChange={(event) => setManualQuery(event.target.value)} /></LabeledInput> : null}
              <LabeledInput label="Publication types" hint="Optional comma-separated OpenAlex work types">
                <Input aria-label="Publication types" placeholder="article, preprint" value={publicationTypes} onChange={(event) => setPublicationTypes(event.target.value)} />
              </LabeledInput>
              <LabeledInput label="Language" hint="Use en for the English-only evaluation; clear for unrestricted discovery">
                <Input aria-label="Language" placeholder="en" maxLength={3} value={language} onChange={(event) => setLanguage(event.target.value)} />
              </LabeledInput>
              <LabeledInput label="From year" hint="Optional lower publication-year bound">
                <Input aria-label="From year" type="number" min={1800} max={3000} value={fromYear} onChange={(event) => setFromYear(event.target.value)} />
              </LabeledInput>
              <LabeledInput label="To year" hint="Optional upper publication-year bound">
                <Input aria-label="To year" type="number" min={1800} max={3000} value={toYear} onChange={(event) => setToYear(event.target.value)} />
              </LabeledInput>
            </CardContent>
          </Card>

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
        </div>

        <StrategySummary
          blocked={blocked || manualMissing}
          estimatedCalls={mode === "catalog_funnel" ? estimatedCalls : simpleModeCalls}
          estimatedCost={mode === "catalog_funnel" ? estimatedCost : simpleModeCalls * (mode === "manual" ? 0.001 : 0.0001)}
          excluded={excludedIds.size}
          loading={previewMutation.isPending}
          preview={preview}
          processingLimit={processingLimit}
          mode={mode}
          onPreview={() => previewMutation.mutate(previewRequest)}
        />
      </div>

      {previewMutation.isError ? <PreviewError error={previewMutation.error} /> : null}
      {preview ? (
        <>
          <PhaseResults mode={mode} preview={preview} />
          <CandidatePreview mode={mode} previewId={preview.preview_id} candidates={preview.candidates} excludedIds={excludedIds} onSelect={setSelectedCandidate} onToggle={toggleCandidate} total={preview.candidate_count} />
        </>
      ) : null}

      <div className="pointer-events-none fixed inset-x-0 bottom-0 z-30 flex justify-end gap-2 border-t bg-background/95 px-3 py-2 backdrop-blur xl:static xl:border-0 xl:bg-transparent xl:p-0">
        {launchJob.phase === "discovering" ? (
          <Button className="pointer-events-auto" type="button" variant="outline" onClick={launchJob.cancel}>
            <RiStopCircleLine data-icon="inline-start" aria-hidden="true" />
            Cancel launch
          </Button>
        ) : null}
        <Button className="pointer-events-auto min-w-40 shadow-lg" type="submit" disabled={launchJob.isPending || previewMutation.isPending || blocked || manualMissing}>
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
  const options: Array<{ mode: DiscoveryMode; label: string; badge: string; description: string; detail: string; icon: ReactNode }> = [
    { mode: "catalog_funnel", label: "Adaptive funnel", badge: "Recommended", description: "Find publications most plausibly connected to UM datasets.", detail: "Direct evidence → exact mentions → focused expansion", icon: <RiRouteLine /> },
    { mode: "random", label: "Random sample", badge: "Baseline", description: "Draw an unbiased sample from all OpenAlex works matching your filters.", detail: "Seeded, reproducible, and independent of the UM catalog", icon: <RiShuffleLine /> },
    { mode: "manual", label: "Manual query", badge: "Advanced", description: "Run one expert OpenAlex query when you already know the search framing.", detail: "One query with the same budget and review controls", icon: <RiSearchLine /> },
  ];
  return (
    <fieldset className="grid gap-3 lg:grid-cols-3" aria-labelledby="strategy-heading">
      <legend className="sr-only">Discovery strategy</legend>
      {options.map((option) => {
        const selected = option.mode === mode;
        return (
          <button
            key={option.mode}
            type="button"
            aria-label={`Select ${option.label}`}
            aria-pressed={selected}
            onClick={() => onChange(option.mode)}
            className={`grid min-h-44 gap-3 rounded-xl border p-4 text-left transition-colors focus-visible:outline-none focus-visible:ring-3 focus-visible:ring-ring/50 ${selected ? "border-primary bg-primary/5 ring-1 ring-primary" : "hover:border-foreground/30 hover:bg-muted/35"}`}
          >
            <span className="flex items-start justify-between gap-3"><span className={`flex size-10 items-center justify-center rounded-lg ${selected ? "bg-primary text-primary-foreground" : "bg-muted text-muted-foreground"}`}>{option.icon}</span><Badge variant={selected ? "secondary" : "outline"}>{option.badge}</Badge></span>
            <span><span className="block font-heading text-lg font-medium">{option.label}</span><span className="mt-1 block text-sm text-muted-foreground">{option.description}</span></span>
            <span className="mt-auto flex items-center gap-2 text-xs text-muted-foreground">{selected ? <RiCheckLine className="size-4 text-primary" /> : null}{option.detail}</span>
          </button>
        );
      })}
    </fieldset>
  );
}

function ReadinessStrip({ mode, processingLimit, profile, loading, status }: { mode: DiscoveryMode; processingLimit: number; profile: Awaited<ReturnType<typeof api.umDiscoveryProfile>> | undefined; loading: boolean; status: Awaited<ReturnType<typeof api.openAlexStatus>> | undefined }) {
  const modeLabel = mode === "catalog_funnel" ? "Adaptive funnel" : mode === "random" ? "Random sample" : "Manual query";
  const cards = [
    { label: "OpenAlex", value: status?.status === "ready" ? "Ready" : status?.status ? "Needs attention" : "Checking", icon: status?.available ? <RiCheckLine /> : <RiShieldLine /> },
    { label: "UM matching catalog", value: profile?.dataset_count.toLocaleString(), icon: <RiDatabase2Line /> },
    { label: "Selected strategy", value: modeLabel, icon: mode === "random" ? <RiShuffleLine /> : mode === "manual" ? <RiSearchLine /> : <RiRouteLine /> },
    { label: "Process-now target", value: processingLimit.toLocaleString(), icon: <RiPulseLine /> },
  ];
  return (
    <div className="grid grid-cols-2 gap-3 xl:grid-cols-4" aria-label="Discovery readiness">
      {cards.map((card) => <Card key={card.label} size="sm"><CardHeader><CardDescription>{card.label}</CardDescription><CardTitle>{loading ? <Skeleton className="h-7 w-24" /> : card.value ?? "—"}</CardTitle><span className="text-muted-foreground">{card.icon}</span></CardHeader></Card>)}
    </div>
  );
}

function StrategySummary({ blocked, estimatedCalls, estimatedCost, excluded, loading, mode, preview, processingLimit, onPreview }: { blocked: boolean; estimatedCalls: number; estimatedCost: number; excluded: number; loading: boolean; mode: DiscoveryMode; preview: Awaited<ReturnType<typeof api.discoveryPreview>> | undefined; processingLimit: number; onPreview: () => void }) {
  return (
    <Card className="sticky top-20">
      <CardHeader><CardTitle>Strategy preview</CardTitle><CardDescription>Check yield, readiness, and provider cost before creating a run.</CardDescription></CardHeader>
      <CardContent className="grid gap-4">
        <div className="grid grid-cols-2 gap-3">
          <Metric label="Process target" value={processingLimit} />
          <Metric label="Est. API calls" value={estimatedCalls} />
        </div>
        <div className="rounded-lg border bg-muted/25 p-3 text-sm">
          <SummaryLine label="Estimated OpenAlex cost" value={`$${estimatedCost.toFixed(3)}`} />
          {preview ? <div className="mt-3 grid gap-2 border-t pt-3"><SummaryLine label="Candidates retained" value={String(preview.candidate_count)} /><SummaryLine label="Candidates with PDF URL" value={String(preview.ready_count)} /><SummaryLine label="Processing queue" value={String(Math.max(0, preview.included_count - excluded))} /><SummaryLine label="No-PDF-URL watchlist" value={String(preview.watchlist_count)} /><SummaryLine label="Stopped because" value={formatText(preview.stop_reason)} /><SummaryLine label="Actual cost" value={`$${preview.actual_cost_usd.toFixed(4)}`} /></div> : null}
          {preview?.random_seed ? <div className="mt-2 border-t pt-2"><SummaryLine label="Random seed" value={String(preview.random_seed)} /></div> : null}
        </div>
        {preview?.partial ? <p className="text-xs text-amber-700 dark:text-amber-300">This is a usable partial preview; inspect its warnings before launch.</p> : null}
        <Button type="button" onClick={onPreview} disabled={blocked || loading}><RiPulseLine data-icon="inline-start" />{loading ? "Previewing results…" : preview ? "Refresh results" : "Preview results"}</Button>
        <p className="text-center text-xs text-muted-foreground">{mode === "random" ? "A fresh automatic seed produces a new random sample." : "Previewing uses OpenAlex credits but does not start PDF processing."}</p>
      </CardContent>
    </Card>
  );
}

function PhaseResults({ mode, preview }: { mode: DiscoveryMode; preview: Awaited<ReturnType<typeof api.discoveryPreview>> }) {
  const title = mode === "random" ? "Sampling execution" : mode === "manual" ? "Query execution" : "Funnel execution";
  const description = mode === "random" ? "OpenAlex drew the requested seeded sample across the selected year and publication-type scope." : mode === "manual" ? "The manual query ran through the shared budget and review controls." : "Later phases run only when earlier evidence has not filled the PDF-link target.";
  return (
    <Card>
      <CardHeader><CardTitle>{title}</CardTitle><CardDescription>{description}</CardDescription></CardHeader>
      <CardContent className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        {Object.entries(preview.phase_results).map(([phase, result]) => (
          <div key={phase} className="rounded-lg border p-3 text-sm">
            <div className="flex items-center justify-between gap-2"><p className="font-medium capitalize">{formatText(phase)}</p><Badge variant={result.status === "completed" ? "secondary" : "outline"}>{String(result.status)}</Badge></div>
            <div className="mt-3 grid gap-1 text-xs text-muted-foreground"><SummaryLine label="Unique added" value={String(result.unique_added ?? 0)} /><SummaryLine label="Ready afterward" value={String(result.ready_after_phase ?? 0)} /><SummaryLine label="Calls" value={String(result.calls ?? 0)} /></div>
          </div>
        ))}
      </CardContent>
    </Card>
  );
}

function CandidatePreview({ mode, previewId, candidates, excludedIds, onSelect, onToggle, total }: { mode: DiscoveryMode; previewId: string; candidates: DiscoveryCandidate[]; excludedIds: Set<string>; onSelect: (candidate: DiscoveryCandidate) => void; onToggle: (candidate: DiscoveryCandidate, include: boolean) => void; total: number }) {
  const selectedCandidates = candidates.filter((candidate) => candidate.included && candidate.pipeline_ready && !excludedIds.has(candidate.paper_id));
  return (
    <Card className="min-w-0">
      <CardHeader className="flex items-start justify-between gap-4">
        <div className="min-w-0"><CardTitle>Candidate review</CardTitle><CardDescription>{mode === "random" ? `${total} eligible publications retained in seeded random order. Random inclusion is not evidence of dataset use.` : `${total} candidates ranked by evidence—not predicted reuse probability.`}</CardDescription></div>
        <Button type="button" variant="outline" size="sm" className="shrink-0" disabled={!selectedCandidates.length} onClick={() => downloadCandidatePreviewCsv(previewId, mode, selectedCandidates)}><RiDownload2Line data-icon="inline-start" />Download selected CSV</Button>
      </CardHeader>
      <CardContent className="overflow-x-auto">
        <Table className="min-w-[940px]"><TableHeader><TableRow><TableHead>Include</TableHead><TableHead>Paper</TableHead><TableHead>{mode === "random" ? "Sampling basis" : "Evidence tier"}</TableHead><TableHead>Strength</TableHead><TableHead>{mode === "random" ? "Selection reason" : "Evidence"}</TableHead><TableHead>Access</TableHead></TableRow></TableHeader>
          <TableBody>{candidates.map((candidate) => {
            const included = candidate.included && !excludedIds.has(candidate.paper_id);
            return <TableRow key={candidate.paper_id}>
              <TableCell><Switch aria-label={`Include ${candidate.title ?? candidate.paper_id}`} checked={included} disabled={!candidate.pipeline_ready || !candidate.included} onCheckedChange={(checked) => onToggle(candidate, checked)} /></TableCell>
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
  return <Sheet open={candidate !== null} onOpenChange={(open) => !open && onClose()}><SheetContent side="right" className="w-[94vw] max-w-[calc(100vw-1rem)] sm:max-w-xl"><SheetHeader><SheetTitle>{candidate?.title ?? "Candidate evidence"}</SheetTitle><SheetDescription>{candidate?.paper_id}</SheetDescription></SheetHeader>{candidate ? <div className="grid gap-4 overflow-y-auto px-4 pb-5"><div className="grid grid-cols-2 gap-3 rounded-lg border p-4"><Detail label={mode === "random" ? "Sampling basis" : "Evidence tier"} value={mode === "random" ? "Random" : candidate.evidence_tier} /><Detail label="Candidate strength" value={mode === "random" ? "Not scored" : `${Math.round(candidate.candidate_strength)}/100`} /><Detail label="Discovery readiness" value={candidate.pipeline_ready ? "PDF URL found; not yet validated" : "No-PDF-URL watchlist"} /><Detail label="Citations" value={candidate.cited_by_count ?? "—"} /></div><div className="rounded-lg border p-4"><p className="text-xs font-medium uppercase text-muted-foreground">{mode === "random" ? "Selection reason" : "Discovery evidence"}</p><div className="mt-2 flex flex-wrap gap-2">{candidate.evidence_reasons.map((reason) => <Badge key={reason} variant="outline">{formatText(reason)}</Badge>)}</div></div><div className="rounded-lg border p-4"><p className="text-xs font-medium uppercase text-muted-foreground">Verified UM dataset IDs</p><p className="mt-2 break-words font-mono text-sm">{candidate.matched_um_dataset_ids.join(", ") || "No dataset ID can be safely attributed from the OpenAlex response."}</p></div>{candidate.source_url ? <Button asChild variant="outline"><a href={candidate.source_url} target="_blank" rel="noreferrer"><RiExternalLinkLine data-icon="inline-start" />Open in OpenAlex</a></Button> : null}</div> : null}</SheetContent></Sheet>;
}

function AdvancedSettings({ highThroughput, maxCost, outputPath, overwrite, umDatasetsPath, onHighThroughput, onMaxCost, onOutputPath, onOverwrite, onUmDatasetsPath }: { highThroughput: boolean; maxCost: number; outputPath: string; overwrite: boolean; umDatasetsPath: string; onHighThroughput: (value: boolean) => void; onMaxCost: (value: number) => void; onOutputPath: (value: string) => void; onOverwrite: (value: boolean) => void; onUmDatasetsPath: (value: string) => void }) {
  return <Card><details><summary className="flex cursor-pointer list-none items-center gap-2 p-5 font-medium focus-visible:outline-none focus-visible:ring-3 focus-visible:ring-ring/50"><RiSettings3Line /> Advanced settings<span className="ml-auto text-xs font-normal text-muted-foreground">Paths, budget, execution</span></summary><CardContent className="grid gap-4 border-t pt-5 sm:grid-cols-2"><LabeledInput label="UM dataset metadata" hint="Catalog source used outside this already-tested preview"><Input value={umDatasetsPath} onChange={(event) => onUmDatasetsPath(event.target.value)} /></LabeledInput><LabeledInput label="Output CSV" hint="Generated insight export"><Input value={outputPath} onChange={(event) => onOutputPath(event.target.value)} /></LabeledInput><LabeledInput label="Maximum OpenAlex cost" hint="Hard ceiling for the preview"><Input aria-label="Maximum OpenAlex cost" type="number" min={0.01} max={5} step={0.01} value={maxCost} onChange={(event) => onMaxCost(Number(event.target.value))} /></LabeledInput><div className="grid gap-3"><ToggleRow label="High-throughput mode" description="Process selected papers through run-scoped queues." checked={highThroughput} onCheckedChange={onHighThroughput} /><ToggleRow label="Overwrite artifacts" description="Replace existing generated files for matching papers." checked={overwrite} onCheckedChange={onOverwrite} /></div></CardContent></details></Card>;
}

function LabeledInput({ label, hint, children }: { label: string; hint: string; children: ReactNode }) { return <label className="grid min-w-0 gap-1.5"><span className="text-sm font-medium">{label}</span>{children}<span className="text-xs text-muted-foreground">{hint}</span></label>; }
function ToggleRow({ label, description, checked, onCheckedChange }: { label: string; description: string; checked: boolean; onCheckedChange: (checked: boolean) => void }) { return <label className="flex items-start gap-3 rounded-lg border p-3"><Switch aria-label={label} checked={checked} onCheckedChange={onCheckedChange} /><span><span className="block text-sm font-medium">{label}</span><span className="block text-xs text-muted-foreground">{description}</span></span></label>; }
function Metric({ label, value }: { label: string; value: string | number }) { return <div className="rounded-lg border p-3"><p className="text-xs text-muted-foreground">{label}</p><p className="mt-1 font-heading text-xl font-medium">{value}</p></div>; }
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
