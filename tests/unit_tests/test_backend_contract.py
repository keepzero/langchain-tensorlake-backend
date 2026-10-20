"""Portable backend policies and the asynchronous BackendProtocol surface."""

from contextvars import ContextVar
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from deepagents.backends import CompositeBackend, StateBackend
from deepagents.backends.protocol import BackendProtocol
from tensorlake.filesystem import FileEntry, Filesystem
from tensorlake.filesystem.exceptions import FilesystemAPIError

from langchain_tensorlake_backend import TensorlakeFilesystemBackend


@pytest.fixture
def filesystem():
    filesystem = Mock(spec=Filesystem)
    filesystem.name = "test-volume"
    filesystem.status.return_value = SimpleNamespace(version_id="head")
    return filesystem


def test_concrete_filesystem_and_public_exports(filesystem):
    from langchain_tensorlake_backend.backends import (
        TensorlakeFilesystemBackend as Exported,
    )

    backend = TensorlakeFilesystemBackend(filesystem)
    filesystem.read_file.return_value = b"content"
    assert isinstance(backend, BackendProtocol)
    assert Exported is TensorlakeFilesystemBackend
    assert backend.read("/report.md").file_data["content"] == "content"
    assert not hasattr(backend, "execute")
    filesystem.snapshot.assert_not_called()


def test_no_application_specific_names_are_hidden_by_default(filesystem):
    filesystem.read_file.return_value = b"visible"
    filesystem.list_files.return_value = [
        FileEntry(name=".mytrade", path=".mytrade", kind="directory")
    ]
    backend = TensorlakeFilesystemBackend(filesystem)
    assert backend.read("/.mytrade/report.md").error is None
    assert backend.ls("/").entries[0]["path"] == "/.mytrade/"


def test_custom_exclusions_apply_at_every_depth(filesystem):
    backend = TensorlakeFilesystemBackend(filesystem, hidden_names=(".internal",))
    filesystem.list_files.return_value = [
        FileEntry(name=".internal", path="reports/.internal", kind="directory"),
        FileEntry(name="visible.txt", path="reports/visible.txt"),
        FileEntry(name="link", path="reports/link", kind="symlink"),
    ]
    assert backend.read("/reports/.internal/a").error.startswith("permission_denied")
    assert [row["path"] for row in backend.ls("/reports").entries] == ["/reports/visible.txt"]
    assert backend.glob("*", path="/reports").matches[0]["path"] == "/reports/visible.txt"
    filesystem.read_file.assert_not_called()


@pytest.mark.parametrize("option", ["max_file_bytes", "max_search_files", "max_search_bytes"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_processing_budgets_require_positive_integers(filesystem, option, value):
    with pytest.raises(ValueError, match=option):
        TensorlakeFilesystemBackend(filesystem, **{option: value})


@pytest.mark.parametrize("hidden_names", [".private", ("",), ("a/b",), ("..",), ("a\\b",)])
def test_hidden_names_are_individual_components(filesystem, hidden_names):
    with pytest.raises(ValueError, match="hidden_names"):
        TensorlakeFilesystemBackend(filesystem, hidden_names=hidden_names)


def test_configurable_file_limit_and_empty_upload(filesystem):
    backend = TensorlakeFilesystemBackend(filesystem, max_file_bytes=3)
    filesystem.status.return_value = SimpleNamespace(version_id=None)
    assert "file_too_large" in backend.write("/a", "four").error
    assert backend.upload_files([]) == []
    assert backend.upload_files([("/a", b"four")])[0].error
    filesystem.write_files.assert_not_called()
    filesystem.status.return_value = SimpleNamespace(version_id="head")
    filesystem.read_file.return_value = b"four"
    assert "file_too_large" in backend.read("/a.txt").error


def test_atomic_upload_rejects_normalized_duplicate_paths(filesystem):
    backend = TensorlakeFilesystemBackend(filesystem)
    result = backend.upload_files([("/a/b", b"first"), ("/a//b", b"second")])
    assert all(row.error for row in result)
    filesystem.write_files.assert_not_called()


@pytest.mark.parametrize("path", ["/", "//", "//escape", "/~", "/a/../b"])
def test_invalid_file_paths_do_not_access_sdk(filesystem, path):
    backend = TensorlakeFilesystemBackend(filesystem)
    assert backend.read(path).error
    assert backend.write(path, "x").error
    assert backend.edit(path, "a", "b").error
    assert backend.delete(path).error
    filesystem.status.assert_not_called()
    filesystem.delete_file.assert_not_called()


def test_search_entry_and_byte_budgets(filesystem):
    filesystem.list_files.return_value = [
        FileEntry(name="a.txt", path="a.txt"),
        FileEntry(name="b.txt", path="b.txt"),
    ]
    filesystem.read_file.return_value = b"match"
    backend = TensorlakeFilesystemBackend(filesystem, max_search_files=1)
    assert "search_too_large" in backend.glob("*").error
    backend = TensorlakeFilesystemBackend(filesystem, max_search_bytes=5)
    result = backend.grep("match")
    assert result.truncated and len(result.matches) == 1
    assert all(row.kwargs["version"] == "head" for row in filesystem.read_file.call_args_list)


def test_grep_is_literal_and_file_filters_are_respected(filesystem):
    filesystem.list_files.return_value = [
        FileEntry(name="a.md", path="a.md"),
        FileEntry(name="b.txt", path="b.txt"),
    ]
    filesystem.read_file.return_value = b"[literal]\nordinary\n"
    backend = TensorlakeFilesystemBackend(filesystem)
    assert backend.grep("[literal]", glob="*.md").matches == [
        {"path": "/a.md", "line": 1, "text": "[literal]"}
    ]
    filesystem.read_file.assert_called_once_with("a.md", version="head")


def test_binary_download_partial_failure_and_redaction(filesystem):
    backend = TensorlakeFilesystemBackend(filesystem)
    filesystem.read_file.side_effect = [
        b"\x00\xff",
        FilesystemAPIError(403, "secret-provider-token"),
    ]
    result = backend.download_files(["/a.bin", "/bad.bin", "/../escape"])
    assert result[0].content == b"\x00\xff"
    assert result[1].error == "permission_denied"
    assert result[2].error == "invalid_path"
    assert "secret-provider-token" not in repr(result)


def test_create_only_write_edit_and_delete(filesystem):
    backend = TensorlakeFilesystemBackend(filesystem)
    filesystem.read_file.return_value = b"old"
    assert "file_exists" in backend.write("/file.txt", "new").error
    assert backend.edit("/file.txt", "old", "new").occurrences == 1
    filesystem.write_file.assert_called_once_with("file.txt", "new")
    assert backend.delete("/file.txt").path == "/file.txt"
    filesystem.delete_file.assert_called_once_with("file.txt")
    filesystem.snapshot.assert_not_called()


async def test_async_methods_preserve_resolver_context_and_arbitrary_composite_route(filesystem):
    current = ContextVar("current_filesystem")
    current.set(filesystem)
    filesystem.read_file.return_value = b"one\ntwo\n"
    filesystem.list_files.return_value = [FileEntry(name="a.md", path="a.md")]
    backend = TensorlakeFilesystemBackend(lambda: current.get())
    composite = CompositeBackend(default=StateBackend(), routes={"/artifacts/": backend})
    assert (await composite.aread("/artifacts/a.md", offset=1, limit=1)).file_data[
        "content"
    ] == "two\n"
    assert (await backend.als("/")).entries[0]["path"] == "/a.md"
    assert (await backend.aglob("*.md")).matches[0]["path"] == "/a.md"
    assert (await backend.agrep("two")).matches[0]["line"] == 2
    assert (await backend.aedit("/a.md", "two", "changed")).occurrences == 1
    assert (await backend.adelete("/a.md")).error is None
    assert (await backend.aupload_files([("/b", b"binary")]))[0].error is None
    assert (await backend.adownload_files(["/a.md"]))[0].content == b"one\ntwo\n"
    filesystem.status.return_value = SimpleNamespace(version_id=None)
    assert (await backend.awrite("/new.md", "first")).error is None
