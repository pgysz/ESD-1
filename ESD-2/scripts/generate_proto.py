#!/usr/bin/env python3
"""Generate the Python gRPC code from proto/attendance.proto.

Equivalent to running (from the project root):

    python -m grpc_tools.protoc -I proto --python_out=grpc_service \
        --grpc_python_out=grpc_service proto/attendance.proto

plus one fix-up: protoc emits `import attendance_pb2 ...` (a top-level import)
but our generated files live inside the grpc_service package, so the import
is rewritten to a package-relative one.
"""
import re
import sys
from pathlib import Path

from grpc_tools import protoc

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "grpc_service"


def main():
    code = protoc.main([
        "grpc_tools.protoc", f"-I{ROOT / 'proto'}",
        f"--python_out={OUT}", f"--grpc_python_out={OUT}",
        str(ROOT / "proto" / "attendance.proto"),
    ])
    if code != 0:
        sys.exit(code)
    grpc_file = OUT / "attendance_pb2_grpc.py"
    text = grpc_file.read_text()
    text = re.sub(r"^import attendance_pb2 as", "from . import attendance_pb2 as", text, flags=re.M)
    grpc_file.write_text(text)
    print("Generated grpc_service/attendance_pb2.py and attendance_pb2_grpc.py")


if __name__ == "__main__":
    main()
