"""Unit tests -- fast, logic-only, minimal disk I/O.

Covers:
  - Brace-expansion glob parser
  - Glob matcher helper
  - YAML config validation (missing keys, bad types, negative keep_last)
  - ZipInfo metadata stripping
"""

from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest

from backup.config import _require_field, load_config, parse_glob
from backup.collector import _matches_any
from backup.constants import ZIP_EXTERNAL_ATTR


# ===================================================================
# parse_glob
# ===================================================================
class TestParseGlob:
    """Verify brace expansion and passthrough of standard patterns."""

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "pattern, expected",
        [
            # Standard glob -- returned as single-element list
            ("*.json", ["*.json"]),
            ("*", ["*"]),
            ("data_*.csv", ["data_*.csv"]),
            # Brace expansion -- split into individual patterns
            ("*.{log,txt}", ["*.log", "*.txt"]),
            ("*.{a,b,c}", ["*.a", "*.b", "*.c"]),
            # Brace expansion with prefix and suffix
            ("prefix_{a,b}.suffix", ["prefix_a.suffix", "prefix_b.suffix"]),
            # Whitespace tolerance
            ("  *.json  ", ["*.json"]),
            ("*.{ log , txt }", ["*.log", "*.txt"]),
        ],
        ids=[
            "simple_glob",
            "wildcard_all",
            "prefix_wildcard",
            "two_alternatives",
            "three_alternatives",
            "prefix_and_suffix",
            "whitespace_stripped",
            "brace_inner_whitespace",
        ],
    )
    def test_expansion(self, pattern: str, expected: list[str]) -> None:
        assert parse_glob(pattern) == expected

    @pytest.mark.unit
    def test_single_brace_alternative(self) -> None:
        """A brace group with one item is technically valid -- should still expand."""
        assert parse_glob("*.{log}") == ["*.log"]

    @pytest.mark.unit
    def test_no_braces_passthrough(self) -> None:
        """Patterns without braces must never be modified beyond stripping."""
        assert parse_glob("exact_filename.txt") == ["exact_filename.txt"]


# ===================================================================
# _matches_any
# ===================================================================
class TestMatchesAny:
    """Verify glob matching against filename-only strings."""

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "filename, patterns, expected",
        [
            ("data.json", ["*.json"], True),
            ("data.json", ["*.txt"], False),
            ("data.json", ["*.json", "*.txt"], True),
            ("data.json", ["*.txt", "*.csv"], False),
            ("cache-tmp.json", ["*-tmp.json"], True),
            ("data.json", ["*-tmp.json"], False),
            ("anything", ["*"], True),
        ],
        ids=[
            "single_match",
            "single_miss",
            "multi_first_matches",
            "multi_none_match",
            "exclude_pattern_hit",
            "exclude_pattern_miss",
            "wildcard_all",
        ],
    )
    def test_matching(self, filename: str, patterns: list[str], expected: bool) -> None:
        assert _matches_any(filename, patterns) is expected


# ===================================================================
# _require_field
# ===================================================================
class TestRequireField:
    """Validate the config field accessor."""

    @pytest.mark.unit
    def test_returns_value_when_present(self) -> None:
        assert _require_field({"key": "val"}, "key", "test") == "val"

    @pytest.mark.unit
    def test_raises_on_missing_key(self) -> None:
        with pytest.raises(ValueError, match="Missing required field 'missing'"):
            _require_field({}, "missing", "test context")


# ===================================================================
# load_config validation
# ===================================================================
class TestConfigValidation:
    """Test config loading against invalid inputs.

    Each test writes a minimal YAML file via the write_yaml fixture and
    asserts the expected ValueError.
    """

    @pytest.mark.unit
    def test_missing_backup_name(self, write_yaml) -> None:
        data = {
            "target_base_dir": "/tmp",
            "keep_last": 3,
            "structure": {"f": {"sf": {"source_dir": "/tmp", "include": "*"}}},
        }
        with pytest.raises(ValueError, match="backup_name"):
            load_config(write_yaml(data))

    @pytest.mark.unit
    def test_missing_target_base_dir(self, write_yaml) -> None:
        data = {
            "backup_name": "test",
            "keep_last": 3,
            "structure": {"f": {"sf": {"source_dir": "/tmp", "include": "*"}}},
        }
        with pytest.raises(ValueError, match="target_base_dir"):
            load_config(write_yaml(data))

    @pytest.mark.unit
    def test_missing_keep_last(self, write_yaml) -> None:
        data = {
            "backup_name": "test",
            "target_base_dir": "/tmp",
            "structure": {"f": {"sf": {"source_dir": "/tmp", "include": "*"}}},
        }
        with pytest.raises(ValueError, match="keep_last"):
            load_config(write_yaml(data))

    @pytest.mark.unit
    def test_negative_keep_last(self, write_yaml) -> None:
        data = {
            "backup_name": "test",
            "target_base_dir": "/tmp",
            "keep_last": -1,
            "structure": {"f": {"sf": {"source_dir": "/tmp", "include": "*"}}},
        }
        with pytest.raises(ValueError, match="keep_last.*>= 0"):
            load_config(write_yaml(data))

    @pytest.mark.unit
    def test_keep_last_bool_rejected(self, write_yaml) -> None:
        data = {
            "backup_name": "test",
            "target_base_dir": "/tmp",
            "keep_last": True,
            "structure": {"f": {"sf": {"source_dir": "/tmp", "include": "*"}}},
        }
        with pytest.raises(ValueError, match="keep_last.*integer"):
            load_config(write_yaml(data))

    @pytest.mark.unit
    def test_missing_structure(self, write_yaml) -> None:
        data = {
            "backup_name": "test",
            "target_base_dir": "/tmp",
            "keep_last": 3,
        }
        with pytest.raises(ValueError, match="structure"):
            load_config(write_yaml(data))

    @pytest.mark.unit
    def test_structure_not_a_mapping(self, write_yaml) -> None:
        data = {
            "backup_name": "test",
            "target_base_dir": "/tmp",
            "keep_last": 3,
            "structure": "not_a_dict",
        }
        with pytest.raises(ValueError, match="structure.*mapping"):
            load_config(write_yaml(data))

    @pytest.mark.unit
    def test_folder_not_a_mapping(self, write_yaml) -> None:
        data = {
            "backup_name": "test",
            "target_base_dir": "/tmp",
            "keep_last": 3,
            "structure": {"folder1": "not_a_dict"},
        }
        with pytest.raises(ValueError, match="folder1.*subfolder"):
            load_config(write_yaml(data))

    @pytest.mark.unit
    def test_subfolder_missing_source_dir(self, write_yaml) -> None:
        data = {
            "backup_name": "test",
            "target_base_dir": "/tmp",
            "keep_last": 3,
            "structure": {"f": {"sf": {"include": "*"}}},
        }
        with pytest.raises(ValueError, match="source_dir"):
            load_config(write_yaml(data))

    @pytest.mark.unit
    def test_subfolder_missing_include(self, write_yaml) -> None:
        data = {
            "backup_name": "test",
            "target_base_dir": "/tmp",
            "keep_last": 3,
            "structure": {"f": {"sf": {"source_dir": "/tmp"}}},
        }
        with pytest.raises(ValueError, match="include"):
            load_config(write_yaml(data))

    @pytest.mark.unit
    def test_include_must_be_string(self, write_yaml) -> None:
        data = {
            "backup_name": "test",
            "target_base_dir": "/tmp",
            "keep_last": 3,
            "structure": {"f": {"sf": {"source_dir": "/tmp", "include": ["*.json"]}}},
        }
        with pytest.raises(ValueError, match="include.*string"):
            load_config(write_yaml(data))

    @pytest.mark.unit
    def test_empty_structure(self, write_yaml) -> None:
        """Structure with zero subfolders is invalid."""
        data = {
            "backup_name": "test",
            "target_base_dir": "/tmp",
            "keep_last": 3,
            "structure": {},
        }
        with pytest.raises(ValueError, match="at least one subfolder"):
            load_config(write_yaml(data))

    @pytest.mark.unit
    def test_malformed_yaml(self, tmp_path: Path) -> None:
        bad_yaml = tmp_path / "bad.yaml"
        bad_yaml.write_text(":\n  - :\n    invalid: [unterminated", encoding="utf-8")
        with pytest.raises(ValueError, match="Malformed YAML"):
            load_config(bad_yaml)

    @pytest.mark.unit
    def test_yaml_root_not_mapping(self, tmp_path: Path) -> None:
        """A YAML list at root level should fail."""
        list_yaml = tmp_path / "list.yaml"
        list_yaml.write_text("- item1\n- item2\n", encoding="utf-8")
        with pytest.raises(ValueError, match="root must be a mapping"):
            load_config(list_yaml)

    @pytest.mark.unit
    def test_nonexistent_config_file(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="does not exist"):
            load_config(tmp_path / "ghost.yaml")

    @pytest.mark.unit
    def test_backup_name_rejects_path_separators(self, write_yaml) -> None:
        data = {
            "backup_name": "../escape/out",
            "target_base_dir": "/tmp",
            "keep_last": 3,
            "structure": {"f": {"sf": {"source_dir": "/tmp", "include": "*"}}},
        }
        with pytest.raises(ValueError, match="backup_name.*path separators"):
            load_config(write_yaml(data))

    @pytest.mark.unit
    def test_valid_config_parses_correctly(self, write_yaml) -> None:
        """Positive test: a fully valid config must parse without error."""
        data = {
            "backup_name": "mybackup",
            "target_base_dir": "/srv/backups",
            "keep_last": 5,
            "structure": {
                "data": {
                    "json_files": {
                        "source_dir": "/opt/app/data",
                        "include": "*.json",
                        "exclude": "*-tmp.json",
                    },
                },
                "logs": {
                    "app_logs": {
                        "source_dir": "/var/log/app",
                        "include": "*.{log,txt}",
                    },
                },
            },
        }
        config = load_config(write_yaml(data))

        assert config.backup_name == "mybackup"
        assert config.target_base_dir == Path("/srv/backups")
        assert config.keep_last == 5
        assert len(config.subfolders) == 2

        json_sf = config.subfolders[0]
        assert json_sf.folder_name == "data"
        assert json_sf.subfolder_name == "json_files"
        assert json_sf.include == "*.json"
        assert json_sf.exclude == "*-tmp.json"

        log_sf = config.subfolders[1]
        assert log_sf.folder_name == "logs"
        assert log_sf.exclude is None

    @pytest.mark.unit
    def test_keep_last_zero_is_valid(self, write_yaml) -> None:
        """keep_last=0 means unlimited retention -- must be accepted."""
        data = {
            "backup_name": "test",
            "target_base_dir": "/tmp",
            "keep_last": 0,
            "structure": {"f": {"sf": {"source_dir": "/tmp", "include": "*"}}},
        }
        config = load_config(write_yaml(data))
        assert config.keep_last == 0


# ===================================================================
# _make_clean_zipinfo -- metadata stripping
# ===================================================================
class TestMakeCleanZipInfo:
    """Verify that ZipInfo entries carry sanitised metadata."""

    @pytest.mark.unit
    def test_external_attr_is_neutral(self, tmp_path: Path) -> None:
        """All entries must have the fixed 0644 permission, not the source's."""
        from backup.archive import _make_clean_zipinfo

        src = tmp_path / "secret.key"
        src.write_text("supersecret")
        # Even if the source has 0o700, the ZipInfo must carry 0o644
        src.chmod(0o700)

        info = _make_clean_zipinfo("folder/secret.key", src)

        assert info.external_attr == ZIP_EXTERNAL_ATTR
        assert info.filename == "folder/secret.key"
        assert info.compress_type == zipfile.ZIP_DEFLATED

    @pytest.mark.unit
    def test_mod_time_preserved(self, tmp_path: Path) -> None:
        """The modification timestamp should come from the source file."""
        from backup.archive import _make_clean_zipinfo

        src = tmp_path / "file.txt"
        src.write_text("content")

        info = _make_clean_zipinfo("file.txt", src)

        # date_time is a 6-tuple: (year, month, day, hour, minute, second)
        assert len(info.date_time) == 6
        assert info.date_time[0] >= 2020  # sanity: year is reasonable

    @pytest.mark.unit
    def test_mod_time_before_1980_is_clamped(self, tmp_path: Path) -> None:
        """ZIP metadata must be normalized to the earliest supported timestamp."""
        from backup.archive import _make_clean_zipinfo

        src = tmp_path / "ancient.txt"
        src.write_text("content")
        os.utime(src, (0, 0))

        info = _make_clean_zipinfo("ancient.txt", src)

        assert info.date_time == (1980, 1, 1, 0, 0, 0)
