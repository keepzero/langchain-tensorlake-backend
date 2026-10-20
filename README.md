# langchain-tensorlake-backend

`TensorlakeFilesystemBackend` connects Deep Agents file tools to an existing
Tensorlake Cloud Volume, without requiring a running sandbox.

## Install

Use the Python version in `.python-version` and install with uv:

```bash
uv sync --locked
```

To install into another existing virtual environment, use `uv pip install .`.
The package has not been published to PyPI.

## Usage

Set `TENSORLAKE_API_KEY` in the trusted application process and select an existing
Cloud Volume:

```python
from tensorlake.filesystem import FilesystemClient

from langchain_tensorlake_backend import TensorlakeFilesystemBackend

filesystem = FilesystemClient().get("agent-workspace")
backend = TensorlakeFilesystemBackend(filesystem)

result = backend.write("/report.md", "Research notes\n")
if result.error:
    raise RuntimeError(result.error)
print(backend.read("/report.md").file_data)
```

Pass the backend to `create_deep_agent(backend=backend)`. Async file operations are
also available. The application manages volume creation and task-level snapshots;
see [backend behavior](docs/backend.md) for details and limits.

Copy `.env.example` to `.env` and set the API key before running the
[example](samples/filesystem-backend/README.md). Keep credentials outside source
files and agent-visible files.

## Development

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv build
```

The default test command runs unit tests. Real-service tests create and delete
randomly named temporary volumes and require a configured `.env`:

```bash
uv run --env-file .env pytest tests/integration_tests
```

## Layout

```text
src/langchain_tensorlake_backend/   # Python package
tests/unit_tests/                   # local SDK contract tests
tests/integration_tests/            # real Cloud Volume tests
samples/filesystem-backend/         # usage example
docs/backend.md                    # API behavior and lifecycle boundaries
```

See [contributing](CONTRIBUTING.md) for development conventions.
