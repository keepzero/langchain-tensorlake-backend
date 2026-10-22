"""Deep Agents filesystem backend backed by Tensorlake Cloud Volumes."""

import base64
from collections.abc import Callable
from functools import wraps
from pathlib import PurePosixPath
from threading import RLock

from deepagents.backends.protocol import (
    BackendProtocol,
    DeleteResult,
    EditResult,
    FileData,
    FileDownloadResponse,
    FileUploadResponse,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)
from deepagents.backends.utils import (
    _get_backend_read_file_type,
    compile_grep_include_glob,
    perform_string_replacement,
    slice_read_response,
)
from tensorlake.filesystem import Filesystem
from tensorlake.filesystem.exceptions import (
    FileNotFoundInFilesystemError,
    FilesystemAPIError,
    FilesystemError,
)

MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_SEARCH_FILES = 10_000
MAX_SEARCH_BYTES = 64 * 1024 * 1024


def _volume_path(path: str, *, allow_root: bool = False) -> str:
    """Translate virtual absolute paths to filesystem-relative paths."""
    parsed = PurePosixPath(path)
    if (
        not path.startswith("/")
        or path.startswith("//")
        or "\x00" in path
        or "\\" in path
        or any(part in {"..", "~"} for part in parsed.parts)
        or (not allow_root and not parsed.name)
    ):
        raise ValueError("invalid_path: use an absolute path within the filesystem")
    return str(parsed).lstrip("/")


def _storage_error(error: FilesystemError) -> str:
    """Raw provider errors can contain signed URLs or credentials."""
    if isinstance(error, FileNotFoundInFilesystemError):
        return "file_not_found"
    if isinstance(error, FilesystemAPIError) and error.status_code in {401, 403}:
        return "permission_denied"
    if isinstance(error, FilesystemAPIError):
        return f"storage_unavailable: {type(error).__name__}, HTTP {error.status_code}"
    return f"storage_unavailable: {type(error).__name__}"


def _file_result(result_type):
    """Keep model-facing errors free of raw provider responses."""

    def decorate(method):
        @wraps(method)
        def call(*args, **kwargs):
            try:
                with args[0]._lock:
                    return method(*args, **kwargs)
            except ValueError as error:
                return result_type(error=str(error))
            except FilesystemError as error:
                return result_type(error=_storage_error(error))

        return call

    return decorate


class TensorlakeFilesystemBackend(BackendProtocol):
    """Shared file tools without a shell or a sandbox dependency.

    Pass a Tensorlake ``Filesystem`` or a synchronous filesystem resolver.
    All paths are rooted at that filesystem. CompositeBackend may mount this
    backend under any virtual prefix. File tools never select a different volume.

    Args:
        filesystem: Existing filesystem or resolver called within each operation.
        max_file_bytes: Maximum bytes read or written by a single file operation.
        max_search_files: Maximum entries visited by a directory search.
        max_search_bytes: Maximum content bytes inspected by grep.

    Each instance serializes its own operations with an internal reentrant lock.
    Cross-backend coordination, snapshot retention, sandbox mounts, and task
    lifecycle belong to the caller.
    Async methods are inherited from BackendProtocol and run sync work in threads.
    """

    def __init__(
        self,
        filesystem: Filesystem | Callable[[], Filesystem],
        *,
        max_file_bytes: int = MAX_FILE_BYTES,
        max_search_files: int = MAX_SEARCH_FILES,
        max_search_bytes: int = MAX_SEARCH_BYTES,
    ) -> None:
        if isinstance(filesystem, Filesystem):
            self._filesystem = lambda: filesystem
        elif callable(filesystem):
            self._filesystem = filesystem
        else:
            raise TypeError("filesystem must be a Tensorlake Filesystem or synchronous resolver")
        for name, value in {
            "max_file_bytes": max_file_bytes,
            "max_search_files": max_search_files,
            "max_search_bytes": max_search_bytes,
        }.items():
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self._lock = RLock()
        self._max_file_bytes = max_file_bytes
        self._max_search_files = max_search_files
        self._max_search_bytes = max_search_bytes

    def _path(self, path: str, *, allow_root: bool = False) -> str:
        return _volume_path(path, allow_root=allow_root)

    @_file_result(LsResult)
    def ls(self, path: str) -> LsResult:
        relative = self._path(path, allow_root=True)
        fs = self._filesystem()
        version = fs.status().version_id
        if version is None:
            return LsResult(entries=[])
        entries = []
        for row in fs.list_files(relative, version=version):
            if row.is_symlink:
                continue
            self._path("/" + row.path)
            entries.append(
                {
                    "path": "/" + row.path.rstrip("/") + ("/" if row.is_dir else ""),
                    "is_dir": row.is_dir,
                    "size": row.size or 0,
                }
            )
        return LsResult(entries=entries)

    def _read_bytes(self, fs: Filesystem, path: str, version: str | None = None) -> bytes:
        if version is None:
            version = fs.status().version_id
        if version is None:
            raise FileNotFoundInFilesystemError(fs.name, path)
        data = fs.read_file(path, version=version)
        if len(data) > self._max_file_bytes:
            raise ValueError(f"file_too_large: maximum file size is {self._max_file_bytes} bytes")
        return data

    @_file_result(ReadResult)
    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        relative = self._path(file_path)
        if int(limit) <= 0:
            return ReadResult(no_lines_requested=True)
        data = self._read_bytes(self._filesystem(), relative)
        if _get_backend_read_file_type(file_path) != "text":
            if len(data) > 500 * 1024:
                return ReadResult(error="Binary file exceeds maximum preview size")
            return ReadResult(
                file_data=FileData(
                    content=base64.b64encode(data).decode("ascii"), encoding="base64"
                )
            )
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return ReadResult(error="binary_file: use download_files for raw bytes")
        return slice_read_response(FileData(content=text, encoding="utf-8"), offset, limit)

    @_file_result(WriteResult)
    def write(self, file_path: str, content: str) -> WriteResult:
        relative = self._path(file_path)
        data = content.encode("utf-8")
        if len(data) > self._max_file_bytes:
            return WriteResult(
                error=f"file_too_large: maximum file size is {self._max_file_bytes} bytes"
            )
        with self._lock:
            fs = self._filesystem()
            version = fs.status().version_id
            if version is not None:
                try:
                    fs.read_file(relative, version=version)
                except FileNotFoundInFilesystemError:
                    pass
                else:
                    return WriteResult(error="file_exists: use edit_file for an existing file")
            fs.write_file(relative, data)
        return WriteResult(path="/" + relative)

    @_file_result(EditResult)
    def edit(
        self, file_path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> EditResult:
        relative = self._path(file_path)
        with self._lock:
            fs = self._filesystem()
            try:
                text = self._read_bytes(fs, relative).decode("utf-8")
            except UnicodeDecodeError:
                return EditResult(error="binary_file: cannot edit as text")
            result = perform_string_replacement(text, old_string, new_string, replace_all)
            if isinstance(result, str):
                return EditResult(error=result)
            changed, count = result
            if len(changed.encode("utf-8")) > self._max_file_bytes:
                return EditResult(
                    error=f"file_too_large: maximum file size is {self._max_file_bytes} bytes"
                )
            fs.write_file(relative, changed)
        return EditResult(path="/" + relative, occurrences=count)

    @_file_result(DeleteResult)
    def delete(self, file_path: str) -> DeleteResult:
        relative = self._path(file_path)
        with self._lock:
            self._filesystem().delete_file(relative)
        return DeleteResult(path="/" + relative)

    def _walk(self, fs: Filesystem, path: str, version: str):
        pending = [path]
        visited = 0
        while pending:
            directory = pending.pop()
            for row in fs.list_files(directory, version=version):
                visited += 1
                if visited > self._max_search_files:
                    raise ValueError("search_too_large: narrow the search path")
                if row.is_symlink:
                    continue
                self._path("/" + row.path)
                if row.is_dir:
                    pending.append(row.path)
                else:
                    yield row

    def _grep_entries(self, fs: Filesystem, path: str, version: str):
        if path:
            parent = str(PurePosixPath(path).parent)
            parent = "" if parent == "." else parent
            entry = next(
                (row for row in fs.list_files(parent, version=version) if row.path == path), None
            )
            if entry is None or entry.is_symlink:
                raise FileNotFoundInFilesystemError(fs.name, path)
            if not entry.is_dir:
                yield entry
                return
        yield from self._walk(fs, path, version)

    @_file_result(GlobResult)
    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        relative = self._path(path or "/", allow_root=True)
        matcher = compile_grep_include_glob(pattern)
        fs = self._filesystem()
        version = fs.status().version_id
        if version is None:
            return GlobResult(matches=[])
        matches = []
        for row in self._walk(fs, relative, version):
            name = str(PurePosixPath(row.path).relative_to(relative or "."))
            if matcher(name):
                matches.append({"path": "/" + row.path, "is_dir": False, "size": row.size or 0})
        return GlobResult(matches=matches)

    @_file_result(GrepResult)
    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ) -> GrepResult:
        relative = self._path(path or "/", allow_root=True)
        matcher = compile_grep_include_glob(glob or "*")
        fs = self._filesystem()
        version = fs.status().version_id
        if version is None:
            return GrepResult(matches=[])
        matches = []
        total = 0
        maximum = max(0, max_count) if max_count is not None else 1000
        for row in self._grep_entries(fs, relative, version):
            if row.path == relative:
                name = PurePosixPath(row.path).name
            else:
                name = str(PurePosixPath(row.path).relative_to(relative or "."))
            if not matcher(name) or _get_backend_read_file_type(row.path) != "text":
                continue
            data = self._read_bytes(fs, row.path, version)
            total += len(data)
            if total > self._max_search_bytes:
                return GrepResult(matches=matches, truncated=True)
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if pattern in line:
                    if len(matches) >= maximum:
                        return GrepResult(matches=matches, truncated=True)
                    matches.append({"path": "/" + row.path, "line": number, "text": line})
        return GrepResult(matches=matches)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        try:
            uploads = {self._path(path): data for path, data in files}
            if any(len(data) > self._max_file_bytes for data in uploads.values()):
                raise ValueError("file_too_large")
            if len(uploads) != len(files):
                raise ValueError("duplicate_path")
            if uploads:
                with self._lock:
                    self._filesystem().write_files(uploads)
            return [FileUploadResponse(path=path) for path, _ in files]
        except ValueError:
            return [FileUploadResponse(path=path, error="invalid_path") for path, _ in files]
        except FilesystemError as error:
            return [FileUploadResponse(path=path, error=_storage_error(error)) for path, _ in files]

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        responses = []
        for path in paths:
            try:
                with self._lock:
                    relative = self._path(path)
                    data = self._read_bytes(self._filesystem(), relative)
                responses.append(FileDownloadResponse(path=path, content=data))
            except ValueError:
                responses.append(FileDownloadResponse(path=path, error="invalid_path"))
            except FilesystemError as error:
                responses.append(FileDownloadResponse(path=path, error=_storage_error(error)))
        return responses
