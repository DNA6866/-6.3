from __future__ import annotations

import argparse
import os
import zipfile
from pathlib import Path


def create_zip(source: Path, output: Path) -> None:
    source = source.resolve()
    output = output.resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"待压缩目录不存在: {source}")

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f"{output.name}.tmp")
    temporary.unlink(missing_ok=True)

    try:
        with zipfile.ZipFile(
            temporary,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
            allowZip64=True,
        ) as archive:
            for path in sorted(source.rglob("*")):
                if not path.is_file():
                    continue
                # zipfile 会为中文路径自动写入 UTF-8 标志，避免受系统区域设置影响。
                archive.write(path, path.relative_to(source).as_posix())
        os.replace(temporary, output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="生成离线更新 ZIP 包")
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    create_zip(args.source, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
