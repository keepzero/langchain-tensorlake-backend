# Filesystem backend example

From the repository root, configure `.env` with the API key and an existing
`TENSORLAKE_FILESYSTEM`, then run:

```bash
uv run --env-file .env python samples/filesystem-backend/main.py /hello.md --create 'Hello'
uv run --env-file .env python samples/filesystem-backend/main.py /hello.md
```

The example creates a file only when `--create` is explicitly provided and does
not provision or delete a volume. Repeating `--create` for an existing path returns
`file_exists`; the read-only command can be repeated.
