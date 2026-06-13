"""
test_kbuild_map.py — kbuild Makefile parsing and module→kconfig cache.

Parser fixtures are miniature kernel-tree layouts under tmp_path; no real
kernel source is required.
"""
from sysforge.primitives import kbuild_map


def _tree(tmp_path, files):
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return tmp_path


# ---------------------------------------------------------------------------
# parse_kbuild_tree
# ---------------------------------------------------------------------------

def test_parse_basic_obj_line(tmp_path):
    root = _tree(tmp_path, {
        "drivers/nvme/host/Makefile": "obj-$(CONFIG_BLK_DEV_NVME) += nvme.o\n",
    })
    assert kbuild_map.parse_kbuild_tree(root) == {"nvme": "CONFIG_BLK_DEV_NVME"}


def test_parse_multi_object_line(tmp_path):
    root = _tree(tmp_path, {
        "drivers/foo/Makefile": "obj-$(CONFIG_FOO) += alpha.o beta.o\n",
    })
    assert kbuild_map.parse_kbuild_tree(root) == {
        "alpha": "CONFIG_FOO", "beta": "CONFIG_FOO",
    }


def test_parse_backslash_continuation(tmp_path):
    root = _tree(tmp_path, {
        "drivers/foo/Makefile": (
            "obj-$(CONFIG_FOO) += alpha.o \\\n"
            "                     beta.o\n"
        ),
    })
    assert kbuild_map.parse_kbuild_tree(root) == {
        "alpha": "CONFIG_FOO", "beta": "CONFIG_FOO",
    }


def test_parse_dash_normalized_to_underscore(tmp_path):
    root = _tree(tmp_path, {
        "drivers/nvme/host/Makefile": "obj-$(CONFIG_NVME_CORE) += nvme-core.o\n",
    })
    assert kbuild_map.parse_kbuild_tree(root) == {"nvme_core": "CONFIG_NVME_CORE"}


def test_parse_assignment_forms(tmp_path):
    root = _tree(tmp_path, {
        "drivers/a/Makefile": "obj-$(CONFIG_A) := alpha.o\n",
        "drivers/b/Makefile": "obj-$(CONFIG_B) = beta.o\n",
    })
    assert kbuild_map.parse_kbuild_tree(root) == {
        "alpha": "CONFIG_A", "beta": "CONFIG_B",
    }


def test_parse_subdir_path_object_uses_basename(tmp_path):
    root = _tree(tmp_path, {
        "drivers/foo/Makefile": "obj-$(CONFIG_FOO) += sub/alpha.o\n",
    })
    assert kbuild_map.parse_kbuild_tree(root) == {"alpha": "CONFIG_FOO"}


def test_parse_reads_kbuild_files(tmp_path):
    root = _tree(tmp_path, {
        "drivers/foo/Kbuild": "obj-$(CONFIG_FOO) += alpha.o\n",
    })
    assert kbuild_map.parse_kbuild_tree(root) == {"alpha": "CONFIG_FOO"}


def test_parse_ignores_obj_y_and_obj_m(tmp_path):
    root = _tree(tmp_path, {
        "drivers/foo/Makefile": "obj-y += core.o\nobj-m += extra.o\n",
    })
    assert kbuild_map.parse_kbuild_tree(root) == {}


def test_parse_ignores_directory_entries(tmp_path):
    root = _tree(tmp_path, {
        "drivers/Makefile": "obj-$(CONFIG_FOO) += foo/\n",
    })
    assert kbuild_map.parse_kbuild_tree(root) == {}


def test_parse_skips_non_driver_top_dirs(tmp_path):
    root = _tree(tmp_path, {
        "Documentation/Makefile": "obj-$(CONFIG_DOC) += doc.o\n",
        "tools/Makefile": "obj-$(CONFIG_TOOL) += tool.o\n",
        "scripts/Makefile": "obj-$(CONFIG_SCRIPT) += script.o\n",
        "samples/Makefile": "obj-$(CONFIG_SAMPLE) += sample.o\n",
        "drivers/foo/Makefile": "obj-$(CONFIG_FOO) += alpha.o\n",
    })
    assert kbuild_map.parse_kbuild_tree(root) == {"alpha": "CONFIG_FOO"}


def test_parse_first_wins_on_duplicate_module(tmp_path):
    # Sorted walk: drivers/a before drivers/b — the first mapping sticks.
    root = _tree(tmp_path, {
        "drivers/a/Makefile": "obj-$(CONFIG_FIRST) += alpha.o\n",
        "drivers/b/Makefile": "obj-$(CONFIG_SECOND) += alpha.o\n",
    })
    assert kbuild_map.parse_kbuild_tree(root) == {"alpha": "CONFIG_FIRST"}


def test_parse_top_level_makefile_included(tmp_path):
    root = _tree(tmp_path, {
        "Makefile": "obj-$(CONFIG_TOP) += top.o\n",
    })
    assert kbuild_map.parse_kbuild_tree(root) == {"top": "CONFIG_TOP"}


# ---------------------------------------------------------------------------
# save_map / load_map
# ---------------------------------------------------------------------------

def test_cache_round_trip_with_provenance(tmp_path):
    path = tmp_path / kbuild_map.KBUILD_MAP_FILENAME
    kbuild_map.save_map(path, {"nvme": "CONFIG_BLK_DEV_NVME"}, "6.10.0-test")
    assert kbuild_map.load_map(path) == (
        {"nvme": "CONFIG_BLK_DEV_NVME"}, "6.10.0-test",
    )


def test_cache_none_release_round_trips_empty(tmp_path):
    path = tmp_path / "map.json"
    kbuild_map.save_map(path, {"a": "CONFIG_A"}, None)
    assert kbuild_map.load_map(path) == ({"a": "CONFIG_A"}, "")


def test_load_missing_returns_none(tmp_path):
    assert kbuild_map.load_map(tmp_path / "nonexistent.json") is None


def test_load_corrupt_returns_none(tmp_path):
    path = tmp_path / "map.json"
    path.write_text("not json {")
    assert kbuild_map.load_map(path) is None


def test_load_wrong_shape_returns_none(tmp_path):
    path = tmp_path / "map.json"
    path.write_text('["a", "list"]')
    assert kbuild_map.load_map(path) is None
    path.write_text('{"entries": "not-a-dict"}')
    assert kbuild_map.load_map(path) is None


def test_load_drops_non_string_entries(tmp_path):
    path = tmp_path / "map.json"
    path.write_text('{"kernel_release": "6.10", "entries": {"good": "CONFIG_G", "bad": 7}}')
    assert kbuild_map.load_map(path) == ({"good": "CONFIG_G"}, "6.10")
