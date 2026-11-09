from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from datasight.domain.stages import PipelineStage, stage_values
from datasight.interfaces.cli.main import build_parser


def test_stage_values_are_canonical_order():
    assert stage_values() == [
        "discover",
        "download_pdf",
        "grobid_convert",
        "render_document",
        "detect_mentions",
        "extract_features",
        "match_um_dataset",
        "export_insights",
    ]


def test_cli_parses_enqueue_discover():
    args = build_parser().parse_args(
        [
            "enqueue",
            PipelineStage.DISCOVER,
            "--query",
            "dataset reuse",
            "--topic-id",
            "T123",
            "--keyword-term",
            "biobank",
            "--use-um-profile",
        ]
    )
    assert args.command == "enqueue"
    assert args.stage == "discover"
    assert args.query == "dataset reuse"
    assert args.topic_ids == ["T123"]
    assert args.keyword_terms == ["biobank"]
    assert args.use_um_profile is True


def test_cli_parses_doctor():
    args = build_parser().parse_args(["doctor"])
    assert args.command == "doctor"


def test_cli_parses_import_um_datasets():
    args = build_parser().parse_args(["import-um-datasets", "--path", "um.json"])
    assert args.command == "import-um-datasets"
    assert args.path == "um.json"


def test_cli_discovery_preview_defaults_to_adaptive_catalog_funnel():
    defaults = build_parser().parse_args(
        ["discovery-preview"]
    )

    assert defaults.mode == "catalog_funnel"
    assert defaults.discovery_limit == 500
    assert defaults.processing_limit == 50
    assert defaults.max_cost_usd == 0.25


def test_cli_parses_seeded_random_discovery_preview():
    args = build_parser().parse_args(
        ["discovery-preview", "--mode", "random", "--random-seed", "42"]
    )

    assert args.mode == "random"
    assert args.random_seed == 42


def test_cli_parses_english_preview_and_evaluation_export():
    preview = build_parser().parse_args(["discovery-preview", "--language", "en"])
    export = build_parser().parse_args(
        ["evaluation-export", "--run-id", "9", "--output-dir", "storage/evaluation/run-9"]
    )
    assert preview.language == "en"
    assert export.run_id == 9
    assert export.output_dir == "storage/evaluation/run-9"


def test_cli_parses_guided_run_all():
    args = build_parser().parse_args(
        [
            "run-all",
            "--preview-id",
            "preview-123",
            "--processing-limit",
            "5",
            "--um-datasets",
            "um.csv",
            "--output",
            "storage/exports/insights.csv",
        ]
    )
    assert args.command == "run-all"
    assert args.mode == "guided"
    assert args.preview_id == "preview-123"
    assert args.processing_limit == 5
    assert args.um_datasets == "um.csv"
    assert args.output == "storage/exports/insights.csv"
    assert args.render_profile == "pruned"
