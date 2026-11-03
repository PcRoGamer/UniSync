import os
import shutil
import tarfile
import zipfile
from pathlib import Path


def package_unisync():
    root = Path(__file__).resolve().parent
    dist_dir = root / "dist"
    dist_dir.mkdir(exist_ok=True)

    zip_path = dist_dir / "UniSync-macOS.zip"
    tar_path = dist_dir / "UniSync-macOS.tar.gz"
    starter_zip = dist_dir / "UniSync-Starter.zip"

    include_patterns = [
        "backend",
        "userscript",
        "codex",
        "start.bat",
        "start.sh",
        "run_mcp.sh",
        "clean_disk.py",
        "clean_disk.sh",
        "requirements.txt",
        "pyproject.toml",
        "QUICKSTART.md",
        "QUICKSTART_MACOS.md",
        "README.md",
        "VERIFICATION.md",
    ]

    clean_dirs = [
        "data/courses",
        "data/cache/echo360_subtitles",
        "data/workspace/todos",
        "data/workspace/email_monitor",
        "data/libraries",
        "data/sync_state",
    ]

    # Collect files
    file_list: list[tuple[Path, str]] = []
    for item in include_patterns:
        p = root / item
        if p.is_file():
            file_list.append((p, item))
        elif p.is_dir():
            for root_dir, dirs, files in os.walk(p):
                dirs[:] = [d for d in dirs if d not in ("__pycache__", ".pytest_cache", ".venv", "data")]
                for file in files:
                    if file.endswith((".pyc", ".pyo", ".pyd")):
                        continue
                    full_p = Path(root_dir) / file
                    rel_p = full_p.relative_to(root)
                    file_list.append((full_p, str(rel_p).replace("\\", "/")))

    # 1. Package macOS tar.gz with executable permissions (0o755 for .sh)
    print(f"[*] Packaging macOS tar.gz into: {tar_path.name}")
    if tar_path.exists():
        tar_path.unlink()
    with tarfile.open(tar_path, "w:gz") as tar:
        for full_p, arcname in file_list:
            tarinfo = tar.gettarinfo(full_p, arcname=f"UniSync/{arcname}")
            if arcname.endswith(".sh"):
                tarinfo.mode = 0o755
            with open(full_p, "rb") as fh:
                tar.addfile(tarinfo, fh)
        for d in clean_dirs:
            ti = tarfile.TarInfo(name=f"UniSync/{d}/.gitkeep")
            ti.size = 0
            ti.mode = 0o644
            tar.addfile(ti)

    # 2. Package macOS zip with POSIX permission attributes (0o755 for .sh)
    print(f"[*] Packaging macOS zip into: {zip_path.name}")
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        for full_p, arcname in file_list:
            data = full_p.read_bytes()
            zinfo = zipfile.ZipInfo(f"UniSync/{arcname}")
            if arcname.endswith(".sh"):
                zinfo.external_attr = 0o755 << 16
            else:
                zinfo.external_attr = 0o644 << 16
            zipf.writestr(zinfo, data)
        for d in clean_dirs:
            zipf.writestr(f"UniSync/{d}/.gitkeep", "")

    # 3. Create generic starter zip
    if starter_zip.exists():
        starter_zip.unlink()
    shutil.copy2(zip_path, starter_zip)

    size_tar = tar_path.stat().st_size / (1024 * 1024)
    size_zip = zip_path.stat().st_size / (1024 * 1024)
    print(f"[+] macOS tar.gz created: {tar_path} ({size_tar:.2f} MB)")
    print(f"[+] macOS zip created: {zip_path} ({size_zip:.2f} MB)")
    return zip_path, tar_path


if __name__ == "__main__":
    package_unisync()
