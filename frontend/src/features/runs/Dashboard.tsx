import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  RiAdminLine,
  RiArrowRightLine,
  RiCheckLine,
  RiClipboardLine,
  RiDatabase2Line,
  RiDeleteBinLine,
  RiDownload2Line,
  RiErrorWarningLine,
  RiFileList3Line,
  RiFileTextLine,
  RiGitBranchLine,
  RiListCheck,
  RiLoader4Line,
  RiMoonLine,
  RiNodeTree,
  RiPlayLine,
  RiPulseLine,
  RiRefreshLine,
  RiRouteLine,
  RiSearchLine,
  RiServerLine,
  RiSettings3Line,
  RiShieldLine,
  RiSunLine,
  RiTimeLine,
  type RemixiconComponentType,
} from "@remixicon/react";
import { lazy, Suspense, useEffect, useMemo, useState } from "react";
import {
  BrowserRouter,
  Link,
  Navigate,
  Outlet,
  Route,
  Routes,
  useLocation,
  useOutletContext,
  useParams,
} from "react-router";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogMedia,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
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
import {
  Field,
  FieldContent,
  FieldDescription,
  FieldGroup,
  FieldLabel,
  FieldLegend,
  FieldSet,
} from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Progress } from "@/components/ui/progress";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarInset,
  SidebarMenu,
  SidebarMenuBadge,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarProvider,
  SidebarRail,
  SidebarSeparator,
  SidebarTrigger,
} from "@/components/ui/sidebar";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import maastrichtBanner from "@/assets/maastricht-university-seeklogo.png";
import { LaunchJobProvider, useLaunchJob } from "@/features/discovery/LaunchJobProvider";
import { cn } from "@/lib/utils";
import {
  ApiError,
  api,
  isActiveRun,
  type HealthResponse,
  type PipelineRunEvent,
  type PipelineRunSummary,
  type ResetResponse,
} from "@/shared/api/client";

import {
  activeStageCount,
  activeParallelStages,
  edgeToneFromSourceStatus,
  formatStageName,
  isTerminalStatus,
  isEnabledStage,
  isWorkingStatus,
  mergeStages,
  runStrategy,
  runStrategyLabel,
  stageProgress,
  stageStatusCounts,
  stageStatusLabel,
  statusBadgeVariant,
  statusTone,
  type VisualStage,
} from "./pipeline";

const DiscoveryLaunchPage = lazy(() => import("@/features/discovery/DiscoveryLaunchPage"));
const UMDatasetsPage = lazy(() => import("@/features/datasets/UMDatasetsPage"));

const RESET_CONFIRMATION = "RESET DATASIGHT";
const RUN_RESULT_PAGE_SIZE = 50;
const RUN_CANDIDATE_EXPORT_COLUMNS = [
  "paper_id",
  "publication_title",
  "publication_year",
  "discovery_method",
  "pdf_downloaded",
  "grobid_converted",
  "document_rendered",
];
type RunResultDownloadKind = "candidates" | "insights";

type OutletContext = {
  health: HealthResponse | undefined;
  healthLoading: boolean;
  runs: PipelineRunSummary[];
  runsLoading: boolean;
  latestRun: PipelineRunSummary | undefined;
  refreshAll: () => void;
};

type GraphNode = {
  stage: VisualStage;
  x: number;
  y: number;
};

type PipelinePort = "left" | "right" | "top" | "bottom";

type PipelineEdgeDefinition = {
  from: string;
  to: string;
  fromPort: PipelinePort;
  toPort: PipelinePort;
};

const NAV_ITEMS: Array<{
  label: string;
  to: string;
  icon: RemixiconComponentType;
  workspace?: boolean;
}> = [
  { label: "Launch", to: "/launch", icon: RiPlayLine },
  { label: "Runs", to: "/runs", icon: RiListCheck },
  { label: "Workspace", to: "/workspace", icon: RiRouteLine, workspace: true },
  { label: "Insights", to: "/insights", icon: RiFileList3Line },
  { label: "UM datasets", to: "/datasets", icon: RiDatabase2Line },
  { label: "Admin", to: "/admin", icon: RiAdminLine },
];

const PIPELINE_CANVAS = {
  width: 2240,
  height: 600,
  nodeWidth: 224,
  nodeHeight: 88,
} as const;

const STAGE_POSITIONS: Record<string, { x: number; y: number }> = {
  discover: { x: 180, y: 300 },
  download_pdf: { x: 560, y: 300 },
  grobid_convert: { x: 940, y: 165 },
  render_document: { x: 940, y: 435 },
  detect_mentions: { x: 1320, y: 300 },
  extract_features: { x: 1700, y: 165 },
  match_um_dataset: { x: 1700, y: 435 },
  export_insights: { x: 2080, y: 300 },
};

const PIPELINE_EDGES: PipelineEdgeDefinition[] = [
  { from: "discover", to: "download_pdf", fromPort: "right", toPort: "left" },
  {
    from: "download_pdf",
    to: "grobid_convert",
    fromPort: "right",
    toPort: "left",
  },
  {
    from: "grobid_convert",
    to: "render_document",
    fromPort: "bottom",
    toPort: "top",
  },
  {
    from: "render_document",
    to: "detect_mentions",
    fromPort: "right",
    toPort: "left",
  },
  {
    from: "detect_mentions",
    to: "extract_features",
    fromPort: "right",
    toPort: "left",
  },
  {
    from: "extract_features",
    to: "match_um_dataset",
    fromPort: "bottom",
    toPort: "top",
  },
  {
    from: "match_um_dataset",
    to: "export_insights",
    fromPort: "right",
    toPort: "left",
  },
];

const STAGE_ICONS: Record<string, RemixiconComponentType> = {
  discover: RiSearchLine,
  download_pdf: RiFileTextLine,
  grobid_convert: RiSettings3Line,
  render_document: RiFileList3Line,
  detect_mentions: RiPulseLine,
  extract_features: RiNodeTree,
  match_um_dataset: RiDatabase2Line,
  export_insights: RiArrowRightLine,
};

function Dashboard() {
  return (
    <BrowserRouter>
      <LaunchJobProvider>
        <Routes>
          <Route element={<PipelineShell />}>
          <Route index element={<Navigate replace to="/launch" />} />
          <Route
            path="launch"
            element={
              <Suspense fallback={<RouteFallback />}>
                <DiscoveryLaunchPage />
              </Suspense>
            }
          />
          <Route path="runs" element={<RunsPage />} />
          <Route path="runs/:runId/results" element={<RunResultsPage />} />
          <Route path="workspace" element={<WorkspaceRedirect />} />
          <Route path="runs/:runId" element={<WorkspacePage />} />
          <Route path="insights" element={<InsightsPage />} />
          <Route
            path="datasets"
            element={
              <Suspense fallback={<RouteFallback />}>
                <UMDatasetsPage />
              </Suspense>
            }
          />
          <Route path="admin" element={<AdminPage />} />
          <Route path="*" element={<Navigate replace to="/launch" />} />
          </Route>
        </Routes>
      </LaunchJobProvider>
    </BrowserRouter>
  );
}

function RouteFallback() {
  return <Skeleton aria-label="Loading page" className="h-72 w-full rounded-xl" />;
}

function PipelineShell() {
  const queryClient = useQueryClient();
  const location = useLocation();
  const launchJob = useLaunchJob();
  const [theme, setTheme] = useTheme();

  const healthQuery = useQuery({
    queryKey: ["health"],
    queryFn: api.health,
    refetchInterval: 5000,
  });
  const runsQuery = useQuery({
    queryKey: ["runs"],
    queryFn: () => api.runs(25),
    refetchInterval: 5000,
  });

  const runs = runsQuery.data ?? [];
  const latestRun = runs[0];
  const workspacePath = latestRun ? `/runs/${latestRun.id}` : "/runs";
  const title = routeTitle(location.pathname);

  function refreshAll() {
    void queryClient.invalidateQueries({ queryKey: ["health"] });
    void queryClient.invalidateQueries({ queryKey: ["stages"] });
    void queryClient.invalidateQueries({ queryKey: ["runs"] });
    void queryClient.invalidateQueries({ queryKey: ["run"] });
    void queryClient.invalidateQueries({ queryKey: ["run-events"] });
    void queryClient.invalidateQueries({ queryKey: ["insights"] });
    void queryClient.invalidateQueries({ queryKey: ["um-datasets"] });
    void queryClient.invalidateQueries({ queryKey: ["um-datasets-verification"] });
  }

  return (
    <SidebarProvider>
      <Sidebar collapsible="icon">
        <SidebarHeader className="h-14 justify-center p-2">
          <div className="flex h-10 w-full items-center justify-between gap-2 px-2 group-data-[collapsible=icon]:hidden">
            <div className="flex min-w-0 items-center gap-2">
              <div className="brand-mark">
                <img
                  src={maastrichtBanner}
                  alt=""
                  className="size-full object-contain"
                />
              </div>
              <div className="min-w-0">
                <p className="font-heading text-sm font-medium">DataSight</p>
                <p className="truncate text-xs text-muted-foreground">
                  Dataset reuse monitoring
                </p>
              </div>
            </div>
            <SidebarTrigger
              aria-label="Collapse sidebar"
              className="shrink-0"
            />
          </div>
          <SidebarTrigger
            aria-label="Open sidebar"
            className="brand-sidebar-trigger hidden group-data-[collapsible=icon]:inline-flex"
          >
            <img
              src={maastrichtBanner}
              alt=""
              className="size-full object-contain"
            />
          </SidebarTrigger>
        </SidebarHeader>
        <SidebarContent>
          <SidebarGroup>
            <SidebarGroupLabel>Workflow</SidebarGroupLabel>
            <SidebarGroupContent>
              <SidebarMenu>
                {NAV_ITEMS.map((item) => {
                  const isWorkspace =
                    (/^\/runs\/\d+$/.test(location.pathname)) ||
                    location.pathname === "/workspace";
                  const isRunResults = /^\/runs\/\d+\/results$/.test(location.pathname);
                  const isActive = item.workspace
                    ? isWorkspace
                    : item.label === "Runs"
                      ? location.pathname === "/runs" || isRunResults
                      : location.pathname.startsWith(item.to);
                  const to = item.workspace ? workspacePath : item.to;
                  const Icon = item.icon;
                  return (
                    <SidebarMenuItem key={item.label}>
                      <SidebarMenuButton
                        asChild
                        isActive={isActive}
                        tooltip={item.label}
                        className="brand-nav-link"
                      >
                        <Link to={to}>
                          {item.label === "Launch" && launchJob.isPending ? (
                            <RiLoader4Line className="animate-spin" aria-hidden="true" />
                          ) : (
                            <Icon />
                          )}
                          <span>{item.label === "Launch" && launchJob.isPending ? "Launching…" : item.label}</span>
                        </Link>
                      </SidebarMenuButton>
                      {item.label === "Runs" && runs.length ? (
                        <SidebarMenuBadge>{runs.length}</SidebarMenuBadge>
                      ) : null}
                    </SidebarMenuItem>
                  );
                })}
              </SidebarMenu>
            </SidebarGroupContent>
          </SidebarGroup>
          <SidebarSeparator />
          <SidebarGroup>
            <SidebarGroupLabel>System</SidebarGroupLabel>
            <SidebarGroupContent>
              <HealthList
                health={healthQuery.data}
                isLoading={healthQuery.isLoading}
                compact
              />
            </SidebarGroupContent>
          </SidebarGroup>
        </SidebarContent>
        <SidebarFooter>
          <Button
            asChild
            variant="outline"
            size="sm"
            className="justify-start group-data-[collapsible=icon]:px-2"
          >
            <Link to="/launch">
              {launchJob.isPending ? (
                <RiLoader4Line className="animate-spin" data-icon="inline-start" aria-hidden="true" />
              ) : (
                <RiPlayLine data-icon="inline-start" />
              )}
              <span className="group-data-[collapsible=icon]:hidden">
                {launchJob.isPending ? "Launching…" : "Start run"}
              </span>
            </Link>
          </Button>
        </SidebarFooter>
        <SidebarRail />
      </Sidebar>

      <SidebarInset>
        <header className="sticky top-0 z-20 flex min-h-14 items-center gap-3 border-b bg-background/95 px-3 backdrop-blur supports-[backdrop-filter]:bg-background/80 sm:px-5">
          <SidebarTrigger
            aria-label="Open navigation menu"
            className="shrink-0 md:hidden"
          />
          <div className="min-w-0 flex-1">
            <h1 className="truncate font-heading text-base font-medium">
              {title}
            </h1>
            <p className="hidden text-xs text-muted-foreground sm:block">
              {latestRun
                ? `${latestRun.query || `Run ${latestRun.id}`} · Run #${latestRun.id}`
                : "No run selected"}
            </p>
          </div>
          <HealthPills
            health={healthQuery.data}
            isLoading={healthQuery.isLoading}
          />
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-label="Refresh"
            onClick={refreshAll}
          >
            <RiRefreshLine />
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-label="Toggle theme"
            onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
          >
            {theme === "dark" ? <RiSunLine /> : <RiMoonLine />}
          </Button>
          <Button
            asChild
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-label="Admin settings"
          >
            <Link to="/admin">
              <RiSettings3Line />
            </Link>
          </Button>
        </header>
        <div className="min-h-[calc(100svh-3.5rem)] p-3 sm:p-5">
          <Outlet
            context={
              {
                health: healthQuery.data,
                healthLoading: healthQuery.isLoading,
                runs,
                runsLoading: runsQuery.isLoading,
                latestRun,
                refreshAll,
              } satisfies OutletContext
            }
          />
        </div>
      </SidebarInset>
    </SidebarProvider>
  );
}

function RunsPage() {
  const { runs, runsLoading } = useOutletData();
  const [search, setSearch] = useState("");
  const filteredRuns = useMemo(() => {
    const query = search.trim().toLowerCase();
    if (!query) {
      return runs;
    }
    return runs.filter((run) =>
      `${run.id} ${run.query ?? ""} ${run.status} ${runStrategyLabel(run)}`
        .toLowerCase()
        .includes(query),
    );
  }, [runs, search]);

  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-5">
      <Card>
        <CardHeader>
          <CardTitle>Runs</CardTitle>
          <CardDescription>
            Open a run to inspect the workspace, events, and stage metrics.
          </CardDescription>
          <CardAction>
            <Button asChild>
              <Link to="/launch">
                <RiPlayLine data-icon="inline-start" />
                start new run
              </Link>
            </Button>
          </CardAction>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <Field>
            <FieldLabel htmlFor="runSearch" className="sr-only">
              Search runs
            </FieldLabel>
            <div className="relative">
              <RiSearchLine className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
              <Input
                id="runSearch"
                className="pl-9"
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                placeholder="Search runs by query, id, or status"
              />
            </div>
          </Field>
          {runsLoading ? <RunListSkeleton /> : null}
          {!runsLoading && !filteredRuns.length ? (
            <EmptyState
              icon={RiListCheck}
              title="No runs found"
              description="Start a new run or adjust the search term."
            />
          ) : null}
          <div className="grid gap-3">
            {filteredRuns.map((run) => (
              <div key={run.id} className="grid min-w-0 grid-cols-[2.25rem_minmax(0,1fr)] gap-2">
                <Tooltip>
                  <TooltipTrigger asChild>
                    <Link
                      to={`/runs/${run.id}/results`}
                      aria-label={`View results for Run #${run.id}`}
                      className="flex min-h-20 items-center justify-center rounded-lg border bg-muted/30 text-muted-foreground transition-colors hover:border-primary/50 hover:bg-primary/10 hover:text-primary focus-visible:outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
                    >
                      <RiFileList3Line className="size-4" />
                    </Link>
                  </TooltipTrigger>
                  <TooltipContent side="left">View candidates and insights</TooltipContent>
                </Tooltip>
                <Link
                  to={`/runs/${run.id}`}
                  className="group grid min-w-0 gap-3 rounded-lg border bg-card p-4 text-card-foreground transition-colors hover:bg-muted/50 md:grid-cols-[minmax(0,1fr)_auto]"
                >
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <h2 className="truncate font-heading text-base font-medium">
                        {run.query || `Run ${run.id}`}
                      </h2>
                      <StatusBadge status={run.status} />
                      <StrategyBadge run={run} />
                    </div>
                    <p className="mt-1 text-sm text-muted-foreground">
                      Run #{run.id} · {formatDate(run.created_at)} ·{" "}
                      {run.stages.length} recorded stages
                    </p>
                  </div>
                  <Button variant="ghost" size="sm" tabIndex={-1}>
                    Open
                    <RiArrowRightLine data-icon="inline-end" />
                  </Button>
                </Link>
              </div>
            ))}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

function RunResultsPage() {
  const { runId } = useParams();
  const numericRunId = Number(runId);
  const [tab, setTab] = useState<RunResultDownloadKind>("candidates");
  const [candidateOffset, setCandidateOffset] = useState(0);
  const [insightOffset, setInsightOffset] = useState(0);
  const [downloadOpen, setDownloadOpen] = useState(false);
  const [downloadKind, setDownloadKind] = useState<RunResultDownloadKind>("candidates");
  const [selectedColumns, setSelectedColumns] = useState<string[]>([]);
  const [selectedInsight, setSelectedInsight] = useState<Record<string, unknown> | null>(null);
  const runQuery = useQuery({
    queryKey: ["run", numericRunId],
    queryFn: () => api.run(numericRunId),
    enabled: Number.isFinite(numericRunId),
    refetchInterval: (query) =>
      isActiveRun(query.state.data?.status) ? 5000 : false,
  });
  const pollInterval = runQuery.data && isActiveRun(runQuery.data.status) ? 5000 : false;
  const candidatesQuery = useQuery({
    queryKey: ["run-candidates", numericRunId, candidateOffset],
    queryFn: () => api.runCandidates(numericRunId, candidateOffset, RUN_RESULT_PAGE_SIZE),
    enabled: Number.isFinite(numericRunId),
    refetchInterval: pollInterval,
  });
  const insightsQuery = useQuery({
    queryKey: ["run-insights", numericRunId, insightOffset],
    queryFn: () => api.runResults(numericRunId, insightOffset, RUN_RESULT_PAGE_SIZE),
    enabled: Number.isFinite(numericRunId),
    refetchInterval: pollInterval,
  });
  const candidateColumns = candidatesQuery.data?.columns ?? RUN_CANDIDATE_EXPORT_COLUMNS;
  const insightColumns = insightsQuery.data?.columns ?? [];
  const activeExportColumns = downloadKind === "candidates" ? candidateColumns : insightColumns;
  const downloadMutation = useMutation({
    mutationFn: ({ kind, selected }: { kind: RunResultDownloadKind; selected: string[] }) =>
      kind === "candidates"
        ? api.downloadRunCandidateResultsCsv(numericRunId, selected)
        : api.downloadRunInsightResultsCsv(numericRunId, selected),
    onSuccess: ({ blob, filename }) => {
      const objectUrl = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = objectUrl;
      anchor.download = filename;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(objectUrl);
      setDownloadOpen(false);
    },
  });

  useEffect(() => {
    setTab("candidates");
    setCandidateOffset(0);
    setInsightOffset(0);
    setDownloadOpen(false);
    setSelectedInsight(null);
  }, [numericRunId]);

  function openDownload(kind: RunResultDownloadKind) {
    setDownloadKind(kind);
    setSelectedColumns(kind === "candidates" ? candidateColumns : insightColumns);
    downloadMutation.reset();
    setDownloadOpen(true);
  }

  function toggleColumn(column: string, checked: boolean) {
    setSelectedColumns((current) => {
      const next = new Set(current);
      if (checked) next.add(column);
      else next.delete(column);
      return activeExportColumns.filter((candidate) => next.has(candidate));
    });
  }

  if (!Number.isFinite(numericRunId)) {
    return <Navigate replace to="/runs" />;
  }

  const run = runQuery.data;

  return (
    <div className="mx-auto flex max-w-7xl flex-col">
      <Card className="gap-0 overflow-hidden">
        <CardHeader className="flex items-start justify-between gap-4 border-b py-4">
          <div className="min-w-0 space-y-1">
            <div className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
              <Link className="hover:text-foreground hover:underline" to="/runs">Runs</Link>
              <RiArrowRightLine className="size-3.5" aria-hidden="true" />
              <span>Run #{numericRunId}</span>
            </div>
            <CardTitle className="truncate text-base">{run?.query || `Run ${numericRunId}`} results</CardTitle>
            <CardDescription className="text-xs">
              Selected papers and their extracted insights; empty insight fields mean no mention was detected.
            </CardDescription>
          </div>
          {run ? <StatusBadge status={run.status} /> : null}
        </CardHeader>
        <CardContent className="min-w-0 p-0">
          {runQuery.isError ? <InlineQueryError title="Run could not be loaded" error={runQuery.error} /> : null}
          <Tabs value={tab} onValueChange={(value) => { setTab(value as RunResultDownloadKind); setDownloadOpen(false); }} className="gap-0">
            <div className="flex flex-wrap items-center justify-between gap-3 border-b bg-muted/20 px-3 py-2">
              <TabsList>
                <TabsTrigger value="candidates">Candidates{candidatesQuery.data ? <Badge variant="secondary">{candidatesQuery.data.total}</Badge> : null}</TabsTrigger>
                <TabsTrigger value="insights">Insights{insightsQuery.data ? <Badge variant="secondary">{insightsQuery.data.total}</Badge> : null}</TabsTrigger>
              </TabsList>
              <Button type="button" variant="outline" size="sm" disabled={tab === "candidates" ? !candidateColumns.length : !insightColumns.length} onClick={() => openDownload(tab)}>
                <RiDownload2Line data-icon="inline-start" />Download CSV
              </Button>
            </div>
            <TabsContent value="candidates" className="mt-0 min-w-0">
              <p className="border-b px-3 py-2 text-xs text-muted-foreground">One row per selected paper, with its discovery method and stage pass flags.</p>
              {candidatesQuery.isLoading ? <TableSkeleton /> : null}
              {candidatesQuery.isError ? <InlineQueryError title="Candidates could not be loaded" error={candidatesQuery.error} /> : null}
              {!candidatesQuery.isLoading && !candidatesQuery.isError && !candidatesQuery.data?.rows.length ? <EmptyState icon={RiFileList3Line} title="No selected candidates" description="This run has no included, PDF-ready discovery candidates." /> : null}
              {candidatesQuery.data?.rows.length ? <RunCandidatesResultTable rows={candidatesQuery.data.rows} /> : null}
              {candidatesQuery.data ? <ResultPagination offset={candidateOffset} pageSize={RUN_RESULT_PAGE_SIZE} shown={candidatesQuery.data.rows.length} total={candidatesQuery.data.total} loading={candidatesQuery.isFetching} onOffset={setCandidateOffset} /> : null}
            </TabsContent>
            <TabsContent value="insights" className="mt-0 min-w-0">
              <p className="border-b px-3 py-2 text-xs text-muted-foreground">Detailed mention rows for this run; select a row to inspect every insight field.</p>
              {insightsQuery.isLoading ? <TableSkeleton /> : null}
              {insightsQuery.isError ? <InlineQueryError title="Insights could not be loaded" error={insightsQuery.error} /> : null}
              {!insightsQuery.isLoading && !insightsQuery.isError && !insightsQuery.data?.rows.length ? <EmptyState icon={RiFileList3Line} title="No insight rows" description="No selected candidates are available for this run." /> : null}
              {insightsQuery.data?.rows.length ? <RunInsightsResultTable rows={insightsQuery.data.rows} onSelect={setSelectedInsight} /> : null}
              {insightsQuery.data ? <ResultPagination offset={insightOffset} pageSize={RUN_RESULT_PAGE_SIZE} shown={insightsQuery.data.rows.length} total={insightsQuery.data.total} loading={insightsQuery.isFetching} onOffset={setInsightOffset} /> : null}
            </TabsContent>
          </Tabs>
        </CardContent>
      </Card>
      <RunResultsDownloadSheet
        open={downloadOpen}
        kind={downloadKind}
        columns={activeExportColumns}
        selectedColumns={selectedColumns}
        pending={downloadMutation.isPending}
        error={downloadMutation.error}
        onOpenChange={setDownloadOpen}
        onColumns={setSelectedColumns}
        onToggle={toggleColumn}
        onDownload={() => downloadMutation.mutate({ kind: downloadKind, selected: selectedColumns })}
      />
      <InsightDetailSheet insight={selectedInsight} onClose={() => setSelectedInsight(null)} />
    </div>
  );
}

function RunCandidatesResultTable({ rows }: { rows: Record<string, unknown>[] }) {
  return (
    <div className="max-w-full overflow-x-auto">
      <Table className="min-w-[820px]">
        <TableHeader className="sticky top-0 z-10 bg-card"><TableRow><TableHead className="min-w-[340px]">Paper</TableHead><TableHead>Discovery method</TableHead><TableHead>PDF downloaded</TableHead><TableHead>GROBID converted</TableHead><TableHead>Document rendered</TableHead></TableRow></TableHeader>
        <TableBody>{rows.map((row, index) => (
          <TableRow key={`${String(row.paper_id)}-${index}`}>
            <PaperResultCell row={row} />
            <TableCell><DiscoveryMethodBadge value={row.discovery_method} /></TableCell>
            {(["pdf_downloaded", "grobid_converted", "document_rendered"] as const).map((column) => <TableCell key={column}><RunStageFlag passed={row[column] === true} /></TableCell>)}
          </TableRow>
        ))}</TableBody>
      </Table>
    </div>
  );
}

function RunInsightsResultTable({ rows, onSelect }: { rows: Record<string, unknown>[]; onSelect: (row: Record<string, unknown>) => void }) {
  const visibleColumns = ["dataset_name", "dataset_role", "match_status", "match_score", "um_dataset_title"];
  return <div className="max-w-full overflow-x-auto"><Table className="min-w-[1100px]"><TableHeader className="sticky top-0 z-10 bg-card"><TableRow><TableHead className="min-w-[340px]">Paper</TableHead><TableHead>Discovery method</TableHead>{visibleColumns.map((column) => <TableHead key={column}>{insightColumnLabel(column)}</TableHead>)}</TableRow></TableHeader><TableBody>{rows.map((row, index) => <TableRow key={`${String(row.paper_id)}-${String(row.dataset_name)}-${index}`} className="cursor-pointer" onClick={() => onSelect(row)}><PaperResultCell row={row} /><TableCell><DiscoveryMethodBadge value={row.discovery_method} /></TableCell>{visibleColumns.map((column) => <TableCell key={column} className="max-w-[260px] truncate" title={formatInsightCell(column, row[column])}>{formatInsightCell(column, row[column])}</TableCell>)}</TableRow>)}</TableBody></Table></div>;
}

function PaperResultCell({ row }: { row: Record<string, unknown> }) {
  const title = formatInsightCell("publication_title", row.publication_title);
  return <TableCell className="max-w-[420px]"><p className="truncate font-medium" title={title}>{title}</p><p className="truncate font-mono text-xs text-muted-foreground">{formatInsightCell("paper_id", row.paper_id)}</p></TableCell>;
}

function DiscoveryMethodBadge({ value }: { value: unknown }) {
  return <Badge variant="secondary" className="whitespace-nowrap">{formatCell(value)}</Badge>;
}

function RunStageFlag({ passed }: { passed: boolean }) {
  return <Badge variant={passed ? "secondary" : "outline"}>{passed ? "Passed" : "Not passed"}</Badge>;
}

function ResultPagination({ offset, pageSize, shown, total, loading, onOffset }: { offset: number; pageSize: number; shown: number; total: number; loading: boolean; onOffset: (offset: number) => void }) {
  const start = total ? offset + 1 : 0;
  const end = Math.min(offset + shown, total);
  return <div className="flex items-center justify-between gap-3 border-t p-3 text-xs text-muted-foreground"><p>Showing {start}–{end} of {total}</p><div className="flex gap-2"><Button type="button" variant="outline" size="sm" disabled={!offset || loading} onClick={() => onOffset(Math.max(0, offset - pageSize))}>Previous</Button><Button type="button" variant="outline" size="sm" disabled={offset + pageSize >= total || loading} onClick={() => onOffset(offset + pageSize)}>Next</Button></div></div>;
}

function RunResultsDownloadSheet({ open, kind, columns, selectedColumns, pending, error, onOpenChange, onColumns, onToggle, onDownload }: { open: boolean; kind: RunResultDownloadKind; columns: string[]; selectedColumns: string[]; pending: boolean; error: Error | null; onOpenChange: (open: boolean) => void; onColumns: (columns: string[]) => void; onToggle: (column: string, checked: boolean) => void; onDownload: () => void }) {
  const candidateExport = kind === "candidates";
  return <Sheet open={open} onOpenChange={onOpenChange}><SheetContent side="right" className="min-h-0 w-[92vw] sm:max-w-md"><SheetHeader><SheetTitle>Download {candidateExport ? "candidates" : "insights"} CSV</SheetTitle><SheetDescription>{candidateExport ? "One row per selected paper, with discovery provenance and pipeline stage pass flags." : "Detailed candidate and insight rows with discovery provenance. Stage pass flags are excluded."}</SheetDescription></SheetHeader><div className="flex items-center justify-between gap-3 px-4"><p className="text-xs text-muted-foreground">{selectedColumns.length} of {columns.length} columns selected</p><div className="flex gap-2"><Button type="button" variant="ghost" size="xs" onClick={() => onColumns(columns)}>Select all</Button><Button type="button" variant="ghost" size="xs" onClick={() => onColumns([])}>Clear</Button></div></div><ScrollArea className="min-h-0 flex-1 px-4"><div className="grid gap-2 pb-4">{columns.map((column) => <label key={column} className="flex cursor-pointer items-center gap-3 rounded-lg border bg-background px-3 py-2.5 hover:bg-muted/50"><input type="checkbox" aria-label={`Include ${insightColumnLabel(column)}`} checked={selectedColumns.includes(column)} onChange={(event) => onToggle(column, event.target.checked)} className="size-4 accent-primary" /><span className="text-sm">{insightColumnLabel(column)}</span></label>)}</div></ScrollArea>{error ? <div className="px-4"><Alert variant="destructive"><RiErrorWarningLine /><AlertTitle>Download failed</AlertTitle><AlertDescription>{error.message}</AlertDescription></Alert></div> : null}<SheetFooter className="border-t"><Button type="button" disabled={!selectedColumns.length || pending} onClick={onDownload}><RiDownload2Line data-icon="inline-start" />{pending ? "Preparing CSV…" : `Download ${candidateExport ? "candidates" : "insights"} CSV`}</Button></SheetFooter></SheetContent></Sheet>;
}

function InsightDetailSheet({ insight, onClose }: { insight: Record<string, unknown> | null; onClose: () => void }) {
  return <Sheet open={Boolean(insight)} onOpenChange={(open) => !open && onClose()}><SheetContent side="right" className="min-h-0 w-[92vw] max-w-[calc(100vw-2rem)] overflow-hidden sm:max-w-3xl"><SheetHeader><SheetTitle>Insight details</SheetTitle><SheetDescription>Complete field values for this run-scoped insight.</SheetDescription></SheetHeader><ScrollArea className="min-h-0 flex-1 px-4 pb-4"><div className="grid gap-3 pb-4">{insight ? Object.entries(insight).map(([key, value]) => { const json = formatJsonValue(value); return <div key={key} className="min-w-0 rounded-lg border bg-background p-3"><p className="mb-1 text-xs font-medium uppercase text-muted-foreground">{insightColumnLabel(key)}</p>{json ? <pre className="max-h-72 overflow-auto whitespace-pre-wrap break-words rounded-md border bg-muted/50 p-3 font-mono text-xs">{json}</pre> : <p className="max-h-72 overflow-auto whitespace-pre-wrap break-words text-sm">{formatInsightCell(key, value)}</p>}</div>; }) : null}</div></ScrollArea></SheetContent></Sheet>;
}

function InlineQueryError({ title, error }: { title: string; error: Error }) {
  return <div className="p-4"><Alert variant="destructive"><RiErrorWarningLine /><AlertTitle>{title}</AlertTitle><AlertDescription>{error.message}</AlertDescription></Alert></div>;
}

function WorkspaceRedirect() {
  const { latestRun, runsLoading } = useOutletData();
  if (runsLoading) {
    return <WorkspaceLoading />;
  }
  if (!latestRun) {
    return <Navigate replace to="/runs" />;
  }
  return <Navigate replace to={`/runs/${latestRun.id}`} />;
}

function WorkspacePage() {
  const { runId } = useParams();
  const numericRunId = Number(runId);
  const queryClient = useQueryClient();
  const [eventStage, setEventStage] = useState("all");
  const [tab, setTab] = useState("events");
  const [selectedStageName, setSelectedStageName] = useState<string | null>(
    null,
  );

  const stagesQuery = useQuery({ queryKey: ["stages"], queryFn: api.stages });
  const selectedRunQuery = useQuery({
    queryKey: ["run", numericRunId],
    queryFn: () => api.run(numericRunId),
    enabled: Number.isFinite(numericRunId),
    refetchInterval: (query) =>
      isActiveRun(query.state.data?.status) ? 2500 : false,
  });
  const eventsQuery = useQuery({
    queryKey: ["run-events", numericRunId],
    queryFn: () => api.runEvents(numericRunId, 200),
    enabled: Number.isFinite(numericRunId),
    refetchInterval: () => {
      const run = queryClient.getQueryData<PipelineRunSummary>([
        "run",
        numericRunId,
      ]);
      return isActiveRun(run?.status) ? 2500 : false;
    },
  });

  const selectedRun = selectedRunQuery.data;
  const visualStages = useMemo(
    () => mergeStages(stagesQuery.data ?? [], selectedRun),
    [stagesQuery.data, selectedRun],
  );
  const selectedStage = useMemo(() => {
    const requested = visualStages.find(
      (stage) => stage.name === selectedStageName,
    );
    if (requested) {
      return requested;
    }
    return (
      visualStages.find(
        (stage) => isEnabledStage(stage) && isWorkingStatus(stage.status),
      ) ??
      visualStages.find(isEnabledStage) ??
      visualStages[0]
    );
  }, [selectedStageName, visualStages]);
  const filteredEvents = useMemo(() => {
    const events = eventsQuery.data ?? [];
    if (eventStage === "all") {
      return events;
    }
    return events.filter((event) => event.stage === eventStage);
  }, [eventStage, eventsQuery.data]);
  const stageFilters = useMemo(() => {
    const eventStages = (eventsQuery.data ?? [])
      .map((event) => event.stage)
      .filter((stage): stage is string => Boolean(stage));
    return [
      "all",
      ...Array.from(
        new Set([...visualStages.map((stage) => stage.name), ...eventStages]),
      ),
    ];
  }, [eventsQuery.data, visualStages]);

  function inspectStage(stageName: string) {
    setSelectedStageName(stageName);
    setEventStage(stageName);
    setTab("events");
  }

  if (!Number.isFinite(numericRunId)) {
    return <Navigate replace to="/runs" />;
  }

  if (selectedRunQuery.isLoading || stagesQuery.isLoading) {
    return <WorkspaceLoading />;
  }

  if (selectedRunQuery.error) {
    return (
      <EmptyState
        icon={RiErrorWarningLine}
        title="Run unavailable"
        description={selectedRunQuery.error.message}
      />
    );
  }

  return (
    <div className="flex min-h-[calc(100svh-6rem)] flex-col gap-4">
      <WorkspaceHeader run={selectedRun} stages={visualStages} />
      <div className="grid min-h-[620px] min-w-0 flex-1 gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(420px,460px)]">
        <PipelineGraph
          stages={visualStages}
          selectedStageName={selectedStage?.name ?? null}
          onSelectStage={inspectStage}
        />
        <WorkspaceInspector
          run={selectedRun}
          selectedStage={selectedStage}
          tab={tab}
          setTab={setTab}
          stageFilters={stageFilters}
          eventStage={eventStage}
          setEventStage={setEventStage}
          events={filteredEvents}
          eventsLoading={eventsQuery.isLoading}
        />
      </div>
    </div>
  );
}

function InsightsPage() {
  const [selectedInsightIndex, setSelectedInsightIndex] = useState<
    number | null
  >(null);
  const [downloadOpen, setDownloadOpen] = useState(false);
  const [selectedColumns, setSelectedColumns] = useState<string[]>([]);
  const insightsQuery = useQuery({
    queryKey: ["insights"],
    queryFn: () => api.insights(100),
    refetchInterval: 10000,
  });
  const rows = insightsQuery.data?.rows ?? [];
  const columns = useMemo(
    () =>
      insightsQuery.data?.columns ??
      Array.from(new Set(rows.flatMap((row) => Object.keys(row)))),
    [insightsQuery.data?.columns, rows],
  );
  const selectedInsight =
    selectedInsightIndex === null ? null : rows[selectedInsightIndex];
  const downloadMutation = useMutation({
    mutationFn: (chosenColumns: string[]) =>
      api.downloadInsightsCsv(chosenColumns),
    onSuccess: ({ blob, filename }) => {
      const objectUrl = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = objectUrl;
      anchor.download = filename;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(objectUrl);
      setDownloadOpen(false);
    },
  });
  const downloadError =
    downloadMutation.error instanceof ApiError &&
    typeof downloadMutation.error.detail === "string"
      ? downloadMutation.error.detail
      : downloadMutation.error instanceof Error
        ? downloadMutation.error.message
        : null;

  function toggleDownloadColumn(column: string, checked: boolean) {
    setSelectedColumns((current) => {
      const next = new Set(current);
      if (checked) {
        next.add(column);
      } else {
        next.delete(column);
      }
      return columns.filter((candidate) => next.has(candidate));
    });
  }

  return (
    <div className="mx-auto flex w-full min-w-0 max-w-7xl flex-col gap-5">
      <Card className="min-w-0">
        <CardHeader>
          <CardTitle>Insights</CardTitle>
          <CardDescription>
            Preview the latest exported joined insight rows.
          </CardDescription>
          <CardAction>
            <div className="flex items-center gap-2">
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={insightsQuery.isLoading || !rows.length}
                onClick={() => {
                  setSelectedColumns(columns);
                  downloadMutation.reset();
                  setDownloadOpen(true);
                }}
              >
                <RiDownload2Line data-icon="inline-start" />
                Download CSV
              </Button>
              <StatusBadge
                status={insightsQuery.isFetching ? "running" : "ready"}
              />
            </div>
          </CardAction>
        </CardHeader>
        <CardContent className="min-w-0 overflow-hidden">
          {insightsQuery.isLoading ? <TableSkeleton /> : null}
          {!insightsQuery.isLoading && !rows.length ? (
            <EmptyState
              icon={RiFileList3Line}
              title="No insights yet"
              description="Run the pipeline to export joined insight rows."
            />
          ) : null}
          {rows.length ? (
            <>
              <Table className="min-w-max">
                <TableHeader>
                  <TableRow>
                    {columns.map((column) => (
                      <TableHead key={column}>
                        {insightColumnLabel(column)}
                      </TableHead>
                    ))}
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {rows.slice(0, 25).map((row, index) => (
                    <TableRow key={index}>
                      {columns.map((column) => (
                        <TableCell key={column} className="max-w-[300px]">
                          <button
                            type="button"
                            className="block w-full truncate rounded-sm text-left outline-none hover:underline focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
                            title={formatInsightCell(column, row[column])}
                            aria-label={
                              column === "discovery_mode"
                                ? `Discovery strategy: ${formatInsightCell(column, row[column])}`
                                : undefined
                            }
                            onClick={() => setSelectedInsightIndex(index)}
                          >
                            {column === "discovery_mode" ? (
                              <Badge variant="secondary">
                                {formatInsightCell(column, row[column])}
                              </Badge>
                            ) : (
                              formatInsightCell(column, row[column])
                            )}
                          </button>
                        </TableCell>
                      ))}
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
              <p className="mt-3 text-xs text-muted-foreground">
                Select any cell to inspect the full insight row.
              </p>
            </>
          ) : null}
        </CardContent>
      </Card>
      <Sheet open={downloadOpen} onOpenChange={setDownloadOpen}>
        <SheetContent side="right" className="min-h-0 w-[92vw] sm:max-w-md">
          <SheetHeader>
            <SheetTitle>Download insights</SheetTitle>
            <SheetDescription>
              Choose the columns to include. The CSV contains every available
              insight row, not only this preview.
            </SheetDescription>
          </SheetHeader>
          <div className="flex items-center justify-between gap-3 px-4">
            <p className="text-xs text-muted-foreground">
              {selectedColumns.length} of {columns.length} columns selected
            </p>
            <div className="flex gap-2">
              <Button
                type="button"
                variant="ghost"
                size="xs"
                onClick={() => setSelectedColumns(columns)}
              >
                Select all
              </Button>
              <Button
                type="button"
                variant="ghost"
                size="xs"
                onClick={() => setSelectedColumns([])}
              >
                Clear
              </Button>
            </div>
          </div>
          <ScrollArea className="min-h-0 flex-1 px-4">
            <div className="grid gap-2 pb-4">
              {columns.map((column) => {
                const label = insightColumnLabel(column);
                return (
                  <label
                    key={column}
                    className="flex cursor-pointer items-center gap-3 rounded-lg border bg-background px-3 py-2.5 hover:bg-muted/50"
                  >
                    <input
                      type="checkbox"
                      aria-label={`Include ${label}`}
                      checked={selectedColumns.includes(column)}
                      onChange={(event) =>
                        toggleDownloadColumn(column, event.target.checked)
                      }
                      className="size-4 accent-primary"
                    />
                    <span className="text-sm">{label}</span>
                  </label>
                );
              })}
            </div>
          </ScrollArea>
          {downloadError ? (
            <div className="px-4">
              <Alert variant="destructive">
                <RiErrorWarningLine />
                <AlertTitle>Download failed</AlertTitle>
                <AlertDescription>{downloadError}</AlertDescription>
              </Alert>
            </div>
          ) : null}
          <SheetFooter className="border-t">
            <Button
              type="button"
              disabled={!selectedColumns.length || downloadMutation.isPending}
              onClick={() => downloadMutation.mutate(selectedColumns)}
            >
              <RiDownload2Line data-icon="inline-start" />
              {downloadMutation.isPending ? "Preparing CSV…" : "Download CSV"}
            </Button>
          </SheetFooter>
        </SheetContent>
      </Sheet>
      <Sheet
        open={Boolean(selectedInsight)}
        onOpenChange={(open) => {
          if (!open) {
            setSelectedInsightIndex(null);
          }
        }}
      >
        <SheetContent
          side="right"
          className="min-h-0 w-[92vw] max-w-[calc(100vw-2rem)] overflow-hidden sm:max-w-3xl"
        >
          <SheetHeader className="shrink-0">
            <SheetTitle>
              Insight row{" "}
              {selectedInsightIndex === null ? "" : selectedInsightIndex + 1}
            </SheetTitle>
            <SheetDescription>
              Complete field values for the selected exported insight.
            </SheetDescription>
          </SheetHeader>
          <ScrollArea className="min-h-0 flex-1 overflow-hidden px-4 pb-4">
            <div className="grid min-w-0 gap-3 pb-4">
              {selectedInsight
                ? Object.entries(selectedInsight).map(([key, value]) => {
                    const formattedJson = formatJsonValue(value);

                    return (
                      <div
                        key={key}
                        className="min-w-0 rounded-lg border bg-background p-3"
                      >
                        <p className="mb-1 text-xs font-medium uppercase tracking-normal text-muted-foreground">
                          {insightColumnLabel(key)}
                        </p>
                        {formattedJson ? (
                          <pre className="max-h-72 max-w-full overflow-auto whitespace-pre-wrap break-words rounded-md border bg-muted/50 p-3 font-mono text-xs leading-relaxed text-foreground [overflow-wrap:anywhere] [tab-size:2]">
                            {formattedJson}
                          </pre>
                        ) : (
                          <p className="max-h-72 overflow-auto whitespace-pre-wrap break-words [overflow-wrap:anywhere] text-sm">
                            {formatInsightCell(key, value)}
                          </p>
                        )}
                      </div>
                    );
                  })
                : null}
            </div>
          </ScrollArea>
        </SheetContent>
      </Sheet>
    </div>
  );
}

function AdminPage() {
  const queryClient = useQueryClient();
  const [resetConfirm, setResetConfirm] = useState("");
  const [resetAcknowledged, setResetAcknowledged] = useState(false);
  const [resetForce, setResetForce] = useState(false);
  const [resetResult, setResetResult] = useState<ResetResponse | null>(null);

  const resetMutation = useMutation({
    mutationFn: api.reset,
    onSuccess: async (result) => {
      setResetResult(result);
      setResetConfirm("");
      setResetAcknowledged(false);
      setResetForce(false);
      await queryClient.invalidateQueries();
    },
  });
  const canReset = resetConfirm === RESET_CONFIRMATION && resetAcknowledged;
  const resetErrorText =
    resetMutation.error instanceof ApiError &&
    resetMutation.error.status === 409
      ? `${String(resetMutation.error.detail)} Retry only if you are sure by enabling force reset.`
      : resetMutation.error?.message;

  return (
    <div className="mx-auto grid max-w-5xl gap-5 lg:grid-cols-[minmax(0,1fr)_320px]">
      <Card>
        <CardHeader>
          <CardTitle>Admin</CardTitle>
          <CardDescription>
            Operational controls for local pipeline data and generated runtime
            storage.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-6">
          <Alert variant="destructive">
            <RiShieldLine />
            <AlertTitle>Danger zone</AlertTitle>
            <AlertDescription>
              Reset wipes pipeline runs, stage records, events, extracted
              results, and generated files under storage. Source data under data
              is not deleted by the backend reset service.
            </AlertDescription>
          </Alert>

          <FieldSet>
            <FieldLegend>Reset confirmation</FieldLegend>
            <FieldGroup>
              <Field>
                <FieldLabel htmlFor="resetConfirm">
                  Type {RESET_CONFIRMATION} to confirm
                </FieldLabel>
                <Input
                  id="resetConfirm"
                  value={resetConfirm}
                  onChange={(event) => {
                    setResetConfirm(event.target.value);
                    setResetResult(null);
                    resetMutation.reset();
                  }}
                  placeholder={RESET_CONFIRMATION}
                  autoComplete="off"
                />
              </Field>
              <Field orientation="horizontal" className="rounded-lg border p-3">
                <Switch
                  id="resetAcknowledged"
                  checked={resetAcknowledged}
                  onCheckedChange={setResetAcknowledged}
                />
                <FieldContent>
                  <FieldLabel htmlFor="resetAcknowledged">
                    I understand this action cannot be undone from the app.
                  </FieldLabel>
                </FieldContent>
              </Field>
              <Field orientation="horizontal" className="rounded-lg border p-3">
                <Switch
                  id="resetForce"
                  checked={resetForce}
                  onCheckedChange={setResetForce}
                />
                <FieldContent>
                  <FieldLabel htmlFor="resetForce">
                    Force reset active runs
                  </FieldLabel>
                  <FieldDescription>
                    Use only after checking whether active workers can be
                    interrupted.
                  </FieldDescription>
                </FieldContent>
              </Field>
            </FieldGroup>
          </FieldSet>

          {resetErrorText ? (
            <Alert variant="destructive">
              <RiErrorWarningLine />
              <AlertTitle>Reset failed</AlertTitle>
              <AlertDescription>{resetErrorText}</AlertDescription>
            </Alert>
          ) : null}
          {resetResult ? (
            <Alert>
              <RiCheckLine />
              <AlertTitle>Reset complete</AlertTitle>
              <AlertDescription>
                Truncated {resetResult.truncated_tables.length} tables and
                recreated {resetResult.recreated_directories.length} storage
                directories.
              </AlertDescription>
            </Alert>
          ) : null}

          <div className="flex justify-end">
            <AlertDialog>
              <AlertDialogTrigger asChild>
                <Button
                  type="button"
                  variant="destructive"
                  disabled={!canReset || resetMutation.isPending}
                >
                  <RiDeleteBinLine data-icon="inline-start" />
                  Review reset
                </Button>
              </AlertDialogTrigger>
              <AlertDialogContent>
                <AlertDialogHeader>
                  <AlertDialogMedia>
                    <RiShieldLine />
                  </AlertDialogMedia>
                  <AlertDialogTitle>Reset DataSight storage?</AlertDialogTitle>
                  <AlertDialogDescription>
                    This will remove stored pipeline records and generated
                    runtime files. The authoritative UM dataset catalog is
                    preserved. The confirmation phrase has been entered correctly.
                  </AlertDialogDescription>
                </AlertDialogHeader>
                <AlertDialogFooter>
                  <AlertDialogCancel>Cancel</AlertDialogCancel>
                  <AlertDialogAction
                    variant="destructive"
                    onClick={() =>
                      resetMutation.mutate({
                        confirm: resetConfirm,
                        force: resetForce,
                      })
                    }
                  >
                    Reset everything
                  </AlertDialogAction>
                </AlertDialogFooter>
              </AlertDialogContent>
            </AlertDialog>
          </div>
        </CardContent>
      </Card>

      <Card size="sm">
        <CardHeader>
          <CardTitle>Reset scope</CardTitle>
          <CardDescription>
            The backend reset service controls exactly what is removed.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3 text-sm text-muted-foreground">
          <p>
            Pipeline runs, stages, events, publications, mentions, and insight
            records are truncated.
          </p>
          <p>Runtime storage directories are recreated after deletion.</p>
          <p>
            Source datasets in the repository data directory remain untouched.
          </p>
        </CardContent>
      </Card>
    </div>
  );
}

function WorkspaceHeader({
  run,
  stages,
}: {
  run: PipelineRunSummary | undefined;
  stages: VisualStage[];
}) {
  const progress = stageProgress(stages);
  const statusCounts = stageStatusCounts(stages);
  const parallelStages = activeParallelStages(run, stages);

  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle>
          {run ? run.query || `Run ${run.id}` : "Run workspace"}
        </CardTitle>
        <CardDescription>
          {run
            ? `Run #${run.id} · started ${formatDate(run.created_at)}`
            : "Select a run to inspect progress."}
        </CardDescription>
        <CardAction>
          {run ? (
            <div className="flex flex-wrap items-center justify-end gap-2">
              <StrategyBadge run={run} />
              <StatusBadge status={run.status} />
            </div>
          ) : null}
        </CardAction>
      </CardHeader>
      <CardContent className="grid gap-4 md:grid-cols-[minmax(0,1fr)_260px]">
        <div className="flex flex-wrap gap-2">
          <MetricPill
            label="Done"
            value={statusCounts.done}
          />
          <MetricPill
            label="Skipped"
            value={statusCounts.skipped}
          />
          <MetricPill
            label="Errors"
            value={statusCounts.errors}
          />
          <MetricPill label="Stages" value={activeStageCount(stages)} />
          {run && runStrategy(run) === "high_throughput" ? (
            <MetricPill
              label="Mode"
              value={
                parallelStages.length > 1
                  ? `Parallel: ${parallelStages.length} stages active`
                  : "High-throughput"
              }
            />
          ) : null}
        </div>
        <div className="flex flex-col gap-2">
          <div className="flex items-center justify-between text-xs text-muted-foreground">
            <span>Stage progress</span>
            <span>{progress}%</span>
          </div>
          <Progress value={progress} />
        </div>
        {run?.error ? (
          <Alert variant="destructive" className="md:col-span-2">
            <RiErrorWarningLine />
            <AlertTitle>Run error</AlertTitle>
            <AlertDescription>{run.error}</AlertDescription>
          </Alert>
        ) : null}
      </CardContent>
    </Card>
  );
}

function pipelinePortPoint(node: GraphNode, port: PipelinePort) {
  const halfWidth = PIPELINE_CANVAS.nodeWidth / 2;
  const halfHeight = PIPELINE_CANVAS.nodeHeight / 2;

  switch (port) {
    case "left":
      return { x: node.x - halfWidth, y: node.y };
    case "right":
      return { x: node.x + halfWidth, y: node.y };
    case "top":
      return { x: node.x, y: node.y - halfHeight };
    case "bottom":
      return { x: node.x, y: node.y + halfHeight };
  }
}

function pipelineEdgePath(
  edge: PipelineEdgeDefinition,
  fromNode: GraphNode,
  toNode: GraphNode,
) {
  const start = pipelinePortPoint(fromNode, edge.fromPort);
  const end = pipelinePortPoint(toNode, edge.toPort);
  const dx = end.x - start.x;
  const dy = end.y - start.y;

  if (Math.abs(dy) < 1) {
    return `M ${start.x} ${start.y} L ${end.x} ${end.y}`;
  }

  if (
    (edge.fromPort === "left" || edge.fromPort === "right") &&
    (edge.toPort === "left" || edge.toPort === "right")
  ) {
    const handle = Math.max(88, Math.min(180, Math.abs(dx) * 0.48));
    const direction = dx >= 0 ? 1 : -1;
    const c1 = { x: start.x + handle * direction, y: start.y };
    const c2 = { x: end.x - handle * direction, y: end.y };
    return `M ${start.x} ${start.y} C ${c1.x} ${c1.y}, ${c2.x} ${c2.y}, ${end.x} ${end.y}`;
  }

  const handle = Math.max(72, Math.min(150, Math.abs(dy) * 0.55));
  const direction = dy >= 0 ? 1 : -1;
  const c1 = { x: start.x, y: start.y + handle * direction };
  const c2 = { x: end.x, y: end.y - handle * direction };
  return `M ${start.x} ${start.y} C ${c1.x} ${c1.y}, ${c2.x} ${c2.y}, ${end.x} ${end.y}`;
}

function inferPipelineEdge(from: GraphNode, to: GraphNode): PipelineEdgeDefinition {
  const dx = to.x - from.x;
  const dy = to.y - from.y;

  if (Math.abs(dx) >= Math.abs(dy)) {
    return {
      from: from.stage.name,
      to: to.stage.name,
      fromPort: dx >= 0 ? "right" : "left",
      toPort: dx >= 0 ? "left" : "right",
    };
  }

  return {
    from: from.stage.name,
    to: to.stage.name,
    fromPort: dy >= 0 ? "bottom" : "top",
    toPort: dy >= 0 ? "top" : "bottom",
  };
}

function pipelineEdgesForNodes(nodes: GraphNode[]) {
  const definedEdges = new Map(
    PIPELINE_EDGES.map((edge) => [`${edge.from}-${edge.to}`, edge]),
  );

  return nodes.slice(0, -1).map((node, index) => {
    const next = nodes[index + 1]!;
    return (
      definedEdges.get(`${node.stage.name}-${next.stage.name}`) ??
      inferPipelineEdge(node, next)
    );
  });
}

function PipelineGraph({
  stages,
  selectedStageName,
  onSelectStage,
}: {
  stages: VisualStage[];
  selectedStageName: string | null;
  onSelectStage: (stageName: string) => void;
}) {
  if (!stages.length) {
    return (
      <EmptyState
        icon={RiRouteLine}
        title="Stage metadata unavailable"
        description="The backend stage catalog has not loaded yet."
      />
    );
  }

  const nodes = stages.map((stage, index) => ({
    stage,
    ...(STAGE_POSITIONS[stage.name] ?? { x: 160 + index * 180, y: 280 }),
  }));
  const nodesByName = new Map(nodes.map((node) => [node.stage.name, node]));
  const edges = pipelineEdgesForNodes(nodes);

  return (
    <section className="pipeline-canvas min-h-[560px] min-w-0 overflow-auto rounded-xl border bg-card">
      <div
        className="relative"
        style={{
          minHeight: PIPELINE_CANVAS.height,
          minWidth: PIPELINE_CANVAS.width,
        }}
      >
        <div className="absolute left-4 top-4 flex items-center gap-2 rounded-lg border bg-background/80 px-3 py-2 text-xs text-muted-foreground shadow-xs backdrop-blur">
          <RiGitBranchLine />
          <span>Run graph</span>
          <Badge variant="secondary">
            {activeStageCount(stages)} active · {stages.length} total
          </Badge>
        </div>
        <svg
          className="pointer-events-none absolute inset-0 size-full"
          viewBox={`0 0 ${PIPELINE_CANVAS.width} ${PIPELINE_CANVAS.height}`}
          aria-hidden="true"
        >
          <defs>
            <filter
              id="pipelineGlow"
              x="-16"
              y="-16"
              width={PIPELINE_CANVAS.width + 32}
              height={PIPELINE_CANVAS.height + 32}
              filterUnits="userSpaceOnUse"
            >
              <feGaussianBlur stdDeviation="2" result="blur" />
              <feMerge>
                <feMergeNode in="blur" />
                <feMergeNode in="SourceGraphic" />
              </feMerge>
            </filter>
          </defs>
          {edges.map((edge) => {
            const from = nodesByName.get(edge.from);
            const to = nodesByName.get(edge.to);

            if (!from || !to) {
              return null;
            }

            const enabled =
              isEnabledStage(from.stage) && isEnabledStage(to.stage);
            const active =
              enabled &&
              (isTerminalStatus(from.stage.status) ||
                isWorkingStatus(from.stage.status) ||
                isWorkingStatus(to.stage.status));
            const edgeTone = enabled
              ? edgeToneFromSourceStatus(from.stage.status)
              : "muted";
            const start = pipelinePortPoint(from, edge.fromPort);
            const end = pipelinePortPoint(to, edge.toPort);

            return (
              <g key={`${edge.from}-${edge.to}`}>
                <path
                  d={pipelineEdgePath(edge, from, to)}
                  data-active={active}
                  data-edge-tone={edgeTone}
                  data-edge={`${edge.from}-${edge.to}`}
                  className="pipeline-edge"
                  filter={edgeTone !== "muted" ? "url(#pipelineGlow)" : undefined}
                />
                <circle
                  cx={start.x}
                  cy={start.y}
                  r="4.25"
                  data-active={active}
                  data-edge-tone={edgeTone}
                  className="pipeline-port-dot"
                />
                <circle
                  cx={end.x}
                  cy={end.y}
                  r="4.25"
                  data-active={active}
                  data-edge-tone={edgeTone}
                  className="pipeline-port-dot"
                />
              </g>
            );
          })}
        </svg>
        {nodes.map((node) => (
          <PipelineNode
            key={node.stage.name}
            node={node}
            selected={selectedStageName === node.stage.name}
            onSelect={() => onSelectStage(node.stage.name)}
          />
        ))}
      </div>
    </section>
  );
}

function PipelineNode({
  node,
  selected,
  onSelect,
}: {
  node: GraphNode;
  selected: boolean;
  onSelect: () => void;
}) {
  const Icon = STAGE_ICONS[node.stage.name] ?? RiNodeTree;
  const enabled = isEnabledStage(node.stage);
  const tone = enabled ? statusTone(node.stage.status) : "muted";

  return (
    <button
      type="button"
      className={cn("pipeline-node", selected && "pipeline-node-selected")}
      style={{ left: node.x, top: node.y }}
      data-status-tone={tone}
      data-disabled={!enabled ? "true" : undefined}
      data-working={node.stage.working ? "true" : undefined}
      aria-label={`Inspect ${titleCase(formatStageName(node.stage.name))} status and filter events`}
      onClick={onSelect}
    >
      <span className="pipeline-node-icon">
        <Icon />
      </span>
      <span className="min-w-0">
        <span className="block text-[10px] font-medium uppercase tracking-normal text-muted-foreground">
          {stageStatusLabel(node.stage)}
        </span>
        <span className="block font-heading text-sm font-medium leading-snug">
          {node.stage.label ?? titleCase(formatStageName(node.stage.name))}
        </span>
      </span>
    </button>
  );
}

function WorkspaceInspector({
  run,
  selectedStage,
  tab,
  setTab,
  stageFilters,
  eventStage,
  setEventStage,
  events,
  eventsLoading,
}: {
  run: PipelineRunSummary | undefined;
  selectedStage: VisualStage | undefined;
  tab: string;
  setTab: (value: string) => void;
  stageFilters: string[];
  eventStage: string;
  setEventStage: (value: string) => void;
  events: PipelineRunEvent[];
  eventsLoading: boolean;
}) {
  return (
    <Card
      className="flex min-h-[620px] min-w-0 flex-col overflow-hidden"
      size="sm"
      data-datasight-inspector
    >
      <CardHeader>
        <CardTitle>
          {selectedStage
            ? titleCase(formatStageName(selectedStage.name))
            : "Inspector"}
        </CardTitle>
        <CardDescription>
          {selectedStage?.description ??
            "Select a stage node to inspect metrics and structured events."}
        </CardDescription>
        <CardAction>
          {selectedStage ? (
            <StatusBadge status={stageStatusLabel(selectedStage)} />
          ) : null}
        </CardAction>
      </CardHeader>
      <CardContent className="flex min-h-0 min-w-0 flex-1 flex-col gap-4 overflow-hidden">
        <div className="grid grid-cols-2 gap-2">
          <MetricPill label="Run" value={run ? `#${run.id}` : "-"} />
          <MetricPill
            label="Attempts"
            value={selectedStage?.run?.attempt_count ?? 0}
          />
        </div>
        {selectedStage?.run?.error ? (
          <Alert variant="destructive">
            <RiErrorWarningLine />
            <AlertTitle>Stage error</AlertTitle>
            <AlertDescription>{selectedStage.run.error}</AlertDescription>
          </Alert>
        ) : null}
        <Tabs
          value={tab}
          onValueChange={setTab}
          className="flex min-h-0 min-w-0 flex-1 flex-col"
        >
          <TabsList variant="line">
            <TabsTrigger value="events">Events</TabsTrigger>
            <TabsTrigger value="metrics">Metrics</TabsTrigger>
          </TabsList>
          <TabsContent value="events" className="min-h-0 min-w-0 flex-1 overflow-hidden">
            <div className="mb-3 flex max-h-28 min-w-0 flex-wrap gap-2 overflow-auto pr-1">
              {stageFilters.map((stage) => (
                <Button
                  key={stage}
                  type="button"
                  variant={eventStage === stage ? "default" : "outline"}
                  size="xs"
                  onClick={() => setEventStage(stage)}
                >
                  {stage === "all" ? "All" : titleCase(formatStageName(stage))}
                </Button>
              ))}
            </div>
            <EventLog events={events} isLoading={eventsLoading} />
          </TabsContent>
          <TabsContent value="metrics" className="min-h-0 min-w-0 flex-1 overflow-hidden">
            <MetricsPanel metrics={selectedStage?.run?.metrics ?? {}} />
          </TabsContent>
        </Tabs>
      </CardContent>
    </Card>
  );
}

function EventLog({
  events,
  isLoading,
}: {
  events: PipelineRunEvent[];
  isLoading: boolean;
}) {
  if (isLoading) {
    return <EventSkeleton />;
  }
  if (!events.length) {
    return (
      <EmptyState
        icon={RiTimeLine}
        title="No events"
        description="No structured events match this filter."
        compact
      />
    );
  }

  return (
    <div className="h-[52svh] min-h-[340px] max-h-[520px] min-w-0 overflow-x-hidden overflow-y-auto">
      <div className="box-border flex min-w-0 max-w-full flex-col gap-2 pr-5">
        {events.map((event) => (
          <article
            key={event.id}
            className="w-full min-w-0 max-w-full overflow-hidden rounded-lg border bg-background p-3"
          >
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <StatusBadge status={event.level} />
                  {event.stage ? (
                    <Badge variant="secondary">
                      {titleCase(formatStageName(event.stage))}
                    </Badge>
                  ) : null}
                  <span className="text-xs text-muted-foreground">
                    {formatDate(event.created_at)}
                  </span>
                </div>
                <p className="mt-2 break-words text-sm font-medium">
                  {event.message}
                </p>
              </div>
              <Button
                type="button"
                variant="ghost"
                size="icon-xs"
                aria-label="Copy event payload"
                onClick={() => copyPayload(event)}
              >
                <RiClipboardLine />
              </Button>
            </div>
            {Object.keys(event.payload).length ? (
              <pre className="mt-3 box-border max-h-36 w-full min-w-0 max-w-full overflow-auto rounded-md bg-muted p-3 text-xs text-muted-foreground">
                {JSON.stringify(event.payload, null, 2)}
              </pre>
            ) : null}
          </article>
        ))}
      </div>
    </div>
  );
}

function MetricsPanel({ metrics }: { metrics: Record<string, unknown> }) {
  const entries = Object.entries(metrics);
  if (!entries.length) {
    return (
      <EmptyState
        icon={RiPulseLine}
        title="No metrics"
        description="This stage has not recorded metrics yet."
        compact
      />
    );
  }
  return (
    <div className="grid gap-2">
      {entries.map(([key, value]) => (
        <div
          key={key}
          className="flex items-center justify-between gap-3 rounded-lg border bg-background px-3 py-2 text-sm"
        >
          <span className="text-muted-foreground">
            {titleCase(formatStageName(key))}
          </span>
          <span className="max-w-[180px] truncate font-medium">
            {formatCell(value)}
          </span>
        </div>
      ))}
    </div>
  );
}

function HealthList({
  health,
  isLoading,
  compact = false,
}: {
  health: HealthResponse | undefined;
  isLoading: boolean;
  compact?: boolean;
}) {
  const checks = healthChecks(health);
  return (
    <div
      className={cn(
        "grid gap-1.5",
        compact ? "group-data-[collapsible=icon]:hidden" : "sm:grid-cols-2",
      )}
    >
      {checks.map((check) => {
        const Icon = check.icon;
        return (
          <div
            key={check.label}
            className="flex items-center justify-between gap-2 rounded-md px-2 py-1.5 text-sm"
          >
            <span className="flex min-w-0 items-center gap-2">
              <Icon />
              <span className="truncate">{check.label}</span>
            </span>
            <StatusBadge
              status={
                isLoading ? "checking" : check.ready ? "ready" : "offline"
              }
              passive
            />
          </div>
        );
      })}
    </div>
  );
}

function HealthPills({
  health,
  isLoading,
}: {
  health: HealthResponse | undefined;
  isLoading: boolean;
}) {
  const checks = healthChecks(health);
  const ready = checks.filter((check) => check.ready).length;
  return (
    <div className="hidden items-center gap-2 lg:flex">
      <Badge
        variant="outline"
        className="system-status-badge pointer-events-none"
      >
        {isLoading ? "Checking" : `${ready}/${checks.length} ready`}
      </Badge>
    </div>
  );
}

function StatusBadge({
  status,
  passive = false,
}: {
  status: string;
  passive?: boolean;
}) {
  return (
    <Badge
      variant={passive ? "outline" : statusBadgeVariant(status)}
      data-status-tone={statusTone(status)}
      data-passive-status={passive}
      className={cn(passive && "system-status-badge pointer-events-none")}
    >
      {status}
    </Badge>
  );
}

function StrategyBadge({ run }: { run: PipelineRunSummary }) {
  const strategy = runStrategy(run);
  return (
    <Badge variant={strategy === "high_throughput" ? "default" : "secondary"}>
      {runStrategyLabel(run)}
    </Badge>
  );
}

function MetricPill({
  label,
  value,
}: {
  label: string;
  value: string | number;
}) {
  return (
    <div className="min-w-0 rounded-lg border bg-background px-3 py-2">
      <p className="truncate font-heading text-sm font-medium">{value}</p>
      <p className="text-xs text-muted-foreground">{label}</p>
    </div>
  );
}

function EmptyState({
  icon: Icon,
  title,
  description,
  compact = false,
}: {
  icon: RemixiconComponentType;
  title: string;
  description: string;
  compact?: boolean;
}) {
  return (
    <Empty className={compact ? "p-6" : "min-h-72"}>
      <EmptyHeader>
        <EmptyMedia variant="icon">
          <Icon />
        </EmptyMedia>
        <EmptyTitle>{title}</EmptyTitle>
        <EmptyDescription>{description}</EmptyDescription>
      </EmptyHeader>
    </Empty>
  );
}

function WorkspaceLoading() {
  return (
    <div className="flex min-h-[620px] flex-col gap-4">
      <Skeleton className="h-32 w-full rounded-xl" />
      <div className="grid flex-1 gap-4 xl:grid-cols-[minmax(0,1fr)_360px]">
        <Skeleton className="min-h-[560px] rounded-xl" />
        <Skeleton className="min-h-[560px] rounded-xl" />
      </div>
    </div>
  );
}

function RunListSkeleton() {
  return (
    <div className="grid gap-3">
      {Array.from({ length: 3 }).map((_, index) => (
        <Skeleton key={index} className="h-24 rounded-lg" />
      ))}
    </div>
  );
}

function EventSkeleton() {
  return (
    <div className="flex flex-col gap-2">
      {Array.from({ length: 4 }).map((_, index) => (
        <Skeleton key={index} className="h-24 rounded-lg" />
      ))}
    </div>
  );
}

function TableSkeleton() {
  return (
    <div className="flex flex-col gap-2">
      {Array.from({ length: 6 }).map((_, index) => (
        <Skeleton key={index} className="h-10 rounded-md" />
      ))}
    </div>
  );
}

function useOutletData() {
  return useOutletContext<OutletContext>();
}

function useTheme() {
  const [theme, setThemeState] = useState<"light" | "dark">(() => {
    if (typeof window === "undefined") {
      return "light";
    }
    return window.localStorage.getItem("datasight-theme") === "dark"
      ? "dark"
      : "light";
  });

  useEffect(() => {
    document.documentElement.classList.toggle("dark", theme === "dark");
    window.localStorage.setItem("datasight-theme", theme);
  }, [theme]);

  return [theme, setThemeState] as const;
}

function healthChecks(health: HealthResponse | undefined) {
  return [
    {
      label: "Database",
      ready: Boolean(health?.database_ready),
      icon: RiDatabase2Line,
    },
    { label: "Redis", ready: Boolean(health?.redis_ready), icon: RiPulseLine },
    {
      label: "Worker",
      ready: Boolean(health?.worker_ready),
      icon: RiServerLine,
    },
    {
      label: "GROBID",
      ready: Boolean(health?.grobid_ready),
      icon: RiFileTextLine,
    },
  ];
}

function routeTitle(pathname: string) {
  if (/^\/runs\/\d+\/results$/.test(pathname)) {
    return "Run results";
  }
  if (pathname.startsWith("/runs/")) {
    return "Run workspace";
  }
  if (pathname.startsWith("/runs")) {
    return "Runs";
  }
  if (pathname.startsWith("/insights")) {
    return "Insights";
  }
  if (pathname.startsWith("/datasets")) {
    return "UM datasets";
  }
  if (pathname.startsWith("/admin")) {
    return "Admin";
  }
  return "Launch";
}

function formatDate(value: string | null | undefined) {
  if (!value) {
    return "not recorded";
  }
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function formatCell(value: unknown) {
  if (value === null || value === undefined || value === "") {
    return "-";
  }
  if (typeof value === "object") {
    return JSON.stringify(value);
  }
  return String(value);
}

const DISCOVERY_STRATEGY_LABELS: Record<string, string> = {
  adaptive_funnel: "Smart discovery",
  catalog_funnel: "Smart discovery",
  manual: "Manual query",
  manual_query: "Manual query",
  random: "Random selection",
  random_sample: "Random selection",
  smart_discovery: "Smart discovery",
  unrecorded: "Not recorded",
};

function insightColumnLabel(column: string) {
  const runStageLabels: Record<string, string> = {
    discovery_methods: "Discovery method",
    discovery_method: "Discovery method",
    pdf_downloaded: "PDF downloaded",
    grobid_converted: "GROBID converted",
    document_rendered: "Document rendered",
  };
  if (runStageLabels[column]) {
    return runStageLabels[column];
  }
  return column === "discovery_mode"
    ? "Discovery strategy"
    : titleCase(formatStageName(column));
}

function formatInsightCell(column: string, value: unknown) {
  if (isRunStageFlag(column)) {
    return value === true ? "Passed" : "Not passed";
  }
  if (column !== "discovery_mode") {
    return formatCell(value);
  }

  const rawValue = formatCell(value);
  return DISCOVERY_STRATEGY_LABELS[rawValue] ?? titleCase(formatStageName(rawValue));
}

function isRunStageFlag(column: string) {
  return column === "pdf_downloaded" || column === "grobid_converted" || column === "document_rendered";
}

function formatJsonValue(value: unknown) {
  if (typeof value === "object" && value !== null) {
    return JSON.stringify(value, null, 2);
  }
  if (typeof value !== "string") {
    return null;
  }

  const trimmed = value.trim();
  if (
    !(
      (trimmed.startsWith("{") && trimmed.endsWith("}")) ||
      (trimmed.startsWith("[") && trimmed.endsWith("]"))
    )
  ) {
    return null;
  }

  try {
    const parsed: unknown = JSON.parse(trimmed);
    return typeof parsed === "object" && parsed !== null
      ? JSON.stringify(parsed, null, 2)
      : null;
  } catch {
    return null;
  }
}

function titleCase(value: string) {
  return value.replace(/\b\w/g, (match) => match.toUpperCase());
}

function copyPayload(event: PipelineRunEvent) {
  if (navigator.clipboard) {
    void navigator.clipboard.writeText(JSON.stringify(event, null, 2));
  }
}

export default Dashboard;
