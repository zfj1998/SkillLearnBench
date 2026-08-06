#!/usr/bin/env python3
"""Build immutable Harbor task packages for phase-1 AP evaluation."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "tasks"
HUMAN_SKILLS = ROOT / "skills" / "human_authored"
DEFAULT_OUTPUT = ROOT / "ap" / "artifacts" / "packages"
CONDITIONS = ("no_skill", "human_authored")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_revision() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()


def instances() -> list[tuple[str, Path]]:
    found: list[tuple[str, Path]] = []
    for family_dir in sorted(TASKS.iterdir()):
        if not family_dir.is_dir():
            continue
        for instance_dir in sorted(family_dir.iterdir()):
            required = (
                instance_dir / "instruction.md",
                instance_dir / "task.toml",
                instance_dir / "environment" / "Dockerfile",
                instance_dir / "tests" / "test.sh",
            )
            if all(path.exists() for path in required):
                found.append((family_dir.name, instance_dir))
    return found


def normalized_tar(source: Path, archive: Path) -> None:
    """Create a deterministic gzip tar containing source's direct children."""
    archive.parent.mkdir(parents=True, exist_ok=True)
    with archive.open("wb") as raw:
        import gzip

        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
                for path in sorted(source.rglob("*"), key=lambda p: p.as_posix()):
                    relative = path.relative_to(source)
                    info = tar.gettarinfo(str(path), arcname=relative.as_posix())
                    info.uid = info.gid = 0
                    info.uname = info.gname = "root"
                    info.mtime = 0
                    if path.is_file():
                        with path.open("rb") as stream:
                            tar.addfile(info, stream)
                    else:
                        tar.addfile(info)


def package_instance(
    family: str, instance_dir: Path, condition: str, output: Path
) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="skilllearnbench-ap-") as tmp:
        staged = Path(tmp) / instance_dir.name
        shutil.copytree(instance_dir, staged)
        staged_skills = staged / "environment" / "skills"
        if staged_skills.exists():
            shutil.rmtree(staged_skills)
        staged_skills.mkdir(parents=True)

        skill_files: list[dict[str, str]] = []
        if condition == "human_authored":
            source_skills = HUMAN_SKILLS / family
            if not source_skills.is_dir():
                raise RuntimeError(f"missing human-authored skills for {family}")
            for child in sorted(source_skills.iterdir()):
                destination = staged_skills / child.name
                if child.is_dir():
                    shutil.copytree(child, destination)
                else:
                    shutil.copy2(child, destination)
            for skill in sorted(staged_skills.rglob("SKILL.md")):
                skill_files.append(
                    {
                        "path": skill.relative_to(staged_skills).as_posix(),
                        "sha256": sha256(skill),
                    }
                )
            if not skill_files:
                raise RuntimeError(f"no SKILL.md packaged for {family}")

        dockerfile = staged / "environment" / "Dockerfile"
        if "COPY skills" not in dockerfile.read_text(encoding="utf-8"):
            raise RuntimeError(f"Dockerfile does not deploy skills: {instance_dir}")

        archive = output / f"{condition}-assets" / instance_dir.name / "content.tgz"
        normalized_tar(staged, archive)
        return {
            "condition": condition,
            "task_family": family,
            "instance_id": instance_dir.name,
            "archive": archive.relative_to(output).as_posix(),
            "archive_bytes": archive.stat().st_size,
            "archive_sha256": sha256(archive),
            "skill_files": skill_files,
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        if not args.replace:
            raise SystemExit(f"output already exists: {output}; pass --replace")
        shutil.rmtree(output)
    output.mkdir(parents=True)

    discovered = instances()
    families = sorted({family for family, _ in discovered})
    if len(discovered) != 100 or len(families) != 20:
        raise SystemExit(
            f"expected 100 instances across 20 families, got "
            f"{len(discovered)} across {len(families)}"
        )
    if any(not (HUMAN_SKILLS / family).is_dir() for family in families):
        raise SystemExit("human-authored skill coverage is incomplete")

    records = [
        package_instance(family, instance, condition, output)
        for condition in CONDITIONS
        for family, instance in discovered
    ]
    manifest = {
        "schema_version": "1.0",
        "source_repository": "https://github.com/cxcscmu/SkillLearnBench",
        "source_revision": source_revision(),
        "conditions": list(CONDITIONS),
        "task_family_count": len(families),
        "instance_count_per_condition": len(discovered),
        "packages": records,
    }
    manifest_path = output / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (output / "SHA256SUMS").write_text(
        "".join(
            f"{record['archive_sha256']}  {record['archive']}\n" for record in records
        )
        + f"{sha256(manifest_path)}  manifest.json\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(output),
                "source_revision": manifest["source_revision"],
                "families": len(families),
                "instances_per_condition": len(discovered),
                "packages": len(records),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
