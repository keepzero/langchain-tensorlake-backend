# Changelog

## 2026-10-22

- Remove the external `lock` constructor parameter. Each backend retains its own
  internal lock; applications coordinate shared access outside this package.

## Unreleased — 2026-10-21

- Remove the `hidden_names` option and name-based filtering. Dotfiles and dot
  directories are ordinary paths; path validation and symlink filtering remain.

## 0.1.0 — 2026-10-20

- Extract `TensorlakeFilesystemBackend` as an independent Deep Agents integration,
  with configurable path exclusions and file/search budgets.
- Add standalone packaging, unit and real Cloud Volume tests, and usage examples.
- Use a single-package `src/langchain_tensorlake_backend` layout with root-level
  project configuration and tests.
