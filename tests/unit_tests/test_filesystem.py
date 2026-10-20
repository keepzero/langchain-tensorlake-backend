"""Cloud Volume backend contracts; network failures are never disguised as missing files."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from deepagents.backends import CompositeBackend, StateBackend
from tensorlake.filesystem import FileEntry
from tensorlake.filesystem.exceptions import FileNotFoundInFilesystemError, FilesystemAPIError

from langchain_tensorlake_backend import TensorlakeFilesystemBackend


@pytest.fixture
def volume_backend():
    fs = Mock()
    fs.status.return_value = SimpleNamespace(version_id="fixed-version")
    return fs, TensorlakeFilesystemBackend(lambda: fs, hidden_names=(".mytrade",))


def test_read_has_framework_pagination_and_binary_handling(volume_backend):
    fs, backend = volume_backend
    fs.read_file.return_value = b"one\ntwo\nthree\n"
    result = backend.read("/report.md", offset=1, limit=1)
    assert result.error is None
    assert result.file_data["content"] == "two\n"
    assert result.total_lines == 3 and result.next_offset == 2
    fs.read_file.return_value = b"\x00\xff"
    assert backend.read("/chart.png").file_data == {"content": "AP8=", "encoding": "base64"}


def test_zero_line_read_never_accesses_storage(volume_backend):
    fs, backend = volume_backend
    assert backend.read("/report.md", limit=0).no_lines_requested
    fs.read_file.assert_not_called()


def test_empty_volume_does_not_read_a_nonexistent_head(volume_backend):
    fs, backend = volume_backend
    fs.name = "empty-volume"
    fs.status.return_value = SimpleNamespace(version_id=None)
    fs.read_file.side_effect = FilesystemAPIError(400, "empty head")
    assert backend.read("/report.md").error == "file_not_found"
    assert backend.write("/report.md", "first").error is None
    fs.read_file.assert_not_called()
    fs.write_file.assert_called_once_with("report.md", b"first")


@pytest.mark.parametrize("path", ["../x", "/../x", "/a/../x", "/x\x00", "/x\\y", "/.mytrade/x"])
def test_invalid_paths_never_reach_storage(volume_backend, path):
    fs, backend = volume_backend
    assert backend.read(path).error
    assert backend.write(path, "data").error
    fs.read_file.assert_not_called()
    fs.write_file.assert_not_called()


def test_write_does_not_replace_existing_file_or_mask_access_failures(volume_backend):
    fs, backend = volume_backend
    fs.read_file.return_value = b"existing"
    assert "file_exists" in backend.write("/report.md", "new").error
    fs.write_file.assert_not_called()
    fs.read_file.side_effect = FilesystemAPIError(403, "Authorization: private-value")
    assert backend.write("/report.md", "new").error == "permission_denied"
    fs.write_file.assert_not_called()
    fs.read_file.side_effect = FileNotFoundInFilesystemError("owned", "report.md")
    assert backend.write("/report.md", "new").path == "/report.md"
    fs.write_file.assert_called_once_with("report.md", b"new")


def test_edit_validates_match_before_mutation(volume_backend):
    fs, backend = volume_backend
    fs.read_file.return_value = b"x x"
    assert backend.edit("/r.md", "x", "y").error
    fs.write_file.assert_not_called()
    result = backend.edit("/r.md", "x", "y", replace_all=True)
    assert result.occurrences == 2
    fs.write_file.assert_called_once_with("r.md", "y y")


def test_batch_upload_is_one_atomic_sdk_publication(volume_backend):
    fs, backend = volume_backend
    result = backend.upload_files([("/a.txt", b"a"), ("/b.bin", b"\0")])
    assert all(item.error is None for item in result)
    fs.write_files.assert_called_once_with({"a.txt": b"a", "b.bin": b"\0"})
    fs.write_files.reset_mock()
    assert all(item.error for item in backend.upload_files([("/ok", b"x"), ("/../bad", b"x")]))
    fs.write_files.assert_not_called()


def test_search_pins_directory_and_content_reads_to_one_version(volume_backend):
    fs, backend = volume_backend
    fs.list_files.side_effect = lambda path, version: {
        "": [
            FileEntry(name="reports", path="reports", kind="directory"),
            FileEntry(name=".mytrade", path=".mytrade", kind="directory"),
        ],
        "reports": [FileEntry(name="a.md", path="reports/a.md", size=7)],
    }[path]
    fs.read_file.return_value = b"needle\nneedle"
    assert backend.glob("**/*.md").matches[0]["path"] == "/reports/a.md"
    result = backend.grep("needle", max_count=1)
    assert result.matches == [{"path": "/reports/a.md", "line": 1, "text": "needle"}]
    assert result.truncated
    assert all(c.kwargs["version"] == "fixed-version" for c in fs.list_files.call_args_list)
    fs.read_file.assert_called_with("reports/a.md", version="fixed-version")


def test_grep_accepts_a_single_file_path(volume_backend):
    fs, backend = volume_backend
    fs.list_files.return_value = [FileEntry(name="a.md", path="reports/a.md", size=6)]
    fs.read_file.return_value = b"needle"
    assert backend.grep("needle", path="/reports/a.md", glob="*.md").matches == [
        {"path": "/reports/a.md", "line": 1, "text": "needle"}
    ]
    fs.list_files.assert_called_once_with("reports", version="fixed-version")


def test_private_artifact_route_is_explicit_and_main_has_no_execute(volume_backend):
    fs, backend = volume_backend
    assert not hasattr(backend, "execute")
    assert backend.read("/.mytrade/result.txt").error
    internal = TensorlakeFilesystemBackend(lambda: fs)
    fs.read_file.return_value = b"framework result"
    assert internal.read("/.mytrade/result.txt").error is None
    composite = CompositeBackend(default=StateBackend(), routes={"/workspace/": backend})
    assert composite.read("/workspace/report.md").error is None


def test_storage_error_never_exposes_provider_credentials(volume_backend):
    fs, backend = volume_backend
    fs.read_file.side_effect = FilesystemAPIError(
        503, "Authorization: private-value https://host/?token=secret"
    )
    result = backend.read("/report.md")
    assert "private-value" not in result.error and "secret" not in result.error
    assert "storage_unavailable" in result.error
