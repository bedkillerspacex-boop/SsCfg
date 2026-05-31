#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from queue import Empty, Queue
from tkinter import (
    BOTH,
    END,
    LEFT,
    RIGHT,
    VERTICAL,
    W,
    BooleanVar,
    DoubleVar,
    PanedWindow,
    StringVar,
    Tk,
    filedialog,
    messagebox,
    simpledialog,
    ttk,
)
from tkinter.scrolledtext import ScrolledText
from urllib.parse import quote


SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE_REPO_DIR = SCRIPT_DIR.parent
DEFAULT_REPO_CACHE_ROOT = SCRIPT_DIR / "repo_cache"
DEFAULT_OWNER_REPO = "bedkillerspacex-boop/SsCfg"
DEFAULT_BRANCH = "master"
DEFAULT_COMMIT_MESSAGE = "update Southside publisher assets(windows)"
PACK_TYPE_SAFE = "安全"
PACK_TYPE_VIOLENT = "暴力"
PACK_TYPE_OPTIONS = (PACK_TYPE_SAFE, PACK_TYPE_VIOLENT)
PUSH_PROGRESS_RE = re.compile(
    r"(?P<stage>Enumerating objects|Counting objects|Compressing objects|Writing objects):\s*"
    r"(?P<percent>\d+)%\s*\((?P<done>\d+)/(?P<total>\d+)\)"
    r"(?:,\s*(?P<size>[\d.]+)\s*(?P<size_unit>[KMGT]?i?B|[KMGT]?B))?",
    re.IGNORECASE,
)


class PublishError(RuntimeError):
    pass


@dataclass
class SourcePack:
    path: Path
    rel_path: str
    size_bytes: int
    sha256: str

    @property
    def file_name(self) -> str:
        return self.path.name

    @property
    def stem(self) -> str:
        return self.path.stem


@dataclass
class PackMeta:
    pack_id: int
    name: str
    author: str
    summary: str
    pack_type: str
    version: int
    date: str
    southside_version: str
    source_file: str
    created_at: str


@dataclass
class PublisherRegistry:
    schema_version: int
    max_pack_id: int
    bindings: dict[str, int]


@dataclass
class PublishedPack:
    meta: PackMeta
    source: SourcePack | None
    status: str
    warnings: list[str]


@dataclass
class ScanState:
    repo_dir: Path
    registry: PublisherRegistry
    published: list[PublishedPack]
    unpublished: list[SourcePack]
    warnings: list[str]


@dataclass
class BuildResult:
    index_data: dict
    warnings: list[str]
    published_count: int
    missing_count: int
    unpublished_count: int


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def clean_string(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def as_int(value: object, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def normalize_pack_type(value: object) -> str:
    text = clean_string(value)
    if not text:
        return PACK_TYPE_SAFE
    if text not in PACK_TYPE_OPTIONS:
        raise PublishError(f"类型非法: {value!r}，只能是 {PACK_TYPE_SAFE} 或 {PACK_TYPE_VIOLENT}")
    return text


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise PublishError(f"解析 JSON 失败 {path.name}: {exc}") from exc


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def published_source_bytes(content: bytes) -> bytes:
    return content.replace(b"\r\n", b"\n")


def normalize_owner_repo(value: str) -> str:
    text = clean_string(value)
    if not text:
        return DEFAULT_OWNER_REPO
    if text.startswith("https://github.com/"):
        text = text[len("https://github.com/") :]
    elif text.startswith("http://github.com/"):
        text = text[len("http://github.com/") :]
    if text.endswith(".git"):
        text = text[:-4]
    return text.strip("/\\") or DEFAULT_OWNER_REPO


def cache_dir_for_repo(owner_repo: str) -> Path:
    normalized = normalize_owner_repo(owner_repo)
    safe_name = normalized.replace("/", "__").replace("\\", "__").replace(":", "_")
    return DEFAULT_REPO_CACHE_ROOT / safe_name


def default_remote_url(owner_repo: str) -> str:
    return f"https://github.com/{normalize_owner_repo(owner_repo)}.git"


def target_display(owner_repo: str, branch: str, repo_dir: Path) -> str:
    return f"{normalize_owner_repo(owner_repo)}@{clean_string(branch) or DEFAULT_BRANCH} -> {repo_dir}"


def preferred_repo_dir(owner_repo: str) -> Path:
    normalized = normalize_owner_repo(owner_repo)
    workspace_git_dir = WORKSPACE_REPO_DIR / ".git"
    if normalized == normalize_owner_repo(DEFAULT_OWNER_REPO) and workspace_git_dir.exists():
        return WORKSPACE_REPO_DIR
    return cache_dir_for_repo(normalized)


def ensure_repo_dir(path_text: str) -> Path:
    repo_dir = Path(path_text).expanduser()
    if not repo_dir.exists():
        raise PublishError(f"仓库目录不存在: {repo_dir}")
    if not (repo_dir / ".git").exists():
        raise PublishError(f"这不是一个 git 仓库: {repo_dir}")
    return repo_dir.resolve()


def size_to_bytes(value_text: str, unit_text: str) -> float:
    value = float(value_text)
    unit = unit_text.upper()
    factors = {
        "B": 1,
        "KB": 1000,
        "MB": 1000**2,
        "GB": 1000**3,
        "TB": 1000**4,
        "KIB": 1024,
        "MIB": 1024**2,
        "GIB": 1024**3,
        "TIB": 1024**4,
    }
    return value * factors.get(unit, 1)


def format_size_text(size_bytes: float | None) -> str:
    if size_bytes is None:
        return ""
    if size_bytes >= 1024**3:
        return f"{size_bytes / (1024**3):.2f} GB"
    if size_bytes >= 1024**2:
        return f"{size_bytes / (1024**2):.2f} MB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes:.0f} B"


def parse_push_progress(text: str) -> tuple[float | None, str] | None:
    match = PUSH_PROGRESS_RE.search(text)
    if not match:
        return None
    stage = match.group("stage")
    percent = float(match.group("percent"))
    done = int(match.group("done"))
    total = int(match.group("total"))
    status = f"{stage} {int(percent)}% ({done}/{total})"
    size_text = match.group("size")
    size_unit = match.group("size_unit")
    if size_text and size_unit:
        transferred = size_to_bytes(size_text, size_unit)
        estimated_total = transferred if percent <= 0 else transferred / max(percent / 100.0, 0.01)
        status = f"{stage} {int(percent)}% {format_size_text(transferred)}/{format_size_text(estimated_total)}"
    return percent, status


def run_git(repo_dir: Path, *args: str) -> str:
    process = subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if process.returncode != 0:
        message = process.stderr.strip() or process.stdout.strip() or "unknown git error"
        raise PublishError(f"git {' '.join(args)} failed: {message}")
    return process.stdout


def git_status(repo_dir: Path) -> str:
    return run_git(repo_dir, "status", "--short").strip()


def infer_owner_repo(repo_dir: Path) -> str:
    try:
        output = run_git(repo_dir, "config", "--get", "remote.origin.url").strip()
    except PublishError:
        return DEFAULT_OWNER_REPO
    return normalize_owner_repo(output)


def default_owner_repo() -> str:
    if (WORKSPACE_REPO_DIR / ".git").exists():
        return infer_owner_repo(WORKSPACE_REPO_DIR)
    return DEFAULT_OWNER_REPO


def infer_branch(repo_dir: Path) -> str:
    try:
        branch = run_git(repo_dir, "branch", "--show-current").strip()
    except PublishError:
        return DEFAULT_BRANCH
    return branch or DEFAULT_BRANCH


def infer_github_login_status() -> str:
    try:
        process = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "未检测到 gh"

    output = ((process.stdout or "") + "\n" + (process.stderr or "")).strip()
    if process.returncode != 0:
        lowered = output.lower()
        if "not logged" in lowered or "not logged into any hosts" in lowered:
            return "GitHub 未登录"
        return "gh 登录状态未知"

    for line in output.splitlines():
        stripped = line.strip()
        if "Logged in to github.com account" in stripped:
            return f"github.com: {stripped.split('account', 1)[-1].strip()}"
        if stripped.startswith("account "):
            return f"github.com: {stripped[len('account '):].strip()}"
    return "GitHub 已登录"


def sync_cached_repo(repo_dir: Path, owner_repo: str, branch: str, progress_callback=None, output_callback=None) -> str:
    repo_dir = repo_dir.resolve() if repo_dir.exists() else repo_dir
    if repo_dir == WORKSPACE_REPO_DIR.resolve():
        if progress_callback:
            progress_callback(15.0, f"检查当前仓库 {owner_repo}")
        actual_owner = infer_owner_repo(repo_dir)
        actual_branch = infer_branch(repo_dir)
        if progress_callback:
            progress_callback(100.0, f"使用当前仓库 {actual_owner}@{actual_branch}")
        return f"使用当前仓库 {repo_dir}"

    remote_url = default_remote_url(owner_repo)
    repo_dir.parent.mkdir(parents=True, exist_ok=True)
    if not repo_dir.exists():
        if progress_callback:
            progress_callback(10.0, f"正在克隆 {owner_repo}")
        process = subprocess.run(
            ["git", "clone", "--branch", branch, remote_url, str(repo_dir)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if process.returncode != 0:
            message = process.stderr.strip() or process.stdout.strip() or "未知 clone 错误"
            raise PublishError(f"克隆缓存仓库失败: {message}")
        if output_callback:
            output_callback(process.stdout.strip() or f"已克隆缓存仓库到 {repo_dir}")
        if progress_callback:
            progress_callback(100.0, f"已克隆 {owner_repo}")
        return f"已克隆缓存仓库到 {repo_dir}"

    if not (repo_dir / ".git").exists():
        raise PublishError(f"缓存目录存在但不是 git 仓库: {repo_dir}")

    local_changes = git_status(repo_dir)
    if progress_callback:
        progress_callback(20.0, f"正在获取 {owner_repo}")
    run_git(repo_dir, "remote", "set-url", "origin", remote_url)
    run_git(repo_dir, "fetch", "origin", branch)

    current_branch = infer_branch(repo_dir)
    if current_branch != branch and not local_changes:
        if progress_callback:
            progress_callback(45.0, f"正在切换到 {branch}")
        run_git(repo_dir, "checkout", branch)

    if local_changes:
        if progress_callback:
            progress_callback(100.0, "检测到本地改动，跳过自动拉取")
        return f"缓存仓库有本地改动，已跳过自动拉取: {repo_dir}"

    if progress_callback:
        progress_callback(70.0, f"正在拉取 {branch}")
    run_git(repo_dir, "pull", "--ff-only", "origin", branch)
    if progress_callback:
        progress_callback(100.0, f"已同步 {owner_repo}")
    return f"已更新缓存仓库 {repo_dir}"


def run_git_push_with_progress(repo_dir: Path, branch: str, progress_callback=None, output_callback=None) -> str:
    process = subprocess.Popen(
        ["git", "-C", str(repo_dir), "push", "--progress", "origin", branch],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    if process.stdout is None:
        raise PublishError("无法读取 git push 输出")

    chunks: list[str] = []
    current: list[str] = []
    while True:
        char = process.stdout.read(1)
        if not char:
            break
        if char in ("\r", "\n"):
            if current:
                line = "".join(current).strip()
                current.clear()
                if line:
                    chunks.append(line)
                    if output_callback:
                        output_callback(line)
                    if progress_callback:
                        progress = parse_push_progress(line)
                        if progress:
                            progress_callback(*progress)
            continue
        current.append(char)

    if current:
        line = "".join(current).strip()
        if line:
            chunks.append(line)
            if output_callback:
                output_callback(line)
            if progress_callback:
                progress = parse_push_progress(line)
                if progress:
                    progress_callback(*progress)

    output_text = "\n".join(chunks).strip()
    if process.wait() != 0:
        raise PublishError(output_text or "git push 失败")
    return output_text


def normalize_source_file(value: str) -> str:
    text = clean_string(value).replace("\\", "/")
    if not text:
        raise PublishError("sourceFile 不能为空")
    try:
        path = PurePosixPath(text)
    except Exception as exc:
        raise PublishError(f"sourceFile 非法: {value!r}") from exc
    if path.is_absolute():
        raise PublishError(f"sourceFile 必须是仓库相对路径: {value}")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise PublishError(f"sourceFile 非法: {value!r}")
    normalized = path.as_posix()
    if not normalized.startswith("packs/"):
        raise PublishError(f"sourceFile 必须位于 packs/ 目录下: {value}")
    if not normalized.lower().endswith(".json"):
        raise PublishError(f"sourceFile 必须指向 .json 文件: {value}")
    return normalized


def source_file_from_path(repo_dir: Path, selected_path: Path) -> str:
    repo_root = repo_dir.resolve()
    file_path = selected_path.expanduser().resolve()
    try:
        relative = file_path.relative_to(repo_root)
    except ValueError as exc:
        raise PublishError(f"所选文件不在当前仓库内: {selected_path}") from exc
    return normalize_source_file(relative.as_posix())


def source_path_for_file(repo_dir: Path, source_file: str) -> Path:
    normalized = normalize_source_file(source_file)
    return repo_dir.joinpath(*normalized.split("/"))


def source_pack_from_file(repo_dir: Path, source_file: str) -> SourcePack:
    path = source_path_for_file(repo_dir, source_file)
    if not path.exists():
        raise PublishError(f"源文件不存在: {source_file}")
    content = published_source_bytes(path.read_bytes())
    return SourcePack(
        path=path,
        rel_path=normalize_source_file(source_file),
        size_bytes=len(content),
        sha256=sha256_bytes(content),
    )


def normalize_source_editor_text(text: str) -> str:
    return text.replace("\r\n", "\n").rstrip("\n")


def read_source_text(repo_dir: Path, source_file: str) -> str:
    path = source_path_for_file(repo_dir, source_file)
    if not path.exists():
        return ""
    return normalize_source_editor_text(path.read_text(encoding="utf-8-sig"))


def validate_source_json_text(text: str, source_file: str) -> None:
    normalized = normalize_source_editor_text(text)
    if not normalized.strip():
        raise PublishError(f"Source JSON cannot be empty: {source_file}")
    try:
        json.loads(normalized)
    except json.JSONDecodeError as exc:
        raise PublishError(f"Source JSON is invalid for {source_file}: {exc}") from exc


def write_source_text(repo_dir: Path, source_file: str, text: str) -> Path:
    path = source_path_for_file(repo_dir, source_file)
    normalized = normalize_source_editor_text(text)
    validate_source_json_text(normalized, source_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(normalized + "\n", encoding="utf-8")
    return path


def registry_path(repo_dir: Path) -> Path:
    return repo_dir / "publisher_meta" / "registry.json"


def sidecar_dir(repo_dir: Path) -> Path:
    return repo_dir / "publisher_meta" / "packs"


def sidecar_path(repo_dir: Path, pack_id: int) -> Path:
    return sidecar_dir(repo_dir) / f"{pack_id}.json"


def raw_download_url(owner_repo: str, branch: str, source_file: str) -> str:
    return (
        f"https://raw.githubusercontent.com/{normalize_owner_repo(owner_repo)}/refs/heads/"
        f"{quote(clean_string(branch) or DEFAULT_BRANCH, safe='')}/{quote(source_file, safe='/')}"
    )


def collect_source_packs(repo_dir: Path) -> list[SourcePack]:
    packs_dir = repo_dir / "packs"
    if not packs_dir.is_dir():
        raise PublishError(f"缺少 packs 目录: {packs_dir}")
    results: list[SourcePack] = []
    for path in sorted(packs_dir.glob("*.json"), key=lambda item: item.name.lower()):
        content = published_source_bytes(path.read_bytes())
        results.append(
            SourcePack(
                path=path,
                rel_path=f"packs/{path.name}",
                size_bytes=len(content),
                sha256=sha256_bytes(content),
            )
        )
    return results


def load_registry(repo_dir: Path) -> PublisherRegistry:
    path = registry_path(repo_dir)
    if not path.exists():
        return PublisherRegistry(schema_version=1, max_pack_id=0, bindings={})
    data = read_json(path)
    bindings_data = data.get("bindings")
    bindings: dict[str, int] = {}
    if isinstance(bindings_data, dict):
        for source_file, pack_id in bindings_data.items():
            normalized = normalize_source_file(source_file)
            parsed_id = as_int(pack_id, 0)
            if parsed_id <= 0:
                raise PublishError(f"registry 中 {source_file} 的 id 非法: {pack_id}")
            if normalized in bindings:
                raise PublishError(f"registry 中存在重复绑定: {normalized}")
            bindings[normalized] = parsed_id
    max_pack_id = as_int(data.get("maxPackId"), 0)
    if max_pack_id < 0:
        raise PublishError("maxPackId 不能为负数")
    if bindings:
        max_pack_id = max(max_pack_id, max(bindings.values()))
    return PublisherRegistry(schema_version=max(as_int(data.get("schemaVersion"), 1), 1), max_pack_id=max_pack_id, bindings=bindings)


def registry_payload(registry: PublisherRegistry) -> dict:
    return {
        "schemaVersion": max(registry.schema_version, 1),
        "maxPackId": max(registry.max_pack_id, 0),
        "bindings": {source_file: registry.bindings[source_file] for source_file in sorted(registry.bindings)},
    }


def write_registry(repo_dir: Path, registry: PublisherRegistry) -> None:
    write_json(registry_path(repo_dir), registry_payload(registry))


def load_sidecars(repo_dir: Path) -> dict[int, PackMeta]:
    results: dict[int, PackMeta] = {}
    base_dir = sidecar_dir(repo_dir)
    if not base_dir.exists():
        return results
    for path in sorted(base_dir.glob("*.json"), key=lambda item: item.name.lower()):
        data = read_json(path)
        pack_id = as_int(data.get("id"), 0)
        if pack_id <= 0:
            pack_id = as_int(path.stem, 0)
        if pack_id <= 0:
            raise PublishError(f"sidecar 文件名非法: {path.name}")
        if pack_id in results:
            raise PublishError(f"sidecar 元数据重复: id {pack_id}")
        results[pack_id] = PackMeta(
            pack_id=pack_id,
            name=clean_string(data.get("name")) or f"Pack {pack_id}",
            author=clean_string(data.get("author")),
            summary=clean_string(data.get("summary")),
            pack_type=normalize_pack_type(data.get("type")),
            version=max(as_int(data.get("version"), 1), 1),
            date=clean_string(data.get("date")) or utc_now(),
            southside_version=clean_string(data.get("southsideVersion")),
            source_file=normalize_source_file(clean_string(data.get("sourceFile")) or f"packs/{path.stem}.json"),
            created_at=clean_string(data.get("createdAt")) or utc_now(),
        )
    return results


def sidecar_payload(meta: PackMeta) -> dict:
    return {
        "schemaVersion": 1,
        "id": meta.pack_id,
        "name": clean_string(meta.name) or f"Pack {meta.pack_id}",
        "author": clean_string(meta.author),
        "summary": clean_string(meta.summary),
        "type": normalize_pack_type(meta.pack_type),
        "version": max(meta.version, 1),
        "date": clean_string(meta.date) or utc_now(),
        "southsideVersion": clean_string(meta.southside_version),
        "sourceFile": normalize_source_file(meta.source_file),
        "createdAt": clean_string(meta.created_at) or utc_now(),
    }


def write_pack_meta(repo_dir: Path, meta: PackMeta) -> None:
    payload = sidecar_payload(meta)
    write_json(sidecar_path(repo_dir, meta.pack_id), payload)


def allocate_pack_id(registry: PublisherRegistry) -> int:
    registry.max_pack_id += 1
    return registry.max_pack_id


def reverse_bindings(bindings: dict[str, int]) -> dict[int, str]:
    results: dict[int, str] = {}
    for source_file, pack_id in bindings.items():
        if pack_id in results:
            raise PublishError(f"registry 中 id {pack_id} 绑定了多个源文件")
        results[pack_id] = source_file
    return results


def default_meta_for_source(pack_id: int, source_file: str) -> PackMeta:
    stem = Path(source_file).stem
    now = utc_now()
    return PackMeta(
        pack_id=pack_id,
        name=stem,
        author="",
        summary=stem,
        pack_type=PACK_TYPE_SAFE,
        version=1,
        date=now,
        southside_version="",
        source_file=source_file,
        created_at=now,
    )


def scan_repository_state(repo_dir: Path) -> ScanState:
    sources = collect_source_packs(repo_dir)
    source_by_rel = {source.rel_path: source for source in sources}
    registry = load_registry(repo_dir)
    sidecars = load_sidecars(repo_dir)
    reverse = reverse_bindings(registry.bindings)
    warnings: list[str] = []
    published: list[PublishedPack] = []
    bound_sources = set(registry.bindings.keys())

    all_ids = sorted(set(sidecars) | set(reverse))
    for pack_id in all_ids:
        record_warnings: list[str] = []
        meta = sidecars.get(pack_id)
        source_file_from_registry = reverse.get(pack_id)
        if meta is None and source_file_from_registry is None:
            continue
        if meta is None:
            assert source_file_from_registry is not None
            meta = default_meta_for_source(pack_id, source_file_from_registry)
            record_warnings.append(f"id {pack_id}: 缺少 sidecar 元数据，已使用默认值")
        if source_file_from_registry and meta.source_file != source_file_from_registry:
            record_warnings.append(
                f"id {pack_id}: sidecar 的 sourceFile {meta.source_file} 与 registry 绑定 {source_file_from_registry} 不一致"
            )
            meta = replace(meta, source_file=source_file_from_registry)
        source = source_by_rel.get(meta.source_file)
        status = "已发布" if source is not None else "源文件缺失"
        if source is None:
            record_warnings.append(f"id {pack_id}: 已绑定的源文件不存在: {meta.source_file}")
        published.append(PublishedPack(meta=meta, source=source, status=status, warnings=record_warnings))
        warnings.extend(record_warnings)

    unpublished = [source for source in sources if source.rel_path not in bound_sources]
    return ScanState(repo_dir=repo_dir, registry=registry, published=published, unpublished=unpublished, warnings=warnings)


def build_index_data(owner_repo: str, branch: str, state: ScanState) -> BuildResult:
    generated_at = utc_now()
    packs: list[dict] = []
    warnings = list(state.warnings)
    missing_count = 0
    for record in sorted(state.published, key=lambda item: item.meta.pack_id):
        if record.source is None:
            missing_count += 1
            warnings.append(f"已跳过 id {record.meta.pack_id}: 源文件不存在")
            continue
        meta = sidecar_payload(record.meta)
        packs.append(
            {
                "id": meta["id"],
                "name": meta["name"],
                "author": meta["author"],
                "summary": meta["summary"],
                "type": meta["type"],
                "version": meta["version"],
                "date": meta["date"],
                "southsideVersion": meta["southsideVersion"],
                "sha256": record.source.sha256,
                "downloadUrl": raw_download_url(owner_repo, branch, record.source.rel_path),
                "fileName": record.source.file_name,
                "sizeBytes": record.source.size_bytes,
            }
        )
    return BuildResult(
        index_data={
            "schemaVersion": 1,
            "generatedAt": generated_at,
            "maxPackId": state.registry.max_pack_id,
            "packs": packs,
        },
        warnings=warnings,
        published_count=len(packs),
        missing_count=missing_count,
        unpublished_count=len(state.unpublished),
    )


def write_index_file(repo_dir: Path, result: BuildResult) -> Path:
    path = repo_dir / "index.json"
    write_json(path, result.index_data)
    return path


def publish_source_file(repo_dir: Path, source_file: str, meta_template: PackMeta | None = None) -> PackMeta:
    source_file = normalize_source_file(source_file)
    state = scan_repository_state(repo_dir)
    if source_file not in {source.rel_path for source in state.unpublished}:
        raise PublishError(f"源文件已发布或不存在: {source_file}")
    if source_file in state.registry.bindings:
        raise PublishError(f"源文件已经绑定了 id: {source_file}")
    new_id = allocate_pack_id(state.registry)
    meta = default_meta_for_source(new_id, source_file) if meta_template is None else replace(meta_template, pack_id=new_id, source_file=source_file)
    state.registry.bindings[source_file] = new_id
    write_registry(repo_dir, state.registry)
    write_pack_meta(repo_dir, meta)
    return meta


def create_source_file(repo_dir: Path, file_name: str, initial_text: str = "{}") -> SourcePack:
    file_name = clean_string(file_name)
    if not file_name:
        raise PublishError("文件名不能为空")
    if "/" in file_name or "\\" in file_name:
        raise PublishError("新建 JSON 只能填写文件名，不能包含路径")
    if not file_name.lower().endswith(".json"):
        file_name = f"{file_name}.json"
    source_file = normalize_source_file(f"packs/{file_name}")
    path = source_path_for_file(repo_dir, source_file)
    if path.exists():
        raise PublishError(f"文件已存在: {source_file}")
    write_source_text(repo_dir, source_file, initial_text)
    return source_pack_from_file(repo_dir, source_file)


def delete_source_file(repo_dir: Path, source_file: str) -> None:
    path = source_path_for_file(repo_dir, source_file)
    if not path.exists():
        raise PublishError(f"源文件不存在: {source_file}")
    path.unlink()


def unpublish_pack(repo_dir: Path, pack_id: int, delete_source: bool = True) -> None:
    state = scan_repository_state(repo_dir)
    if pack_id <= 0:
        raise PublishError("pack id 必须大于 0")
    record = next((item for item in state.published if item.meta.pack_id == pack_id), None)
    if record is None:
        raise PublishError(f"未知的 pack id: {pack_id}")
    source_file = record.meta.source_file
    source_path = source_path_for_file(repo_dir, source_file)
    if delete_source and source_path.exists():
        source_path.unlink()
    state.registry.bindings = {
        bound_source: bound_id for bound_source, bound_id in state.registry.bindings.items() if bound_id != pack_id
    }
    sidecar = sidecar_path(repo_dir, pack_id)
    if sidecar.exists():
        sidecar.unlink()
    write_registry(repo_dir, state.registry)


def rebind_pack_source(repo_dir: Path, pack_id: int, new_source_file: str) -> PackMeta:
    new_source_file = normalize_source_file(new_source_file)
    state = scan_repository_state(repo_dir)
    if pack_id <= 0:
        raise PublishError("pack id 必须大于 0")
    published_by_id = {record.meta.pack_id: record for record in state.published}
    record = published_by_id.get(pack_id)
    if record is None:
        raise PublishError(f"未知的 pack id: {pack_id}")
    if new_source_file not in {source.rel_path for source in state.unpublished} and new_source_file != record.meta.source_file:
        raise PublishError(f"这个源文件不能用于重新绑定: {new_source_file}")
    old_sources = [source_file for source_file, bound_id in list(state.registry.bindings.items()) if bound_id == pack_id]
    for source_file in old_sources:
        del state.registry.bindings[source_file]
    state.registry.bindings[new_source_file] = pack_id
    new_meta = replace(record.meta, source_file=new_source_file)
    write_registry(repo_dir, state.registry)
    write_pack_meta(repo_dir, new_meta)
    return new_meta


def save_metadata(repo_dir: Path, meta: PackMeta) -> None:
    state = scan_repository_state(repo_dir)
    if meta.pack_id not in {record.meta.pack_id for record in state.published}:
        raise PublishError(f"未知的 pack id: {meta.pack_id}")
    write_pack_meta(repo_dir, meta)


def build_summary(result: BuildResult) -> str:
    return (
        f"已发布 {result.published_count} | 源文件缺失 {result.missing_count} | "
        f"未发布文件 {result.unpublished_count} | 最大 ID {result.index_data['maxPackId']}"
    )


def preview_lines(result: BuildResult) -> list[str]:
    lines = [build_summary(result)]
    for pack in result.index_data["packs"][:10]:
        lines.append(
            f"- id={pack['id']} | {pack['name']} | 版本={pack['version']} | "
            f"Southside 版本={pack['southsideVersion'] or '-'} | {pack['fileName']}"
        )
    remaining = len(result.index_data["packs"]) - min(10, len(result.index_data["packs"]))
    if remaining > 0:
        lines.append(f"... 其余 {remaining} 个包未展开")
    for warning in result.warnings:
        lines.append(f"[warn] {warning}")
    return lines


def parse_version_text(value: str) -> int:
    version = as_int(value, 0)
    if version <= 0:
        raise PublishError("版本号必须是大于 0 的整数")
    return version


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Southside 只读包发布工具")
    parser.add_argument("--repo", default="")
    parser.add_argument("--owner-repo", default="")
    parser.add_argument("--branch", default="")
    parser.add_argument("--message", default=DEFAULT_COMMIT_MESSAGE)
    parser.add_argument("--write-index", action="store_true")
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--push", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--gui", action="store_true")
    return parser.parse_args(argv)


def resolve_cli_repo(args: argparse.Namespace) -> tuple[Path, str, str]:
    owner_repo = normalize_owner_repo(args.owner_repo or default_owner_repo())
    branch = clean_string(args.branch) or DEFAULT_BRANCH
    repo_dir = Path(args.repo).expanduser() if args.repo else cache_dir_for_repo(owner_repo)
    sync_cached_repo(repo_dir, owner_repo, branch)
    repo_dir = ensure_repo_dir(str(repo_dir))
    return repo_dir, owner_repo, branch


def run_cli(args: argparse.Namespace) -> int:
    repo_dir, owner_repo, branch = resolve_cli_repo(args)
    state = scan_repository_state(repo_dir)
    result = build_index_data(owner_repo, branch, state)
    for line in preview_lines(result):
        print(line)
    if args.dry_run and not args.write_index and not args.commit and not args.push:
        return 0
    if args.write_index or args.commit or args.push:
        print(f"已写入 {write_index_file(repo_dir, result)}")
    if args.commit or args.push:
        run_git(repo_dir, "add", "index.json", "publisher_meta", "packs")
        if git_status(repo_dir):
            print(run_git(repo_dir, "commit", "-m", args.message or DEFAULT_COMMIT_MESSAGE).strip())
            if args.push:
                print(run_git_push_with_progress(repo_dir, infer_branch(repo_dir)))
        else:
            print("没有可提交的改动")
    return 0


class PublisherApp(Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Southside 发布器")
        self.geometry("1320x860")
        self.minsize(1120, 760)

        initial_owner_repo = default_owner_repo()
        initial_repo_dir = preferred_repo_dir(initial_owner_repo)
        initial_branch = infer_branch(initial_repo_dir) if (initial_repo_dir / ".git").exists() else DEFAULT_BRANCH
        self.owner_repo_var = StringVar(value=initial_owner_repo)
        self.branch_var = StringVar(value=initial_branch)
        self.repo_var = StringVar(value=str(initial_repo_dir))
        self.target_var = StringVar(value="")
        self.message_var = StringVar(value=DEFAULT_COMMIT_MESSAGE)
        self.github_status_var = StringVar(value="检测中")
        self.summary_var = StringVar(value="尚未扫描")
        self.status_var = StringVar(value="空闲")
        self.progress_var = DoubleVar(value=0.0)
        self.auto_refresh_date_var = BooleanVar(value=False)

        self.record_status_var = StringVar(value="")
        self.record_id_var = StringVar(value="")
        self.record_source_file_var = StringVar(value="")
        self.record_name_var = StringVar(value="")
        self.record_author_var = StringVar(value="")
        self.record_summary_var = StringVar(value="")
        self.record_type_var = StringVar(value=PACK_TYPE_SAFE)
        self.record_version_var = StringVar(value="1")
        self.record_date_var = StringVar(value="")
        self.record_southside_version_var = StringVar(value="")
        self.rebind_source_var = StringVar(value="")

        self.scan_state: ScanState | None = None
        self.current_item_key: str | None = None
        self.current_kind: str | None = None
        self.loaded_meta_snapshot: tuple[str, ...] | None = None
        self.loaded_source_snapshot: str | None = None
        self.metadata_dirty = False
        self.source_dirty = False
        self.form_dirty = False
        self.suspend_dirty_tracking = False
        self.busy = False
        self.worker_queue: Queue[tuple[str, object]] = Queue()
        self.pending_sync_reload = False
        self.action_buttons: list[ttk.Button] = []

        self._build_widgets()
        self._bind_dirty_tracking()
        self.github_status_var.set(infer_github_login_status())
        self.refresh_target_display()
        repo_dir = self.current_cache_repo_dir()
        if (repo_dir / ".git").exists():
            self.scan_repository()

    def _build_widgets(self) -> None:
        frame = ttk.Frame(self, padding=12)
        frame.pack(fill=BOTH, expand=True)

        form = ttk.Frame(frame)
        form.pack(fill="x")
        for column in range(4):
            form.columnconfigure(column, weight=(1 if column in {1, 3} else 0))

        ttk.Label(form, text="仓库路径").grid(row=0, column=0, sticky=W, pady=4)
        ttk.Label(form, textvariable=self.repo_var).grid(row=0, column=1, columnspan=2, sticky=W, padx=8, pady=4)
        ttk.Button(form, text="同步仓库", command=self.sync_repository).grid(row=0, column=3, sticky="ew", pady=4)

        ttk.Label(form, text="owner/repo").grid(row=1, column=0, sticky=W, pady=4)
        ttk.Entry(form, textvariable=self.owner_repo_var).grid(row=1, column=1, sticky="ew", padx=8, pady=4)
        ttk.Label(form, text="分支").grid(row=1, column=2, sticky=W, padx=(8, 0), pady=4)
        ttk.Entry(form, textvariable=self.branch_var).grid(row=1, column=3, sticky="ew", pady=4)

        ttk.Label(form, text="提交信息").grid(row=2, column=0, sticky=W, pady=4)
        ttk.Entry(form, textvariable=self.message_var).grid(row=2, column=1, columnspan=3, sticky="ew", padx=8, pady=4)

        ttk.Label(form, text="GitHub").grid(row=3, column=0, sticky=W, pady=4)
        ttk.Label(form, textvariable=self.github_status_var).grid(row=3, column=1, columnspan=2, sticky=W, padx=8, pady=4)
        ttk.Button(form, text="刷新登录状态", command=self.refresh_github_status).grid(row=3, column=3, sticky="ew", pady=4)

        ttk.Label(form, text="当前目标").grid(row=4, column=0, sticky=W, pady=4)
        ttk.Label(form, textvariable=self.target_var).grid(row=4, column=1, columnspan=3, sticky=W, padx=8, pady=4)

        ttk.Checkbutton(form, text="保存元数据时自动把日期刷新为当前时间", variable=self.auto_refresh_date_var).grid(
            row=5, column=0, columnspan=4, sticky=W, pady=(4, 8)
        )

        action_bar = ttk.Frame(frame)
        action_bar.pack(fill="x", pady=(4, 10))
        self.action_buttons = [
            ttk.Button(action_bar, text="扫描仓库", command=self.scan_repository),
            ttk.Button(action_bar, text="新建 JSON", command=self.create_new_json),
            ttk.Button(action_bar, text="选择已有 JSON", command=self.choose_existing_json),
            ttk.Button(action_bar, text="删除当前项", command=self.delete_current_item),
            ttk.Button(action_bar, text="发布当前项", command=self.publish_selected_source),
            ttk.Button(action_bar, text="保存源 JSON", command=self.save_current_source_json),
            ttk.Button(action_bar, text="保存元数据", command=self.save_current_metadata),
            ttk.Button(action_bar, text="刷新日期", command=self.refresh_current_date),
            ttk.Button(action_bar, text="重新绑定", command=self.rebind_current_record),
            ttk.Button(action_bar, text="预览", command=self.preview_index),
            ttk.Button(action_bar, text="重建索引", command=self.rebuild_index),
            ttk.Button(action_bar, text="提交并推送", command=self.commit_push),
        ]
        for index, button in enumerate(self.action_buttons):
            button.pack(side=LEFT, padx=(0 if index == 0 else 8, 0))

        ttk.Label(frame, textvariable=self.summary_var).pack(anchor=W, pady=(0, 8))

        status_frame = ttk.Frame(frame)
        status_frame.pack(fill="x", pady=(0, 8))
        ttk.Label(status_frame, text="状态").pack(side=LEFT)
        ttk.Label(status_frame, textvariable=self.status_var).pack(side=LEFT, padx=(8, 12))
        self.progress_bar = ttk.Progressbar(status_frame, maximum=100, variable=self.progress_var)
        self.progress_bar.pack(side=LEFT, fill="x", expand=True)

        body = PanedWindow(frame, orient="horizontal")
        body.pack(fill=BOTH, expand=True)
        left = ttk.Frame(body, padding=(0, 0, 8, 0))
        right = ttk.Frame(body)
        body.add(left, width=520)
        body.add(right)

        self.pack_tree = ttk.Treeview(left, columns=("status", "id", "name", "source"), show="headings", height=22)
        self.pack_tree.heading("status", text="状态")
        self.pack_tree.heading("id", text="ID")
        self.pack_tree.heading("name", text="名称")
        self.pack_tree.heading("source", text="源文件")
        self.pack_tree.column("status", width=120, anchor=W)
        self.pack_tree.column("id", width=70, anchor=W)
        self.pack_tree.column("name", width=180, anchor=W)
        self.pack_tree.column("source", width=320, anchor=W)
        self.pack_tree.pack(side=LEFT, fill=BOTH, expand=True)
        self.pack_tree.bind("<<TreeviewSelect>>", self.on_tree_select)

        tree_scroll = ttk.Scrollbar(left, orient=VERTICAL, command=self.pack_tree.yview)
        tree_scroll.pack(side=RIGHT, fill="y")
        self.pack_tree.configure(yscrollcommand=tree_scroll.set)

        editor = ttk.LabelFrame(right, text="当前记录", padding=12)
        editor.pack(fill="x")
        editor.columnconfigure(1, weight=1)

        ttk.Label(editor, text="状态").grid(row=0, column=0, sticky=W, pady=4)
        ttk.Label(editor, textvariable=self.record_status_var).grid(row=0, column=1, sticky=W, pady=4)
        ttk.Label(editor, text="ID").grid(row=1, column=0, sticky=W, pady=4)
        ttk.Label(editor, textvariable=self.record_id_var).grid(row=1, column=1, sticky=W, pady=4)
        ttk.Label(editor, text="源文件").grid(row=2, column=0, sticky=W, pady=4)
        ttk.Label(editor, textvariable=self.record_source_file_var).grid(row=2, column=1, sticky=W, pady=4)
        ttk.Label(editor, text="名称").grid(row=3, column=0, sticky=W, pady=4)
        ttk.Entry(editor, textvariable=self.record_name_var).grid(row=3, column=1, sticky="ew", pady=4)
        ttk.Label(editor, text="作者").grid(row=4, column=0, sticky=W, pady=4)
        ttk.Entry(editor, textvariable=self.record_author_var).grid(row=4, column=1, sticky="ew", pady=4)
        ttk.Label(editor, text="简介").grid(row=5, column=0, sticky=W, pady=4)
        ttk.Entry(editor, textvariable=self.record_summary_var).grid(row=5, column=1, sticky="ew", pady=4)
        ttk.Label(editor, text="类型").grid(row=6, column=0, sticky=W, pady=4)
        ttk.Combobox(editor, textvariable=self.record_type_var, state="readonly", values=PACK_TYPE_OPTIONS).grid(row=6, column=1, sticky="ew", pady=4)
        ttk.Label(editor, text="版本").grid(row=7, column=0, sticky=W, pady=4)
        ttk.Entry(editor, textvariable=self.record_version_var).grid(row=7, column=1, sticky="ew", pady=4)
        ttk.Label(editor, text="日期").grid(row=8, column=0, sticky=W, pady=4)
        ttk.Entry(editor, textvariable=self.record_date_var).grid(row=8, column=1, sticky="ew", pady=4)
        ttk.Label(editor, text="Southside 版本").grid(row=9, column=0, sticky=W, pady=4)
        ttk.Entry(editor, textvariable=self.record_southside_version_var).grid(row=9, column=1, sticky="ew", pady=4)
        ttk.Label(editor, text="重新绑定目标").grid(row=10, column=0, sticky=W, pady=4)
        self.rebind_combo = ttk.Combobox(editor, textvariable=self.rebind_source_var, state="readonly")
        self.rebind_combo.grid(row=10, column=1, sticky="ew", pady=4)

        source_editor = ttk.LabelFrame(right, text="源 JSON", padding=12)
        source_editor.pack(fill=BOTH, expand=True, pady=(10, 0))
        self.source_text = ScrolledText(source_editor, wrap="none", font=("Consolas", 10), height=18)
        self.source_text.pack(fill=BOTH, expand=True)

        ttk.Label(right, text="日志").pack(anchor=W, pady=(10, 4))
        self.log_box = ScrolledText(right, wrap="word", font=("Consolas", 10), height=18)
        self.log_box.pack(fill=BOTH, expand=True)

    def _bind_dirty_tracking(self) -> None:
        for variable in [
            self.record_name_var,
            self.record_author_var,
            self.record_summary_var,
            self.record_type_var,
            self.record_version_var,
            self.record_date_var,
            self.record_southside_version_var,
        ]:
            variable.trace_add("write", self._on_form_changed)
        self.source_text.bind("<<Modified>>", self._on_source_text_modified)

    def _on_form_changed(self, *_args) -> None:
        if self.suspend_dirty_tracking:
            return
        self.refresh_dirty_state()

    def _on_source_text_modified(self, _event=None) -> None:
        if not self.source_text.edit_modified():
            return
        self.source_text.edit_modified(False)
        if self.suspend_dirty_tracking:
            return
        self.refresh_dirty_state()

    def refresh_github_status(self) -> None:
        self.github_status_var.set(infer_github_login_status())

    def current_cache_repo_dir(self) -> Path:
        repo_dir = preferred_repo_dir(self.owner_repo_var.get() or DEFAULT_OWNER_REPO)
        self.repo_var.set(str(repo_dir))
        return repo_dir

    def refresh_target_display(self) -> None:
        self.target_var.set(target_display(self.owner_repo_var.get(), self.branch_var.get(), self.current_cache_repo_dir()))

    def set_busy(self, busy: bool, status_text: str | None = None) -> None:
        self.busy = busy
        for button in self.action_buttons:
            button.configure(state=("disabled" if busy else "normal"))
        if status_text is not None:
            self.status_var.set(status_text)
        if not busy:
            self.progress_var.set(0.0)

    def set_progress_status(self, status_text: str, percent: float | None = None) -> None:
        self.status_var.set(status_text)
        if percent is not None:
            self.progress_var.set(max(0.0, min(100.0, percent)))

    def log(self, text: str) -> None:
        self.log_box.insert(END, text + "\n")
        self.log_box.see(END)
        self.update_idletasks()

    def clear_log(self) -> None:
        self.log_box.delete("1.0", END)

    def refresh_dirty_state(self) -> None:
        meta_snapshot = self.current_form_snapshot()
        source_snapshot = self.current_source_snapshot()
        self.metadata_dirty = (
            self.loaded_meta_snapshot is not None and meta_snapshot is not None and meta_snapshot != self.loaded_meta_snapshot
        )
        self.source_dirty = (
            self.loaded_source_snapshot is not None and source_snapshot is not None and source_snapshot != self.loaded_source_snapshot
        )
        self.form_dirty = self.metadata_dirty or self.source_dirty
        base = self.summary_var.get()
        if base.startswith("[Unsaved] "):
            base = base[len("[Unsaved] ") :]
        self.summary_var.set(f"[Unsaved] {base}" if self.form_dirty else base)

    def current_form_snapshot(self) -> tuple[str, ...] | None:
        if self.current_kind is None:
            return None
        return (
            self.record_name_var.get().strip(),
            self.record_author_var.get().strip(),
            self.record_summary_var.get().strip(),
            self.record_type_var.get().strip(),
            self.record_version_var.get().strip(),
            self.record_date_var.get().strip(),
            self.record_southside_version_var.get().strip(),
            self.record_source_file_var.get().strip(),
        )

    def current_metadata_editor_values(self) -> tuple[str, ...] | None:
        if self.current_kind is None:
            return None
        return (
            self.record_name_var.get(),
            self.record_author_var.get(),
            self.record_summary_var.get(),
            self.record_type_var.get(),
            self.record_version_var.get(),
            self.record_date_var.get(),
            self.record_southside_version_var.get(),
        )

    def apply_metadata_editor_values(self, values: tuple[str, ...]) -> None:
        self.suspend_dirty_tracking = True
        try:
            (
                name,
                author,
                summary,
                pack_type,
                version,
                date,
                southside_version,
            ) = values
            self.record_name_var.set(name)
            self.record_author_var.set(author)
            self.record_summary_var.set(summary)
            self.record_type_var.set(pack_type)
            self.record_version_var.set(version)
            self.record_date_var.set(date)
            self.record_southside_version_var.set(southside_version)
        finally:
            self.suspend_dirty_tracking = False

    def current_source_snapshot(self) -> str | None:
        if self.current_kind is None:
            return None
        return normalize_source_editor_text(self.source_text.get("1.0", "end-1c"))

    def set_source_editor_text(self, text: str) -> None:
        self.source_text.delete("1.0", END)
        self.source_text.insert("1.0", normalize_source_editor_text(text))
        self.source_text.edit_modified(False)

    def confirm_unsaved_changes(self, action_text: str) -> bool:
        if not self.form_dirty:
            return True
        if self.current_kind == "published":
            choice = messagebox.askyesnocancel(
                "Southside 发布器",
                f"当前记录有未保存修改，{action_text} 前要先保存吗？",
                parent=self,
            )
            if choice is None:
                return False
            if choice:
                self.save_current_changes(show_message=False)
            return True
        if self.current_kind == "unpublished":
            if self.metadata_dirty:
                choice = messagebox.askyesnocancel(
                    "Southside 发布器",
                    f"当前未发布草稿有修改，{action_text} 前要先发布吗？",
                    parent=self,
                )
                if choice is None:
                    return False
                if choice:
                    self.publish_selected_source(show_message=False)
                return True
            choice = messagebox.askyesnocancel(
                "Southside 发布器",
                f"当前源 JSON 有未保存修改，{action_text} 前要先保存吗？",
                parent=self,
            )
            if choice is None:
                return False
            if choice:
                self.save_current_source_json(show_message=False)
            return True
        return True

    def resolve_inputs(self) -> tuple[Path, str, str]:
        repo_dir = self.current_cache_repo_dir()
        owner_repo = normalize_owner_repo(self.owner_repo_var.get() or default_owner_repo())
        branch = clean_string(self.branch_var.get()) or DEFAULT_BRANCH
        self.owner_repo_var.set(owner_repo)
        self.branch_var.set(branch)
        self.repo_var.set(str(repo_dir))
        self.refresh_target_display()
        return repo_dir, owner_repo, branch

    def load_state(self, repo_dir: Path) -> None:
        self.scan_state = scan_repository_state(repo_dir)
        base_summary = (
            f"已发布 {len([record for record in self.scan_state.published if record.source is not None])} | "
            f"源文件缺失 {len([record for record in self.scan_state.published if record.source is None])} | "
            f"未发布文件 {len(self.scan_state.unpublished)} | 最大 ID {self.scan_state.registry.max_pack_id}"
        )
        self.summary_var.set(base_summary)
        self.rebuild_tree()
        self.form_dirty = False

    def scan_repository(self) -> None:
        if self.busy:
            return
        self.clear_log()
        try:
            repo_dir, _, _ = self.resolve_inputs()
            repo_dir = ensure_repo_dir(str(repo_dir))
            self.load_state(repo_dir)
            self.log(f"已扫描 {repo_dir}")
            assert self.scan_state is not None
            for warning in self.scan_state.warnings:
                self.log(f"[warn] {warning}")
            self.set_progress_status("空闲", 0)
        except Exception as exc:
            messagebox.showerror("Southside 发布器", str(exc))

    def rebuild_tree(self) -> None:
        for item in self.pack_tree.get_children():
            self.pack_tree.delete(item)
        if self.scan_state is None:
            self.load_form(None)
            return
        for record in sorted(self.scan_state.published, key=lambda item: item.meta.pack_id):
            self.pack_tree.insert(
                "",
                END,
                iid=f"pub:{record.meta.pack_id}",
                values=(record.status, record.meta.pack_id, record.meta.name, record.meta.source_file),
            )
        for source in self.scan_state.unpublished:
            self.pack_tree.insert("", END, iid=f"src:{source.rel_path}", values=("未发布", "", source.stem, source.rel_path))
        children = self.pack_tree.get_children()
        if children:
            target = self.current_item_key if self.current_item_key in children else children[0]
            self.pack_tree.selection_set(target)
            self.pack_tree.focus(target)
            self.load_form(target)
        else:
            self.load_form(None)

    def select_tree_item(self, item_key: str) -> None:
        if item_key not in self.pack_tree.get_children():
            raise PublishError(f"列表中找不到这条记录: {item_key}")
        if item_key != self.current_item_key:
            if not self.confirm_unsaved_changes("切换选择"):
                return
        self.pack_tree.selection_set(item_key)
        self.pack_tree.focus(item_key)
        self.load_form(item_key)

    def focus_source_file(self, source_file: str) -> None:
        if self.scan_state is None:
            raise PublishError("仓库尚未扫描")
        for record in self.scan_state.published:
            if record.meta.source_file == source_file:
                self.select_tree_item(f"pub:{record.meta.pack_id}")
                return
        for source in self.scan_state.unpublished:
            if source.rel_path == source_file:
                self.select_tree_item(f"src:{source.rel_path}")
                return
        raise PublishError(f"在 packs/ 目录中找不到所选 JSON: {source_file}")

    def build_form_meta(self, pack_id: int | None, source_file: str) -> PackMeta:
        version = parse_version_text(self.record_version_var.get().strip() or "1")
        date = self.record_date_var.get().strip() or utc_now()
        return PackMeta(
            pack_id=pack_id or 0,
            name=self.record_name_var.get().strip() or Path(source_file).stem,
            author=self.record_author_var.get().strip(),
            summary=self.record_summary_var.get().strip() or Path(source_file).stem,
            pack_type=normalize_pack_type(self.record_type_var.get().strip()),
            version=version,
            date=date,
            southside_version=self.record_southside_version_var.get().strip(),
            source_file=normalize_source_file(source_file),
            created_at=utc_now(),
        )

    def populate_form_from_meta(self, meta: PackMeta, status_text: str, pack_id_text: str) -> None:
        self.record_status_var.set(status_text)
        self.record_id_var.set(pack_id_text)
        self.record_source_file_var.set(meta.source_file)
        self.record_name_var.set(meta.name)
        self.record_author_var.set(meta.author)
        self.record_summary_var.set(meta.summary)
        self.record_type_var.set(meta.pack_type)
        self.record_version_var.set(str(meta.version))
        self.record_date_var.set(meta.date)
        self.record_southside_version_var.set(meta.southside_version)

    def update_rebind_choices(self) -> None:
        if self.scan_state is None:
            self.rebind_combo.configure(values=())
            self.rebind_source_var.set("")
            return
        choices: list[str] = []
        current_source = self.record_source_file_var.get().strip()
        if current_source and self.current_kind == "published":
            choices.append(current_source)
        choices.extend(source.rel_path for source in self.scan_state.unpublished if source.rel_path != current_source)
        self.rebind_combo.configure(values=choices)
        self.rebind_source_var.set(choices[0] if choices else "")

    def load_form(self, item_key: str | None) -> None:
        self.current_item_key = item_key
        self.current_kind = None
        self.suspend_dirty_tracking = True
        try:
            if item_key is None or self.scan_state is None:
                self.record_status_var.set("")
                self.record_id_var.set("")
                self.record_source_file_var.set("")
                self.record_name_var.set("")
                self.record_author_var.set("")
                self.record_summary_var.set("")
                self.record_type_var.set(PACK_TYPE_SAFE)
                self.record_version_var.set("1")
                self.record_date_var.set("")
                self.record_southside_version_var.set("")
                self.set_source_editor_text("")
                self.loaded_meta_snapshot = None
                self.loaded_source_snapshot = None
                self.metadata_dirty = False
                self.source_dirty = False
                self.form_dirty = False
                self.update_rebind_choices()
                return
            if item_key.startswith("pub:"):
                pack_id = as_int(item_key.split(":", 1)[1], 0)
                record = next((item for item in self.scan_state.published if item.meta.pack_id == pack_id), None)
                if record is None:
                    self.load_form(None)
                    return
                self.current_kind = "published"
                self.populate_form_from_meta(record.meta, record.status, str(record.meta.pack_id))
            elif item_key.startswith("src:"):
                source_file = item_key.split(":", 1)[1]
                source = next((item for item in self.scan_state.unpublished if item.rel_path == source_file), None)
                if source is None:
                    self.load_form(None)
                    return
                self.current_kind = "unpublished"
                draft = default_meta_for_source(0, source.rel_path)
                draft = replace(draft, date=utc_now())
                self.populate_form_from_meta(draft, "未发布", f"下一个: {self.scan_state.registry.max_pack_id + 1}")
            source_text = read_source_text(self.scan_state.repo_dir, self.record_source_file_var.get().strip()) if self.current_kind else ""
            self.set_source_editor_text(source_text)
            self.loaded_meta_snapshot = self.current_form_snapshot()
            self.loaded_source_snapshot = self.current_source_snapshot()
            self.metadata_dirty = False
            self.source_dirty = False
            self.form_dirty = False
            self.update_rebind_choices()
        finally:
            self.suspend_dirty_tracking = False
        self.refresh_dirty_state()

    def on_tree_select(self, _event=None) -> None:
        if self.busy:
            return
        items = self.pack_tree.selection()
        if not items:
            return
        target = items[0]
        if target == self.current_item_key:
            return
        if not self.confirm_unsaved_changes("切换选择"):
            if self.current_item_key:
                self.pack_tree.selection_set(self.current_item_key)
                self.pack_tree.focus(self.current_item_key)
            return
        self.load_form(target)

    def choose_existing_json(self) -> None:
        if self.busy:
            return
        try:
            repo_dir, _, _ = self.resolve_inputs()
            repo_dir = ensure_repo_dir(str(repo_dir))
            initial_dir = repo_dir / "packs"
            if not initial_dir.exists():
                raise PublishError(f"Missing packs directory: {initial_dir}")
            selected = filedialog.askopenfilename(
                parent=self,
                title="选择已有 JSON",
                initialdir=str(initial_dir),
                filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
            )
            if not selected:
                return
            source_file = source_file_from_path(repo_dir, Path(selected))
            self.load_state(repo_dir)
            self.focus_source_file(source_file)
            self.log(f"已选择 {source_file}")
        except Exception as exc:
            messagebox.showerror("Southside 发布器", str(exc))

    def create_new_json(self) -> None:
        if self.busy:
            return
        try:
            if self.current_item_key and self.form_dirty and not self.confirm_unsaved_changes("新建 JSON"):
                return
            repo_dir, _, _ = self.resolve_inputs()
            repo_dir = ensure_repo_dir(str(repo_dir))
            file_name = simpledialog.askstring("新建 JSON", "输入新的 JSON 文件名", parent=self)
            if file_name is None:
                return
            source = create_source_file(repo_dir, file_name, "{}")
            self.load_state(repo_dir)
            self.focus_source_file(source.rel_path)
            self.log(f"已新建 {source.rel_path}")
            messagebox.showinfo("Southside 发布器", f"已新建 {source.rel_path}")
        except Exception as exc:
            messagebox.showerror("Southside 发布器", str(exc))

    def delete_current_item(self) -> None:
        if self.busy:
            return
        try:
            if self.current_kind is None:
                raise PublishError("请先选择一个包")
            if self.form_dirty:
                should_continue = messagebox.askyesno(
                    "Southside 发布器",
                    "当前有未保存修改，删除会直接丢弃这些修改，继续吗？",
                    parent=self,
                )
                if not should_continue:
                    return
            repo_dir, _, _ = self.resolve_inputs()
            repo_dir = ensure_repo_dir(str(repo_dir))
            if self.current_kind == "published":
                pack_id = self.current_published_id()
                source_file = self.current_source_file()
                should_delete = messagebox.askyesno(
                    "Southside 发布器",
                    f"删除已发布包后会移除绑定和元数据，并删除源文件 {source_file}。继续吗？",
                    parent=self,
                )
                if not should_delete:
                    return
                unpublish_pack(repo_dir, pack_id, delete_source=True)
                self.load_state(repo_dir)
                self.log(f"已删除已发布包 id {pack_id}: {source_file}")
                messagebox.showinfo("Southside 发布器", f"已删除已发布包 id {pack_id}")
                return
            source_file = self.current_source_file()
            should_delete = messagebox.askyesno(
                "Southside 发布器",
                f"确认删除未发布源文件 {source_file} 吗？",
                parent=self,
            )
            if not should_delete:
                return
            delete_source_file(repo_dir, source_file)
            self.load_state(repo_dir)
            self.log(f"已删除未发布源文件 {source_file}")
            messagebox.showinfo("Southside 发布器", f"已删除 {source_file}")
        except Exception as exc:
            messagebox.showerror("Southside 发布器", str(exc))

    def refresh_current_date(self) -> None:
        self.record_date_var.set(utc_now())

    def current_published_id(self) -> int:
        if self.current_kind != "published":
            raise PublishError("当前没有选中已发布的包")
        pack_id = as_int(self.record_id_var.get().strip(), 0)
        if pack_id <= 0:
            raise PublishError("当前 id 非法")
        return pack_id

    def current_source_file(self) -> str:
        source_file = self.record_source_file_var.get().strip()
        return normalize_source_file(source_file)

    def persist_current_source_json(self, repo_dir: Path) -> Path:
        source_file = self.current_source_file()
        source_text = self.current_source_snapshot()
        if source_text is None:
            raise PublishError("当前没有可保存的源 JSON")
        return write_source_text(repo_dir, source_file, source_text)

    def persist_current_metadata(self, repo_dir: Path) -> PackMeta:
        if self.current_kind != "published":
            raise PublishError("请先选择一个已发布的包")
        pack_id = self.current_published_id()
        source_file = self.current_source_file()
        meta = self.build_form_meta(pack_id, source_file)
        existing = next(
            (record.meta for record in (self.scan_state.published if self.scan_state else []) if record.meta.pack_id == pack_id),
            None,
        )
        if existing is not None:
            meta = replace(meta, created_at=existing.created_at)
        if self.auto_refresh_date_var.get():
            meta = replace(meta, date=utc_now())
        save_metadata(repo_dir, meta)
        return meta

    def save_current_source_json(self, show_message: bool = True) -> None:
        if self.busy:
            return
        try:
            if self.current_kind is None:
                raise PublishError("请先选择一个包")
            repo_dir, _, _ = self.resolve_inputs()
            repo_dir = ensure_repo_dir(str(repo_dir))
            draft_values = self.current_metadata_editor_values()
            should_preserve_metadata = bool(self.metadata_dirty and draft_values is not None)
            source_file = self.current_source_file()
            path = self.persist_current_source_json(repo_dir)
            self.load_state(repo_dir)
            self.focus_source_file(source_file)
            if should_preserve_metadata and draft_values is not None:
                loaded_meta_snapshot = self.loaded_meta_snapshot
                self.apply_metadata_editor_values(draft_values)
                self.loaded_meta_snapshot = loaded_meta_snapshot
                self.loaded_source_snapshot = self.current_source_snapshot()
                self.refresh_dirty_state()
            self.log(f"已保存源 JSON: {path}")
            if show_message:
                messagebox.showinfo("Southside 发布器", "源 JSON 已保存")
        except Exception as exc:
            messagebox.showerror("Southside 发布器", str(exc))

    def save_current_changes(self, show_message: bool = True) -> None:
        if self.busy:
            return
        try:
            if self.current_kind != "published":
                raise PublishError("当前记录不能直接保存全部修改")
            repo_dir, _, _ = self.resolve_inputs()
            repo_dir = ensure_repo_dir(str(repo_dir))
            source_file = self.current_source_file()
            changed_parts: list[str] = []
            if self.source_dirty:
                self.persist_current_source_json(repo_dir)
                changed_parts.append("源 JSON")
            if self.metadata_dirty:
                self.persist_current_metadata(repo_dir)
                changed_parts.append("元数据")
            if not changed_parts:
                return
            self.load_state(repo_dir)
            self.focus_source_file(source_file)
            self.log(f"已保存 {', '.join(changed_parts)}")
            if show_message:
                messagebox.showinfo("Southside 发布器", f"已保存 {', '.join(changed_parts)}")
        except Exception as exc:
            messagebox.showerror("Southside 发布器", str(exc))

    def persist_current_record_for_index(self, repo_dir: Path, publish_unpublished: bool) -> tuple[str | None, bool]:
        if self.current_kind is None:
            return None, False
        current_source = self.current_source_file()
        changed = False
        if self.source_dirty:
            self.persist_current_source_json(repo_dir)
            changed = True
        if self.current_kind == "published":
            if self.metadata_dirty:
                self.persist_current_metadata(repo_dir)
                changed = True
            return current_source, changed
        if publish_unpublished:
            meta = self.build_form_meta(None, current_source)
            publish_source_file(repo_dir, meta.source_file, meta)
            self.log(f"已发布 {meta.source_file}")
            changed = True
        return current_source, changed

    def publish_selected_source(self, show_message: bool = True) -> None:
        if self.busy:
            return
        try:
            if self.current_kind != "unpublished":
                raise PublishError("请先选择一个未发布的源文件")
            repo_dir, _, _ = self.resolve_inputs()
            repo_dir = ensure_repo_dir(str(repo_dir))
            self.persist_current_source_json(repo_dir)
            meta = self.build_form_meta(None, self.current_source_file())
            publish_source_file(repo_dir, meta.source_file, meta)
            self.load_state(repo_dir)
            self.focus_source_file(meta.source_file)
            self.log(f"已发布 {meta.source_file}")
            if show_message:
                messagebox.showinfo("Southside 发布器", "已发布新的包元数据")
        except Exception as exc:
            messagebox.showerror("Southside 发布器", str(exc))

    def save_current_metadata(self, show_message: bool = True) -> None:
        if self.busy:
            return
        try:
            if self.current_kind != "published":
                raise PublishError("请先选择一个已发布的包")
            repo_dir, _, _ = self.resolve_inputs()
            repo_dir = ensure_repo_dir(str(repo_dir))
            meta = self.persist_current_metadata(repo_dir)
            self.load_state(repo_dir)
            self.focus_source_file(meta.source_file)
            self.log(f"已保存 id {meta.pack_id} 的元数据")
            if show_message:
                messagebox.showinfo("Southside 发布器", "元数据已保存")
        except Exception as exc:
            messagebox.showerror("Southside 发布器", str(exc))

    def rebind_current_record(self) -> None:
        if self.busy:
            return
        try:
            if self.current_kind != "published":
                raise PublishError("请先选择一个已发布的包")
            new_source = self.rebind_source_var.get().strip()
            if not new_source:
                raise PublishError("还没有选择重新绑定目标")
            repo_dir, _, _ = self.resolve_inputs()
            repo_dir = ensure_repo_dir(str(repo_dir))
            pack_id = self.current_published_id()
            rebind_pack_source(repo_dir, pack_id, new_source)
            self.load_state(repo_dir)
            self.log(f"已把 id {pack_id} 重新绑定到 {new_source}")
            messagebox.showinfo("Southside 发布器", "当前包已重新绑定")
        except Exception as exc:
            messagebox.showerror("Southside 发布器", str(exc))

    def preview_index(self) -> None:
        if self.busy:
            return
        self.clear_log()
        try:
            repo_dir, owner_repo, branch = self.resolve_inputs()
            repo_dir = ensure_repo_dir(str(repo_dir))
            current_source, persisted = self.persist_current_record_for_index(repo_dir, publish_unpublished=False)
            state = scan_repository_state(repo_dir)
            result = build_index_data(owner_repo, branch, state)
            if current_source is not None and persisted:
                self.load_state(repo_dir)
                self.focus_source_file(current_source)
            elif self.current_kind == "unpublished":
                self.log("[warn] 当前 JSON 还未发布，预览不会分配 ID，也不会出现在 index.json 中")
            self.summary_var.set(build_summary(result))
            for line in preview_lines(result):
                self.log(line)
        except Exception as exc:
            messagebox.showerror("Southside 发布器", str(exc))

    def rebuild_index(self) -> None:
        if self.busy:
            return
        self.clear_log()
        try:
            repo_dir, owner_repo, branch = self.resolve_inputs()
            repo_dir = ensure_repo_dir(str(repo_dir))
            current_source, _ = self.persist_current_record_for_index(repo_dir, publish_unpublished=True)
            state = scan_repository_state(repo_dir)
            result = build_index_data(owner_repo, branch, state)
            path = write_index_file(repo_dir, result)
            self.load_state(repo_dir)
            if current_source is not None:
                self.focus_source_file(current_source)
            self.summary_var.set(build_summary(result))
            self.log(build_summary(result))
            self.log(f"已写入 {path}")
            for warning in result.warnings:
                self.log(f"[warn] {warning}")
            self.set_progress_status("index.json 已重建", 100)
            messagebox.showinfo("Southside 发布器", "index.json 已重建")
            self.set_progress_status("空闲", 0)
        except Exception as exc:
            messagebox.showerror("Southside 发布器", str(exc))

    def start_sync(self, sync_and_scan: bool) -> None:
        if self.busy:
            return
        self.pending_sync_reload = sync_and_scan
        self.refresh_target_display()
        self.set_busy(True, "准备同步仓库")
        repo_dir = self.current_cache_repo_dir()
        owner_repo = normalize_owner_repo(self.owner_repo_var.get() or default_owner_repo())
        branch = clean_string(self.branch_var.get()) or DEFAULT_BRANCH
        worker = threading.Thread(target=self._sync_worker, args=(repo_dir, owner_repo, branch), daemon=True)
        worker.start()
        self.after(100, self.poll_worker_queue)

    def _sync_worker(self, repo_dir: Path, owner_repo: str, branch: str) -> None:
        try:
            message = sync_cached_repo(
                repo_dir,
                owner_repo,
                branch,
                progress_callback=lambda percent, text: self.worker_queue.put(("status", (text, percent))),
                output_callback=lambda text: self.worker_queue.put(("log", text)),
            )
            self.worker_queue.put(("sync_done", (repo_dir, owner_repo, branch, message)))
        except Exception as exc:
            self.worker_queue.put(("error", str(exc)))

    def sync_repository(self) -> None:
        self.start_sync(sync_and_scan=True)

    def start_push_worker(self, repo_dir: Path, message: str) -> None:
        self.set_busy(True, "准备提交")
        worker = threading.Thread(target=self._push_worker, args=(repo_dir, message), daemon=True)
        worker.start()
        self.after(100, self.poll_worker_queue)

    def _push_worker(self, repo_dir: Path, message: str) -> None:
        try:
            self.worker_queue.put(("status", ("正在暂存文件", 0.0)))
            run_git(repo_dir, "add", "index.json", "publisher_meta", "packs")
            if not git_status(repo_dir):
                self.worker_queue.put(("done", ("没有可提交的改动", False)))
                return
            self.worker_queue.put(("status", ("正在提交", 5.0)))
            commit_output = run_git(repo_dir, "commit", "-m", message).strip()
            if commit_output:
                self.worker_queue.put(("log", commit_output))
            branch = infer_branch(repo_dir)
            self.worker_queue.put(("status", (f"正在推送到 origin/{branch}", 10.0)))
            push_output = run_git_push_with_progress(
                repo_dir,
                branch,
                progress_callback=lambda percent, text: self.worker_queue.put(("status", (text, percent))),
                output_callback=lambda text: self.worker_queue.put(("log", text)),
            )
            if push_output:
                self.worker_queue.put(("log", push_output))
            self.worker_queue.put(("done", ("提交并推送完成", True)))
        except Exception as exc:
            self.worker_queue.put(("error", str(exc)))

    def poll_worker_queue(self) -> None:
        try:
            while True:
                kind, payload = self.worker_queue.get_nowait()
                if kind == "log":
                    self.log(str(payload))
                elif kind == "status":
                    text, percent = payload
                    self.set_progress_status(str(text), float(percent) if percent is not None else None)
                elif kind == "sync_done":
                    repo_dir, owner_repo, branch, message = payload
                    self.owner_repo_var.set(normalize_owner_repo(owner_repo))
                    self.branch_var.set(branch)
                    self.repo_var.set(str(repo_dir))
                    self.refresh_target_display()
                    self.github_status_var.set(infer_github_login_status())
                    self.log(str(message))
                    if self.pending_sync_reload:
                        self.load_state(repo_dir)
                    self.pending_sync_reload = False
                    self.set_busy(False, "同步完成")
                    self.set_progress_status("空闲", 0)
                    return
                elif kind == "done":
                    message, changed = payload
                    self.set_busy(False, "空闲")
                    if changed:
                        self.set_progress_status("推送完成", 100)
                    messagebox.showinfo("Southside 发布器", str(message))
                    self.set_progress_status("空闲", 0)
                    return
                elif kind == "error":
                    self.pending_sync_reload = False
                    self.set_busy(False, "失败")
                    messagebox.showerror("Southside 发布器", str(payload))
                    self.set_progress_status("空闲", 0)
                    return
        except Empty:
            pass
        if self.busy:
            self.after(100, self.poll_worker_queue)

    def commit_push(self) -> None:
        if self.busy:
            return
        self.clear_log()
        try:
            repo_dir, owner_repo, branch = self.resolve_inputs()
            repo_dir = ensure_repo_dir(str(repo_dir))
            current_source, _ = self.persist_current_record_for_index(repo_dir, publish_unpublished=True)
            state = scan_repository_state(repo_dir)
            result = build_index_data(owner_repo, branch, state)
            path = write_index_file(repo_dir, result)
            self.load_state(repo_dir)
            if current_source is not None:
                self.focus_source_file(current_source)
            self.summary_var.set(build_summary(result))
            self.log(build_summary(result))
            self.log(f"已写入 {path}")
            self.start_push_worker(repo_dir, self.message_var.get().strip() or DEFAULT_COMMIT_MESSAGE)
        except Exception as exc:
            messagebox.showerror("Southside 发布器", str(exc))


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.gui or len(argv) == 0:
        app = PublisherApp()
        app.mainloop()
        return 0
    return run_cli(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
