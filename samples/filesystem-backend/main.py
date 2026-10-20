"""Read and optionally create a file in an explicitly selected Cloud Volume."""

import argparse
import os

from langchain_tensorlake_backend import TensorlakeFilesystemBackend
from tensorlake.filesystem import FilesystemClient


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file_path", help="Virtual absolute file path, for example /report.md")
    parser.add_argument("--create", metavar="TEXT", help="Create this file with the supplied text")
    args = parser.parse_args()
    filesystem_name = os.environ.get("TENSORLAKE_FILESYSTEM")
    if not filesystem_name:
        parser.error("Set TENSORLAKE_FILESYSTEM to an existing Cloud Volume name")
    backend = TensorlakeFilesystemBackend(FilesystemClient().get(filesystem_name))
    if args.create is not None:
        written = backend.write(args.file_path, args.create)
        if written.error:
            raise SystemExit(written.error)
    result = backend.read(args.file_path)
    if result.error:
        raise SystemExit(result.error)
    print(result.file_data["content"])


if __name__ == "__main__":
    main()
