import pytest

from benchmarks.vllm import mark_interrupted as script


def test_stopped_run_marks_only_running_papers_and_requires_no_active_lock(monkeypatch):
    class Repo:
        locked = True
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def __init__(self): self.cursor = self
        def extraction_lock(self, run_id): return self.locked
        def execute(self, query, params): assert params == (7,)
        def fetchall(self): return [{"id": 3}, {"id": 4}]
        def fail_publication_extraction(self, extraction_id, status, reason):
            calls.append((extraction_id, status))
        def interrupt_extraction_run(self, run_id, reason): calls.append((run_id, "run"))

    calls = []
    monkeypatch.setattr(script, "PipelineRepository", Repo)
    assert script.mark_interrupted(7) == 2
    assert calls == [(3, "interrupted"), (4, "interrupted"), (7, "run")]
    Repo.locked = False
    with pytest.raises(ValueError, match="still active"):
        script.mark_interrupted(7)
