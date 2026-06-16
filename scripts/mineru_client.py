#!/usr/bin/env python3
"""MinerU OCR client for CancerRisk v3.

Submits local checkup files (PDF, images, Office, HTML) to the MinerU
精准解析 API and standardizes the per-file outputs at
``analysis_output/artifacts/mineru/<data_id>/``. See
``cancerrisk-skill/references/mineru_api.md`` for the full contract.

Production code never imports ``requests``; the default HTTP transport
is built on top of ``urllib`` so the client has zero runtime
dependencies. Tests inject a fake transport for offline contract
exercise. The orchestrator passes a real
:class:`UrllibTransport` instance.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import io
import json
import sys
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Protocol

SKILL_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Supported inputs
# ---------------------------------------------------------------------------

SUPPORTED_EXTENSIONS = {
    ".pdf",
    ".doc", ".docx",
    ".ppt", ".pptx",
    ".xls", ".xlsx",
    ".html", ".htm",
    ".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp",
}
HTML_EXTENSIONS = {".html", ".htm"}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MinerUConfig:
    api_base: str
    token: str
    token_source: str  # "demo" | "user"
    default_model_version: str = "vlm"
    html_model_version: str = "MinerU-HTML"
    poll_interval_seconds: int = 5
    poll_timeout_seconds: int = 1800
    enable_table: bool = True
    enable_formula: bool = False
    is_ocr: bool = True
    language: str = "ch"
    max_file_size_mb: int = 200
    max_batch_files: int = 200
    network_max_attempts: int = 3
    network_retry_delay_seconds: float = 3.0


# ---------------------------------------------------------------------------
# HTTP transport
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HTTPResponse:
    status_code: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


class Transport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[dict[str, str]] = None,
        json_body: Optional[dict] = None,
        data: Optional[bytes] = None,
    ) -> HTTPResponse: ...


class UrllibTransport:
    """Default real-network transport built on top of :mod:`urllib`.

    Important quirk: ``urllib.request`` injects
    ``Content-Type: application/x-www-form-urlencoded`` for any PUT/POST
    that carries a body without a Content-Type header. That breaks the
    Aliyun OSS pre-signed PUT signature (which is computed over an
    empty Content-Type). We always pass an explicit Content-Type — empty
    string when the caller did not specify one for a raw byte upload —
    so urllib never silently injects a wrong value.
    """

    def __init__(self, timeout_seconds: int = 120) -> None:
        self._timeout = timeout_seconds

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[dict[str, str]] = None,
        json_body: Optional[dict] = None,
        data: Optional[bytes] = None,
    ) -> HTTPResponse:
        req_headers = dict(headers or {})
        body: Optional[bytes] = None
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            req_headers.setdefault("Content-Type", "application/json")
        elif data is not None:
            body = data
            req_headers.setdefault("Content-Type", "")
        req = urllib.request.Request(url, data=body, method=method)
        for key, value in req_headers.items():
            # add_unredirected_header bypasses urllib's auto-injection logic
            # and preserves empty values verbatim.
            req.add_unredirected_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return HTTPResponse(
                    status_code=resp.status,
                    body=resp.read(),
                    headers=dict(resp.getheaders()),
                )
        except urllib.error.HTTPError as exc:
            return HTTPResponse(
                status_code=exc.code,
                body=exc.read(),
                headers=dict(exc.headers),
            )
        except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise MinerUError(f"network error contacting {url}: {reason}") from exc


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class MinerUError(RuntimeError):
    """Raised for any unrecoverable MinerU client error."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def data_id_for(path: Path) -> str:
    """Stable per-file identifier (SHA1 of name + size, first 16 chars)."""
    h = hashlib.sha1()
    h.update(path.name.encode("utf-8"))
    h.update(b":")
    h.update(str(path.stat().st_size).encode("utf-8"))
    return h.hexdigest()[:16]


def token_fingerprint(token: str) -> str:
    if not token:
        return "****"
    return f"****{token[-4:]}" if len(token) >= 4 else "****"


def model_version_for(path: Path, config: MinerUConfig) -> str:
    return config.html_model_version if path.suffix.lower() in HTML_EXTENSIONS else config.default_model_version


def discover_inputs(input_path: Path, max_depth: int = 1) -> list[Path]:
    """Resolve a path into the list of files MinerU should parse.

    - Single file: returned as a 1-element list (still validated for ext).
    - Folder: returns supported files at depth ``max_depth`` (default 1
      per spec §4.3).
    """
    path = Path(input_path)
    if path.is_file():
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            raise MinerUError(f"unsupported file extension: {path.suffix}")
        return [path]
    if not path.is_dir():
        raise MinerUError(f"input path does not exist: {path}")

    found: list[Path] = []
    if max_depth <= 1:
        candidates = [p for p in path.iterdir() if p.is_file()]
    else:
        candidates = [p for p in path.rglob("*") if p.is_file()]
    for p in candidates:
        if p.name.startswith("."):
            continue
        if p.suffix.lower() in SUPPORTED_EXTENSIONS:
            found.append(p)
    return sorted(found)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class MinerUClient:
    def __init__(
        self,
        config: MinerUConfig,
        transport: Optional[Transport] = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.transport = transport or UrllibTransport()
        self._sleep = sleep
        self._now = now

    # -- public API ---------------------------------------------------------

    def parse_local_files(self, files: list[Path], output_dir: Path) -> dict[str, Any]:
        """Parse local files via the real MinerU API.

        Always calls the live API; the caller is responsible for warning
        the user when ``token_source == "demo"``.
        """
        if not files:
            raise MinerUError("no input files supplied")
        output_dir.mkdir(parents=True, exist_ok=True)

        for p in files:
            if p.stat().st_size > self.config.max_file_size_mb * 1024 * 1024:
                raise MinerUError(f"{p} exceeds {self.config.max_file_size_mb}MB MinerU limit")
        if len(files) > self.config.max_batch_files:
            raise MinerUError(f"{len(files)} files exceed batch limit {self.config.max_batch_files}")

        if len(files) == 1:
            return self._run_live(files, output_dir)
        return self._run_live_one_by_one(files, output_dir)

    # -- live API path ------------------------------------------------------

    def _run_live_one_by_one(self, files: list[Path], output_dir: Path) -> dict[str, Any]:
        records: list[dict[str, Any]] = []
        batch_ids: list[str] = []
        for path in files:
            try:
                manifest = self._run_live([path], output_dir)
            except MinerUError as exc:
                records.append(self._failed_record(path, str(exc)))
                continue
            batch_id = manifest.get("batch_id")
            if batch_id:
                batch_ids.append(str(batch_id))
            records.extend(manifest.get("files", []))
        status = self._derive_status(records)
        return {
            "token_source": self.config.token_source,
            "token_fingerprint": token_fingerprint(self.config.token),
            "model_version": self.config.default_model_version,
            "batch_id": ",".join(batch_ids),
            "batch_ids": batch_ids,
            "files": records,
            "status": status,
        }

    def _run_live(self, files: list[Path], output_dir: Path) -> dict[str, Any]:
        upload_urls, batch_id = self._request_upload_urls(files)
        if len(upload_urls) != len(files):
            raise MinerUError(
                f"MinerU returned {len(upload_urls)} upload URLs for {len(files)} files"
            )
        self._upload_files(files, upload_urls)
        batch_result = self._poll_batch(batch_id)
        records = self._materialize_results(files, batch_result, output_dir)
        status = self._derive_status(records)
        return {
            "token_source": self.config.token_source,
            "token_fingerprint": token_fingerprint(self.config.token),
            "model_version": self.config.default_model_version,
            "batch_id": batch_id,
            "batch_ids": [batch_id],
            "files": records,
            "status": status,
        }

    def _request_upload_urls(self, files: list[Path]) -> tuple[list[str], str]:
        body = {
            "enable_formula": self.config.enable_formula,
            "enable_table": self.config.enable_table,
            "language": self.config.language,
            "model_version": self.config.default_model_version,
            "is_ocr": self.config.is_ocr,
            "files": [
                {
                    "name": f.name,
                    "data_id": data_id_for(f),
                    "is_ocr": self.config.is_ocr,
                }
                for f in files
            ],
        }
        resp = self._request_with_retry(
            "request upload URLs",
            "POST",
            f"{self.config.api_base}/file-urls/batch",
            headers=self._auth_headers(),
            json_body=body,
        )
        self._check(resp, "request upload URLs")
        payload = resp.json()
        data = payload.get("data") or payload
        urls = data.get("file_urls") or data.get("urls") or []
        batch_id = data.get("batch_id") or payload.get("batch_id")
        if not batch_id:
            raise MinerUError(f"MinerU response missing batch_id: {payload}")
        return list(urls), batch_id

    def _upload_files(self, files: list[Path], upload_urls: list[str]) -> None:
        # MinerU returns pre-signed object-storage URLs (Aliyun OSS style).
        # The PUT must not carry an Authorization header and must not
        # override the Content-Type that the signature was computed with —
        # so we send raw bytes with no extra headers at all.
        for path, upload_url in zip(files, upload_urls):
            resp = self._request_with_retry(
                f"upload {path.name}",
                "PUT",
                upload_url,
                headers={},
                data=path.read_bytes(),
            )
            if resp.status_code >= 400:
                raise MinerUError(
                    f"upload failed for {path.name}: HTTP {resp.status_code} {resp.body[:200]!r}"
                )

    def _poll_batch(self, batch_id: str) -> dict[str, Any]:
        url = f"{self.config.api_base}/extract-results/batch/{batch_id}"
        deadline = self._now() + self.config.poll_timeout_seconds
        while True:
            resp = self._request_with_retry("poll batch", "GET", url, headers=self._auth_headers())
            self._check(resp, "poll batch")
            payload = resp.json()
            data = payload.get("data") or payload
            results = data.get("extract_result") or []
            terminal = bool(results) and all(
                (r.get("state") in {"done", "failed"}) for r in results
            )
            if terminal:
                return data
            if self._now() >= deadline:
                raise MinerUError(f"polling timeout for batch {batch_id}")
            self._sleep(self.config.poll_interval_seconds)

    def _materialize_results(
        self,
        files: list[Path],
        batch_result: dict[str, Any],
        output_dir: Path,
    ) -> list[dict[str, Any]]:
        files_by_data_id = {data_id_for(f): f for f in files}
        records = []
        for entry in batch_result.get("extract_result") or []:
            did = entry.get("data_id")
            if not did:
                continue
            source = files_by_data_id.get(did)
            per = output_dir / did
            per.mkdir(parents=True, exist_ok=True)

            outputs: list[str] = []
            if entry.get("state") == "done":
                outputs = self._download_artifacts(entry, per)

            # raw_result.json is always written, even on failure.
            if "raw_result.json" not in outputs:
                (per / "raw_result.json").write_text(
                    json.dumps(entry, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                outputs.append("raw_result.json")

            records.append(
                {
                    "data_id": did,
                    "source_path": str(source) if source else None,
                    "size_bytes": source.stat().st_size if source else None,
                    "state": entry.get("state"),
                    "outputs": outputs,
                    "model_version": (
                        model_version_for(source, self.config) if source else self.config.default_model_version
                    ),
                    "error": entry.get("err_msg"),
                }
            )
        return records

    def _download_artifacts(self, entry: dict[str, Any], per: Path) -> list[str]:
        outputs: list[str] = []
        for src_key, target_name in (
            ("content_md_url", "content.md"),
            ("layout_url", "layout.json"),
            ("raw_result_url", "raw_result.json"),
        ):
            url = entry.get(src_key)
            if not url:
                continue
            resp = self._request_with_retry(
                f"download {target_name}",
                "GET",
                url,
                headers=self._auth_headers(),
            )
            self._check(resp, f"download {target_name}")
            (per / target_name).write_bytes(resp.body)
            outputs.append(target_name)

        zip_url = entry.get("full_zip_url")
        if zip_url and ("content.md" not in outputs or "layout.json" not in outputs):
            zip_resp = self._request_with_retry(
                "download full_zip",
                "GET",
                zip_url,
                headers=self._auth_headers(),
            )
            self._check(zip_resp, "download full_zip")
            self._unpack_zip(zip_resp.body, per, outputs)
        return outputs

    def _unpack_zip(self, body: bytes, per: Path, outputs: list[str]) -> None:
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            for name in zf.namelist():
                target = per / name
                target.parent.mkdir(parents=True, exist_ok=True)
                if name.endswith("/"):
                    continue
                with zf.open(name) as src, open(target, "wb") as dst:
                    dst.write(src.read())
        # Some MinerU bundles call the markdown ``full.md``; expose it as ``content.md``.
        if (per / "full.md").exists() and not (per / "content.md").exists():
            (per / "content.md").write_bytes((per / "full.md").read_bytes())
        for name in ("content.md", "layout.json"):
            if (per / name).exists() and name not in outputs:
                outputs.append(name)
        if (per / "images").exists() and "images/" not in outputs:
            outputs.append("images/")

    @staticmethod
    def _derive_status(records: list[dict[str, Any]]) -> str:
        if not records:
            return "fail"
        states = {r.get("state") for r in records}
        if states == {"done"}:
            return "success"
        if "done" in states:
            return "partial"
        return "fail"

    # -- internals ----------------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.config.token}"}

    def _request_with_retry(
        self,
        action: str,
        method: str,
        url: str,
        *,
        headers: Optional[dict[str, str]] = None,
        json_body: Optional[dict] = None,
        data: Optional[bytes] = None,
    ) -> HTTPResponse:
        max_attempts = max(1, int(self.config.network_max_attempts))
        last_error: Optional[MinerUError] = None
        for attempt in range(1, max_attempts + 1):
            try:
                resp = self.transport.request(
                    method,
                    url,
                    headers=headers,
                    json_body=json_body,
                    data=data,
                )
            except MinerUError as exc:
                last_error = exc
                if attempt >= max_attempts:
                    raise
                self._sleep(self.config.network_retry_delay_seconds)
                continue

            if self._is_retryable_status(resp.status_code) and attempt < max_attempts:
                self._sleep(self.config.network_retry_delay_seconds)
                continue
            return resp
        if last_error is not None:
            raise last_error
        raise MinerUError(f"{action} failed after {max_attempts} attempts")

    @staticmethod
    def _is_retryable_status(status_code: int) -> bool:
        return status_code in {408, 409, 425, 429} or 500 <= status_code < 600

    def _failed_record(self, path: Path, error: str) -> dict[str, Any]:
        return {
            "data_id": data_id_for(path),
            "source_path": str(path),
            "size_bytes": path.stat().st_size,
            "state": "failed",
            "outputs": [],
            "model_version": model_version_for(path, self.config),
            "error": error,
        }

    def _check(self, resp: HTTPResponse, action: str) -> None:
        if 200 <= resp.status_code < 300:
            return
        if resp.status_code in {401, 403}:
            raise MinerUError(
                f"MinerU auth failed during {action} (HTTP {resp.status_code}); "
                "please update API token at https://mineru.net/apiManage/token"
            )
        if resp.status_code == 402:
            raise MinerUError(
                f"MinerU quota exhausted during {action} (HTTP 402); "
                "rotate to a paid token at https://mineru.net/apiManage/token"
            )
        body_preview = resp.body[:300] if resp.body else b""
        raise MinerUError(
            f"MinerU HTTP {resp.status_code} during {action}: {body_preview!r}"
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


DEMO_TOKEN_WARNING = (
    "[mineru] ⚠️  使用仓库内置 demo token（仅供快速体验）。\n"
    "         正式部署请到 https://mineru.net/apiManage/token 注册自己的 token，\n"
    "         并通过 --mineru-token 或本地配置传入；demo token 可能失效或额度不足。"
)


def _load_config_from_yaml(
    path: Path,
    token_override: Optional[str],
    *,
    force_demo_token: bool = False,
) -> MinerUConfig:
    import yaml  # local import; yaml is only needed by the CLI path

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    block = raw.get("mineru", {})
    local_path = path.with_name("local.yaml")
    local_token = None
    if local_path.is_file():
        local_raw = yaml.safe_load(local_path.read_text(encoding="utf-8")) or {}
        local_token = (local_raw.get("mineru") or {}).get("user_token")
    if force_demo_token:
        token = block.get("demo_token")
        if not token:
            raise MinerUError("MinerU demo_token missing in config; pass --mineru-token instead")
        token_source = "demo"
    elif token_override:
        token = token_override
        token_source = "user"
    elif local_token:
        token = str(local_token)
        token_source = "user"
    elif block.get("use_demo_token_by_default", True):
        token = block.get("demo_token")
        if not token:
            raise MinerUError("MinerU demo_token missing in config; pass --mineru-token instead")
        token_source = "demo"
    else:
        raise MinerUError(
            "MinerU token not provided; pass --mineru-token or set mineru.use_demo_token_by_default=true"
        )
    return MinerUConfig(
        api_base=block["api_base"],
        token=token,
        token_source=token_source,
        default_model_version=block.get("default_model_version", "vlm"),
        html_model_version=block.get("html_model_version", "MinerU-HTML"),
        poll_interval_seconds=int(block.get("poll_interval_seconds", 5)),
        poll_timeout_seconds=int(block.get("poll_timeout_seconds", 1800)),
        network_max_attempts=int(block.get("network_max_attempts", 3)),
        network_retry_delay_seconds=float(block.get("network_retry_delay_seconds", 3.0)),
        enable_table=bool(block.get("enable_table", True)),
        enable_formula=bool(block.get("enable_formula", False)),
        is_ocr=bool(block.get("is_ocr", True)),
        language=str(block.get("language", "ch")),
        max_file_size_mb=int(block.get("max_file_size_mb", 200)),
        max_batch_files=int(block.get("max_batch_files", 200)),
    )


def save_local_mineru_token(config_path: Path, token: str) -> Path:
    """Persist a user MinerU token in an ignored local config file."""
    import yaml  # local import; yaml is only needed by the CLI path

    if not token.strip():
        raise MinerUError("MinerU token is empty")
    local_path = config_path.with_name("local.yaml")
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_text(
        yaml.safe_dump(
            {"mineru": {"user_token": token.strip()}},
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return local_path


def run_mineru_stage(
    input_path: Path,
    output_dir: Path,
    config_path: Path,
    *,
    token_override: Optional[str] = None,
    force_demo_token: bool = False,
    transport: Optional[Transport] = None,
) -> dict[str, Any]:
    """Top-level helper for the orchestrator.

    Always hits the live MinerU API. Emits a loud warning when the demo
    token is being used so operators remember to provision their own
    credentials before production deployment.
    """
    config = _load_config_from_yaml(
        config_path,
        token_override,
        force_demo_token=force_demo_token,
    )
    if config.token_source == "demo":
        print(DEMO_TOKEN_WARNING, file=sys.stderr)
    client = MinerUClient(config, transport=transport)
    files = discover_inputs(input_path)
    if not files:
        raise MinerUError(f"no supported MinerU inputs under {input_path}")

    mineru_root = output_dir / "mineru"
    mineru_root.mkdir(parents=True, exist_ok=True)
    manifest = client.parse_local_files(files, mineru_root)
    manifest["input_path"] = str(input_path)
    _write_content_bundle(manifest, mineru_root, output_dir / "content_bundle.md")

    manifest_path = output_dir / "conversion_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def _write_content_bundle(manifest: dict[str, Any], mineru_root: Path, bundle_path: Path) -> None:
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    with open(bundle_path, "w", encoding="utf-8") as bundle:
        for record in manifest.get("files", []):
            if record.get("state") != "done":
                continue
            if "content.md" not in record.get("outputs", []):
                continue
            content_path = mineru_root / str(record["data_id"]) / "content.md"
            if content_path.is_file():
                data_id = str(record["data_id"])
                source_path = str(record.get("source_path") or "")
                bundle.write(
                    f"<!-- CANCERRISK_SOURCE_START data_id={data_id} "
                    f"source_path={json.dumps(source_path, ensure_ascii=False)} -->\n"
                )
                bundle.write(f"# Source data_id: {data_id}\n")
                if source_path:
                    bundle.write(f"# Source file: {source_path}\n")
                bundle.write("\n")
                content = content_path.read_text(encoding="utf-8")
                bundle.write(content)
                if not content.endswith("\n"):
                    bundle.write("\n")
                bundle.write(f"\n<!-- CANCERRISK_SOURCE_END data_id={data_id} -->\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-dir", required=True, help="Directory that will receive mineru/ and conversion_manifest.json")
    parser.add_argument("--config", default=str(SKILL_ROOT / "config" / "formal.yaml"))
    parser.add_argument("--mineru-token", default=None)
    parser.add_argument("--use-demo-mineru-token", action="store_true")
    parser.add_argument("--save-mineru-token", default=None)
    args = parser.parse_args()
    if args.save_mineru_token:
        path = save_local_mineru_token(Path(args.config), args.save_mineru_token)
        print(f"[mineru] saved user token to {path}")
        return
    try:
        manifest = run_mineru_stage(
            Path(args.input),
            Path(args.output_dir),
            Path(args.config),
            token_override=args.mineru_token,
            force_demo_token=args.use_demo_mineru_token,
        )
    except MinerUError as exc:
        print(f"[mineru] FAIL: {exc}", file=sys.stderr)
        sys.exit(1)
    print(
        f"[mineru] status={manifest['status']} files={len(manifest['files'])} "
        f"token={manifest['token_source']} fingerprint={manifest['token_fingerprint']}"
    )


if __name__ == "__main__":
    main()
