from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


def test_experiment_lineage_migration_is_the_single_head():
    backend_root = Path(__file__).resolve().parents[1]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "migrations"))
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == ["20261028_0013"]
