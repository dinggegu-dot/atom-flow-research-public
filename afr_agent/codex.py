"""使用现有 Codex 登录执行隔离的结构化语义任务。"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

from .library import RequestError


MODEL = "gpt-6.1-terra"


class StructuredResult(dict):
    def __init__(self, output, usage):
        super().__init__(output)
        self.usage = usage


def _completed_usage(events):
    keys = ("input_tokens", "cached_input_tokens", "output_tokens")
    for line in reversed((events or b"").splitlines()):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "turn.completed":
            usage = event.get("usage")
            if isinstance(usage, dict) and all(type(usage.get(key)) is int and usage[key] >= 0 for key in keys):
                return {key: usage[key] for key in keys}
            break
    return None


def measured(runner, instructions, payload, schema):
    """给回执记录完成的模型调用耗时和输入大小，省去正文日志。"""
    size = len((instructions + "\n" + json.dumps(payload, ensure_ascii=False)).encode())
    start = time.monotonic()
    result = runner(instructions, payload, schema)
    timing = {"elapsed_ms": round((time.monotonic() - start) * 1000), "input_bytes": size}
    if isinstance(result, StructuredResult):
        timing["model"] = MODEL
        if result.usage is not None:
            timing["usage"] = result.usage
    return result, timing


def run_structured(instructions, payload, schema, *, timeout=600):
    binary = shutil.which("codex")
    if not binary:
        raise RequestError("codex_unavailable")
    with tempfile.TemporaryDirectory(prefix="afr-codex-") as temporary:
        base = Path(temporary)
        schema_file = base / "schema.json"
        schema_file.write_text(json.dumps(schema))
        output = base / "result.json"
        argv = [binary, "exec", "--model", MODEL, "--ignore-user-config", "--ignore-rules", "--ephemeral",
                "--skip-git-repo-check", "--sandbox", "read-only", "-c", 'approval_policy="never"',
                "-c", "mcp_servers={}", "-c", 'web_search="disabled"', "-c", 'model_provider="openai"',
                "-c", 'model_reasoning_effort="medium"']
        for feature in ("shell_tool", "unified_exec", "multi_agent", "multi_agent_v2", "hooks", "apps",
                        "plugins", "browser_use", "browser_use_external", "in_app_browser",
                        "skill_mcp_dependency_install"):
            argv.extend(["--disable", feature])
        argv.extend(["--json", "--output-schema", str(schema_file), "--output-last-message", str(output), "-"])
        prompt = instructions + "\n" + json.dumps(payload, ensure_ascii=False)
        # 复用本机登录，隔离项目配置、第三方端点和其他凭据。
        environment = {key: os.environ[key] for key in ("HOME", "PATH", "LANG", "LC_ALL",
                       "TMPDIR", "USER", "LOGNAME", "CODEX_HOME") if key in os.environ}
        try:
            proc = subprocess.run(argv, cwd=base, env=environment, input=prompt.encode(),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise RequestError("codex_timeout") from None
        except OSError:
            raise RequestError("codex_unavailable") from None
        if proc.returncode or not output.is_file():
            error = proc.stderr.decode(errors="replace").lower()
            if any(word in error for word in ("401", "unauthorized", "authentication", "log in", "login required")):
                raise RequestError("codex_authentication")
            raise RequestError("codex_failed")
        try:
            result = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise RequestError("invalid_codex_output") from None
        if not isinstance(result, dict):
            raise RequestError("invalid_codex_output")
        return StructuredResult(result, _completed_usage(proc.stdout))
