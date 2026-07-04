from sysforge.pipeline.stages._partition_plan import _plan_table, _has_existing_partitions
from sysforge.pipeline.stages._bootstrap import BootstrapConfig

def _cfg():
    return BootstrapConfig(target="/mnt", device="/dev/sda", hostname="h",
                           locale="en_US.UTF-8", timezone="UTC", esp_size_mib=512, root_fs="ext4")

def test_plan_table_rows_equal_width():
    lines = _plan_table(_cfg())
    assert len({len(x) for x in lines}) == 1  # all lines same display length
    assert any("Partition plan" in x for x in lines)

def test_has_existing_partitions_false_on_error(monkeypatch):
    import subprocess
    monkeypatch.setattr(subprocess, "run",
        lambda *a, **k: type("R", (), {"returncode": 1, "stdout": ""})())
    assert _has_existing_partitions("/dev/sda") is False
