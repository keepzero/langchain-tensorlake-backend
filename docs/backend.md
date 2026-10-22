# Filesystem backend behavior

`TensorlakeFilesystemBackend` wraps the official Tensorlake `Filesystem` API and
Deep Agents `BackendProtocol`. It does not depend on an application, PostgreSQL,
a model provider, or a sandbox integration package.

## Construction and ownership

Pass a `Filesystem` or a synchronous resolver returning one. A resolver is called
inside an operation, including in the worker thread used by the inherited async
methods; context variables follow Python's `asyncio.to_thread` behavior. Each
instance serializes its own operations with an internal reentrant lock. Shared
locking across backend instances, sandbox commands, or workers belongs in the
application layer; the constructor does not accept an external lock.

The caller selects the volume, keeps API credentials outside agent-visible
files, and manages its lifetime. The backend never creates a volume, retains a
snapshot, starts a sandbox, or chooses a volume from a model-provided path.
Volume permissions and cross-process task/edit locks remain caller concerns.

## Paths and access policy

Paths are virtual absolute POSIX paths, rooted within the supplied volume. Root
is accepted for directory operations; file operations reject root. Paths with
`..`, `~` components, a NUL, backslashes, or a double leading slash are rejected.
Repeated internal separators and `.` segments are normalized by `PurePosixPath`.
Uploads reject aliases that normalize to the same destination before writing.

Dotfiles and dot directories are accessible like other volume paths, including
in listings and searches. Symbolic links are omitted from listings and searches.
Application-specific access policy belongs to the caller. Glob patterns retain
Deep Agents semantics: basename patterns can match files inside dot directories;
recursive `**` patterns require explicitly matching dot-directory segments.

## File operations

- Text reads use Deep Agents' pagination helper; non-positive limits perform no
  storage read and negative offsets read from the beginning.
- Supported binary previews return base64, with a 500 KiB preview budget. Raw
  downloads preserve bytes. Text operations reject invalid UTF-8 as binary.
- `write` intentionally preserves the extracted implementation's create-only
  behavior. This differs from Deep Agents' overwrite-capable `write` contract;
  a failed create returns a recoverable `file_exists` error. Use `edit` or batch
  upload for replacement. This policy is documented rather than silently changed.
- `edit` requires a unique exact match unless `replace_all=True`.
- `delete` delegates one path to the SDK; it does not expose a root wipe command.
- Uploads validate all paths and sizes before a single `write_files` publication.
  Downloads return an individual result for each requested path.
- Async operations use the protocol's thread-backed wrappers; this is not a native
  asynchronous Tensorlake client.

## Versions, limits and failures

Reads and directory/search traversals use explicit remote version IDs. An empty
volume is handled before attempting to read a missing head, which the SDK may
report as HTTP 400 rather than a missing-file response.

`max_file_bytes`, `max_search_files` and `max_search_bytes` must be positive
integers. They are client processing budgets, not Tensorlake storage quotas.
The SDK currently returns full file bytes before the backend checks read size;
these limits do not guarantee bounded network transfer or allocation for a remote
oversized file. Search errors/truncation indicate that callers should narrow scope.

Authentication and provider failures are reported without the raw provider error
message, which may contain credentials or signed URLs. Permission failures are
kept distinct from a missing file; writes never interpret an authentication error
as proof that a file can be created.

File operations produce Tensorlake working versions. The caller decides when to
retain permanent snapshots; `write`/`edit`/upload do not create task-level versions.
This package makes no claim that an SDK write, a native sandbox mount, and an
application database transaction form one atomic operation.

## References

- [Deep Agents backends](https://docs.langchain.com/oss/python/deepagents/backends)
- [Tensorlake Cloud Volume HTTP API](https://docs.tensorlake.ai/filesystems/http-api)
- [Tensorlake authentication](https://docs.tensorlake.ai/platform/authentication)
