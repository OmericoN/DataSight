import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api, type ExtractionDetail, type PipelineRunSummary } from "@/shared/api/client";
import { ExtractionPanel } from "./ExtractionPanel";

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

const run = { id: 1, status: "interrupted", config: {}, stages: [] } as unknown as PipelineRunSummary;

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><ExtractionPanel run={run} /></QueryClientProvider>);
}

describe("dataset extraction outcomes", () => {
  it("distinguishes completed zero results from failure and resumes only on request", async () => {
    vi.spyOn(api, "llmStatus").mockResolvedValue({ configured: true, available: true, model: "test", message: "Model available." });
    vi.spyOn(api, "extractions").mockResolvedValue({ total: 3, offset: 0, limit: 25, items: [
      { id: 10, paper_id: "W1", title: "Zero paper", status: "successful", dataset_count: 0, error: null },
      { id: 11, paper_id: "W2", title: "Failed paper", status: "failed", dataset_count: 0, error: "Invalid evidence" },
      { id: 12, paper_id: "W3", title: "Budget paper", status: "interrupted", dataset_count: 0, error: null,
        error_category: 'extraction_budget_exceeded', progress: { phase: 'shared_features', attempts_this_invocation: 50,
          call_limit: 50, time_limit_seconds: 300, elapsed_seconds: 210, completed: {}, calls_by_phase: {} } },
    ] });
    const resume = vi.spyOn(api, "resumeExtractions").mockResolvedValue({ pipeline_run_id: 1, task_id: "t1", status: "queued" });
    mount();
    expect(await screen.findByText("Completed · zero datasets")).toBeInTheDocument();
    expect(screen.getByText("Invalid evidence")).toBeInTheDocument();
    expect(screen.getByText(/50\/50 calls/)).toBeInTheDocument();
    expect(screen.getByText(/Saved checkpoints can be resumed/)).toBeInTheDocument();
    expect(resume).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Resume extraction" }));
    await waitFor(() => expect(resume).toHaveBeenCalledWith(1));
  });

  it("shows source evidence and missing observations without fabricating a score", async () => {
    vi.spyOn(api, "llmStatus").mockResolvedValue({ configured: true, available: true, model: "test", message: "Model available." });
    vi.spyOn(api, "extractions").mockResolvedValue({ total: 1, offset: 0, limit: 25, items: [
      { id: 10, paper_id: "W1", title: "Evidence paper", status: "successful", dataset_count: 1, error: null },
    ] });
    vi.spyOn(api, "extraction").mockResolvedValue({ id: 10, status: "successful", source_sha256: "abc", error: null,
      config: {}, coverage: { covered_characters: 20 }, metrics: {}, requests: [], result: { datasets: [{
        dataset_local_id: "d1", display_name: "Alpha", review_reasons: [], occurrences: [],
        identity: { name: { state: "not_reported", values: [] } }, use: {}, findability: {},
        accessibility: { access_mode: { state: "reported", values: [{ value: "controlled", evidence_ids: ["e1"] }] } },
        interoperability: {}, reusability: {},
      }], evidence: [{ evidence_id: "e1", quote: "Access is controlled.", char_start: 0, char_end: 21, placement: "body", section_heading: "Availability" }], unresolved_mentions: [] },
    } as ExtractionDetail);
    mount();
    fireEvent.click(await screen.findByRole("button", { name: "Evidence paper" }));
    expect(await screen.findByText("Access is controlled.")).toBeInTheDocument();
    expect(screen.getByText("Not reported")).toBeInTheDocument();
    expect(screen.getByText("controlled")).toBeInTheDocument();
    expect(screen.queryByText(/FAIR score/i)).not.toBeInTheDocument();
  });
});
