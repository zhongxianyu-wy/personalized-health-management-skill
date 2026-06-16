#!/usr/bin/env python3
"""CancerRisk v3+ environment probe.

Checks Python, runtime deps, curl, and (added in v6) the **on-disk
fixtures** the orchestrator needs to start at all — evidence_store
JSON files, HTML templates, and config files. Also surfaces the
MinerU token source so demo-token regressions get flagged before
they hit a real run.

Run modes:

* `--json` — emit machine-readable status, used by orchestrators / CI.
* (default) — human-readable key=value listing.
* `--legacy` — print the v1 payload so any older harness still pinned
  to the old shape keeps working. Will be removed in v4.

`status` is one of `pass` (every required dep + file present),
`warning` (optional pieces missing, run still possible), or `blocked`
(a hard dependency is missing and the orchestrator will fail).
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import shutil
import subprocess
import sys
from pathlib import Path

V3_REQUIRED_DEPS = ("PyYAML", "jsonschema", "jinja2", "requests")
V3_MIN_PYTHON = (3, 10)
V3_RECOMMENDED_PYTHON = (3, 11)

_UV_INSTALL_CMD = (
    "curl -LsSf https://astral.sh/uv/install.sh | sh"
)
_UV_RUN_PREFIX = (
    "uv run --python 3.11 --with PyYAML --with jsonschema --with jinja2 --with requests"
)

_SKILL_ROOT = Path(__file__).resolve().parent.parent

REQUIRED_FIXTURES = tuple(str(_SKILL_ROOT / p) for p in (
    "config/formal.yaml",
    "config/contact.json",
    "references/database/index.json",
    "references/database/cancerrisk/json/cancers.json",
    "references/database/cancerrisk/json/risk_factors.json",
    "references/database/cancerrisk/json/cancer_age_sex_priors.json",
    "references/database/cancerrisk/json/factor_synonyms.json",
    "references/database/cancerrisk/json/risk_assertions_derived.json",
    "references/database/cancerrisk/json/detection_performance_derived.json",
    "references/database/cancerrisk/json/screening_recommendations.json",
    "templates/integrated_report_v14.html",
    "templates/health_summary_v1.html",
))

OPTIONAL_FIXTURES = tuple(str(_SKILL_ROOT / p) for p in (
    "config/local.yaml",
))


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _probe_cli_version(binary: str | None) -> str | None:
    if not binary:
        return None
    for args in ([binary, "--version"], [binary, "-V"]):
        try:
            result = subprocess.run(args, text=True, capture_output=True, timeout=5)
        except Exception:
            continue
        output = (result.stdout or result.stderr or "").strip()
        if output:
            return output.splitlines()[0]
    return None


def _probe_mineru_token() -> dict[str, object]:
    """Surface which MinerU token the skill will use at runtime.

    Reads cancerrisk-skill/config/formal.yaml (no PyYAML hard-required —
    falls back to "unknown" silently if yaml unavailable). Flags the
    bundled demo token loudly because it expires 2026 and is shared.
    """
    cfg_path = _SKILL_ROOT / "config" / "formal.yaml"
    info: dict[str, object] = {"path": str(cfg_path), "token_source": "unknown"}
    if not cfg_path.is_file():
        info["error"] = "config not found"
        return info
    try:
        import yaml  # noqa: WPS433
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        info["error"] = f"config parse failed: {exc}"
        return info
    mineru = cfg.get("mineru", {}) if isinstance(cfg, dict) else {}
    info["token_source"] = mineru.get("token_source") or "unknown"
    info["use_demo_token_by_default"] = bool(mineru.get("use_demo_token_by_default"))
    # Try to decode the JWT exp claim — best effort, no signature check.
    demo_token = mineru.get("demo_token") or ""
    if demo_token and demo_token.count(".") == 2:
        try:
            import base64
            payload_b64 = demo_token.split(".")[1]
            payload_b64 += "=" * (-len(payload_b64) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64))
            if "exp" in payload:
                from datetime import datetime, timezone
                exp_dt = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
                info["demo_token_expires_at"] = exp_dt.isoformat()
                info["demo_token_expired"] = exp_dt < datetime.now(tz=timezone.utc)
        except Exception:
            pass
    return info


def _probe_fixtures() -> dict[str, object]:
    missing_required = [p for p in REQUIRED_FIXTURES if not Path(p).is_file()]
    optional_present = [p for p in OPTIONAL_FIXTURES if Path(p).is_file()]
    return {
        "required": list(REQUIRED_FIXTURES),
        "missing_required": missing_required,
        "optional_present": optional_present,
    }


def v3_payload() -> dict[str, object]:
    py_version = ".".join(str(part) for part in sys.version_info[:3])
    python_ok = sys.version_info >= V3_MIN_PYTHON
    dependencies = {name: _package_version(name) for name in V3_REQUIRED_DEPS}
    curl_path = shutil.which("curl")
    curl_version = _probe_cli_version(curl_path)
    fixtures = _probe_fixtures()
    mineru_token = _probe_mineru_token()

    missing_required = [name for name, ver in dependencies.items() if not ver]
    warnings: list[str] = []

    if mineru_token.get("token_source") == "demo":
        warnings.append("mineru.token_source=demo — set use_demo_token_by_default=false and supply your own token before production.")
    if mineru_token.get("demo_token_expired"):
        warnings.append("bundled MinerU demo token has EXPIRED; orchestrator will fail at task3.")
    if not curl_path:
        warnings.append("curl missing — Task6 (cyzh-cfc API token fetch) will fail; other stages still run.")

    if not python_ok or missing_required or fixtures["missing_required"]:
        status = "blocked"
    elif warnings:
        status = "warning"
    else:
        status = "pass"

    return {
        "script_version": "env-check-v6",
        "python_version": py_version,
        "python_min_required": ".".join(str(p) for p in V3_MIN_PYTHON),
        "python_ok": python_ok,
        "dependencies": dependencies,
        "missing_required_dependencies": missing_required,
        "curl": {"path": curl_path, "version": curl_version},
        "fixtures": fixtures,
        "mineru_token": mineru_token,
        "warnings": warnings,
        "status": status,
        "notes": (
            "Required fixtures = evidence_store JSON + HTML templates + "
            "config/formal.yaml + contact.json. Missing any of these will "
            "crash the orchestrator at import or first read."
        ),
    }


def legacy_payload() -> dict[str, object]:
    """v1 payload kept around for backward-compatible harnesses."""
    return {
        "python_version": ".".join(str(part) for part in sys.version_info[:3]),
        "status": "pass",
        "script_version": "env-check-v1",
    }


def _build_fix_hints(payload: dict) -> list[str]:
    """Return a list of actionable fix instructions based on detected issues."""
    hints: list[str] = []
    py_ver = tuple(int(p) for p in payload.get("python_version", "0.0.0").split(".")[:3])

    if not payload.get("python_ok"):
        current = payload.get("python_version", "unknown")
        minimum = payload.get("python_min_required", "3.10")
        hints.append(
            f"[BLOCKED] Python {current} is below the minimum {minimum}.\n"
            "  Fix option A — use uv (recommended):\n"
            f"    {_UV_INSTALL_CMD}\n"
            f"    uv python install 3.11\n"
            "  Then prefix every command with:\n"
            f"    {_UV_RUN_PREFIX} python ...\n"
            "  Fix option B — use pyenv:\n"
            "    pyenv install 3.11 && pyenv local 3.11\n"
            "    pip install PyYAML jsonschema jinja2 requests"
        )
    elif py_ver < V3_RECOMMENDED_PYTHON:
        hints.append(
            f"[WARNING] Python {payload['python_version']} works but 3.11 is recommended.\n"
            "  Upgrade: uv python install 3.11"
        )

    missing_deps = payload.get("missing_required_dependencies", [])
    if missing_deps:
        deps_str = " ".join(f"--with {d}" for d in missing_deps)
        hints.append(
            f"[BLOCKED] Missing Python packages: {', '.join(missing_deps)}.\n"
            "  Fix option A — uv inline (no install needed):\n"
            f"    uv run --python 3.11 {deps_str} python cancerrisk-skill/scripts/run_formal_analysis.py ...\n"
            "  Fix option B — pip install into current env:\n"
            f"    pip install {' '.join(missing_deps)}"
        )

    missing_fixtures = payload.get("fixtures", {}).get("missing_required", [])
    if missing_fixtures:
        skill_root = str(_SKILL_ROOT)
        short = [f.replace(skill_root + "/", "") for f in missing_fixtures]
        derived = [f for f in short if f.endswith("_derived.json")]
        other = [f for f in short if f not in derived]
        if derived:
            hints.append(
                f"[BLOCKED] Derived evidence files missing: {', '.join(derived)}.\n"
                "  Fix:\n"
                f"    {_UV_RUN_PREFIX} python cancerrisk-skill/scripts/build_derived_evidence.py"
            )
        if other:
            hints.append(
                f"[BLOCKED] Required fixture files missing: {', '.join(other)}.\n"
                "  These must exist in cancerrisk-skill/ — check your git clone or skill installation."
            )

    for w in payload.get("warnings", []):
        if "demo" in w.lower() and "expired" in w.lower():
            hints.append(
                "[BLOCKED] MinerU demo token has expired.\n"
                "  Fix: obtain a production token and run:\n"
                f"    {_UV_RUN_PREFIX} python cancerrisk-skill/scripts/run_formal_analysis.py --save-mineru-token <TOKEN>"
            )
        elif "demo" in w.lower():
            hints.append(
                "[WARNING] Using bundled MinerU demo token — shared and may expire.\n"
                "  Fix: set use_demo_token_by_default: false in config/formal.yaml\n"
                "       and run with --mineru-token <YOUR_TOKEN>"
            )
        elif "curl" in w.lower():
            hints.append(
                "[WARNING] curl not found — Task6 health-summary API call will fail.\n"
                "  Fix (macOS): brew install curl\n"
                "  Fix (Linux): apt-get install curl / yum install curl"
            )

    return hints


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="emit machine-readable JSON")
    parser.add_argument("--legacy", action="store_true",
                        help="emit the v1 payload (deprecated; kept for backward compat)")
    parser.add_argument("--formal", action="store_true",
                        help="alias for v3 mode (default); kept for backward compat")
    args = parser.parse_args()

    payload = legacy_payload() if args.legacy else v3_payload()
    if args.as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for key, value in payload.items():
            print(f"{key}: {value}")
        if not args.legacy:
            hints = _build_fix_hints(payload)
            if hints:
                print("\n--- Environment Setup Required ---")
                for hint in hints:
                    print(f"\n{hint}")
                print(
                    "\nQuick-start (uv, no system install needed):\n"
                    f"  {_UV_RUN_PREFIX} \\\n"
                    "    python cancerrisk-skill/scripts/run_formal_analysis.py --help"
                )
            else:
                print("\nEnvironment OK — ready to run.")

    # Non-zero exit only when v3 mode reports blocked, so CI / orchestrator
    # gates can do `python env_check.py --json && ...`.
    if not args.legacy and isinstance(payload.get("status"), str) and payload["status"] == "blocked":
        sys.exit(1)


if __name__ == "__main__":
    main()
