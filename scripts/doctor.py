#!/usr/bin/env python3
"""`make doctor` -- is anything about to make a test result lie?

WHY THIS EXISTS
---------------
Most lost time on this stack has not been bugs in the agent. It has been
results that measured something other than what we thought:

  * a stale installed fsr_playbooks shadowing the editable checkout, so a green
    suite graded whatever the wheel held;
  * a second framework clone (`_fw_rel2`) drifting from the real one;
  * a dev-patched box reporting the wheel's version while running other code;
  * a box one connector version behind the tree, so a "fixed" bug still fired;
  * workers left on mixed builds after a flaky recycle;
  * an integration (FortiGate, FortiAnalyzer) down, so a sweep row covered
    nothing and read like a widget regression;
  * `ANTHROPIC_API_KEY` exported shell-wide, silently billing the API;
  * a compatibility table in the docs that no longer matched info.json.

Every one of those already had a guard somewhere -- inside `ship`, inside the
sweep, in a memory file -- but only on the path that happened to trip it. This
runs all of them, read-only, in one place, in seconds, and exits non-zero when
a result taken right now could not be trusted.

  FAIL  a result taken now would be wrong or meaningless -- fix before testing
  WARN  real drift worth knowing about; results are still about the code shown
  PASS  checked and fine
  SKIP  could not check (says why)

Usage:
  scripts/doctor.py                 # local + box (ENV= box env file)
  scripts/doctor.py --local         # no box: repos, framework, LLM gateway
  scripts/doctor.py --json          # machine-readable
  scripts/doctor.py --no-ssh        # skip the dev-patch marker probe

Exit: 0 no FAIL · 1 at least one FAIL.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "connector-fsr-agentic-assistant"
HOME = Path.home()
FRAMEWORK = HOME / "PycharmProjects" / "fsr-playbook-framework"
WIDGETS = HOME / "WebstormProjects" / "fsr_all_widgets"
WIDGET = WIDGETS / "widgets-src" / "agenticAssistant"
# No default box: pass --env/--ssh-host (make doctor BOX=<name>) or --local.
# Clones that have diverged from FRAMEWORK before; editing or testing in one of
# them measures stale code.
STALE_FRAMEWORK_CLONES = [HOME / "PycharmProjects" / "_fw_rel2",
                          HOME / "PycharmProjects" / "fw_rel"]
# Integrations the live sweep and the triage helpers lean on. A row whose
# integration is down ENV-SKIPs -- knowing that up front saves a 2-minute run.
SWEEP_INTEGRATIONS = ("fortigate-firewall", "fortinet-fortisiemv2", "fortinet-fortisiem",
                      "fortinet-fortianalyzer", "fortinet-fortiedr")
BOX_SITE = ("/opt/cyops/configs/integrations/packages/integrations_env/conn_pkgs/"
            "lib/python3.12/site-packages/fsr_playbooks")
HEALTH_PROBES = 8  # parallel health_check calls to sample the worker set


@dataclass
class Check:
    area: str
    name: str
    status: str  # PASS | WARN | FAIL | SKIP
    detail: str
    fix: str = ""


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                       timeout=15)
    return r.stdout.strip() if r.returncode == 0 else ""


def _vkey(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", v or ""))


def _read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def _connector_info() -> dict:
    return _read_json(PKG / "info.json")


def _framework_pin() -> str:
    for line in (PKG / "requirements.txt").read_text().splitlines():
        m = re.match(r"^fsr[-_]playbooks==(\S+)", line.strip())
        if m:
            return m.group(1)
    return ""


def _widget_version() -> str:
    return str(_read_json(WIDGET / "widget" / "info.json").get("version") or "")


def _matrix_row() -> dict:
    """The agenticAssistant row of ONBOARDING.md's compatibility matrix."""
    try:
        text = (WIDGETS / "ONBOARDING.md").read_text()
    except OSError:
        return {}
    for line in text.splitlines():
        if line.startswith("| `agenticAssistant`"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            conn = re.search(r"(\d+\.\d+\.\d+)", cells[2] if len(cells) > 2 else "")
            return {"widget": cells[1] if len(cells) > 1 else "",
                    "connector": conn.group(1) if conn else "",
                    "contract": cells[3] if len(cells) > 3 else ""}
    return {}


# ------------------------------------------------------------------ local

def check_repos() -> list[Check]:
    out = []
    repos = {"connector": ROOT, "framework": FRAMEWORK, "widgets": WIDGETS, "widget": WIDGET}
    for label, repo in repos.items():
        if not (repo / ".git").exists():
            out.append(Check("local", f"repo:{label}", "SKIP", f"{repo} is not a git repo"))
            continue
        branch = _git(repo, "branch", "--show-current") or "(detached)"
        dirty = len([ln for ln in _git(repo, "status", "--porcelain").splitlines() if ln])
        counts = _git(repo, "rev-list", "--left-right", "--count", "@{u}...HEAD")
        behind, ahead = (counts.split() + ["?", "?"])[:2] if counts else ("?", "?")
        bits = [f"on {branch}"]
        if dirty:
            bits.append(f"{dirty} uncommitted")
        if counts:
            if ahead != "0":
                bits.append(f"{ahead} unpushed")
            if behind != "0":
                bits.append(f"{behind} behind upstream")
        else:
            bits.append("no upstream")
        status = "PASS" if (not dirty and counts and ahead == "0" and behind == "0") else "WARN"
        fix = ""
        if counts and behind not in ("0", "?"):
            fix = f"git -C {repo} pull --ff-only"
        out.append(Check("local", f"repo:{label}", status, ", ".join(bits), fix))
    return out


def check_stale_clones() -> list[Check]:
    present = [str(p) for p in STALE_FRAMEWORK_CLONES if p.exists()]
    if not present:
        return [Check("local", "framework clones", "PASS", "only fsr-playbook-framework exists")]
    return [Check("local", "framework clones", "WARN",
                  f"diverged clones on disk: {', '.join(present)}",
                  f"work only in {FRAMEWORK}; archive the others when nothing references them")]


def check_dev_env() -> list[Check]:
    """The ONE dev python env (scripts/dev_env.sh): present, built by `make setup`, not stale."""
    r = subprocess.run(["bash", str(ROOT / "scripts" / "dev_env.sh"), "status"],
                       capture_output=True, text=True, timeout=30)
    status = r.stdout.strip() or "missing"
    venv = subprocess.run(["bash", str(ROOT / "scripts" / "dev_env.sh"), "venv"],
                          capture_output=True, text=True, timeout=30).stdout.strip()
    if status == "ok":
        return [Check("local", "dev env", "PASS", f"{venv} matches pyproject.toml + requirements.txt")]
    why = {"missing": "no python env", "unstamped": "built outside `make setup` (contents unknown)",
           "stale": "pyproject.toml/requirements.txt changed since it was built"}.get(status, status)
    fix = "FRESH=1 make setup" if status == "unstamped" else "make setup"
    return [Check("local", "dev env", "FAIL", f"{venv}: {why}", fix)]


def check_framework_resolution() -> list[Check]:
    """Which fsr_playbooks does the connector's venv actually import?"""
    venv = subprocess.run(["bash", str(ROOT / "scripts" / "dev_env.sh"), "venv"],
                          capture_output=True, text=True, timeout=30).stdout.strip()
    py = Path(venv) / "bin" / "python"
    if not py.exists():
        return [Check("local", "framework import", "FAIL", f"no venv at {py}", "make setup")]
    r = subprocess.run([str(py), "-c", "import fsr_playbooks as f; print(f.__file__)"],
                       capture_output=True, text=True, env={**os.environ, "FSRPB_DEV": "1"},
                       timeout=60)
    path = r.stdout.strip()
    if not path:
        return [Check("local", "framework import", "FAIL",
                      f"import failed: {(r.stderr.strip().splitlines() or ['?'])[-1]}")]
    want = expected_framework()
    if os.path.realpath(path).startswith(os.path.realpath(want) + os.sep):
        return [Check("local", "framework import", "PASS", f"venv imports {want}")]
    return [Check("local", "framework import", "FAIL",
                  f"venv imports {path}, not {want} -- local tests grade that copy",
                  "make setup (installs the framework checkout editable)")]


def expected_framework() -> str:
    """The framework local tests must grade: what the one resolver names (an
    agent tree's own checkout, via .fsr_src; else the sibling checkout). The
    make targets put it first on PYTHONPATH, so the check expects the same."""
    r = subprocess.run(["bash", str(ROOT / "scripts" / "framework_dir.sh"), "--soft", str(ROOT)],
                       capture_output=True, text=True, timeout=30)
    return r.stdout.strip() or str(FRAMEWORK)


def check_framework_release() -> list[Check]:
    pin = _framework_pin()
    tag = _git(FRAMEWORK, "describe", "--tags", "--abbrev=0").lstrip("v")
    past = _git(FRAMEWORK, "rev-list", "--count", f"v{tag}..HEAD") if tag else ""
    out = []
    if not tag:
        return [Check("local", "framework pin", "SKIP", "no release tag in the framework repo")]
    if _vkey(tag) > _vkey(pin):
        out.append(Check("local", "framework pin", "WARN",
                         f"framework released {tag} but the connector pins {pin}",
                         "make deploy   (bumps the pin, then ships)"))
    elif _vkey(tag) < _vkey(pin):
        out.append(Check("local", "framework pin", "FAIL",
                         f"connector pins {pin}, newest framework tag is {tag}",
                         f"git -C {FRAMEWORK} fetch --tags"))
    else:
        out.append(Check("local", "framework pin", "PASS", f"pin {pin} = latest tag"))
    if past and past != "0":
        out.append(Check("local", "framework unreleased", "WARN",
                         f"{past} framework commit(s) since v{tag}: the box does not have them",
                         "make deploy FW=next  (or ship-dev to iterate)"))
    return out


def check_release_notes() -> list[Check]:
    info = _connector_info()
    notes = (PKG / "release_notes.md").read_text()
    ver = str(info.get("version") or "")
    head = re.search(r"^## (\d+\.\d+\.\d+)", notes, re.M)
    newest = re.split(r"^## ", notes[head.end():], maxsplit=1, flags=re.M)[0] if head else ""
    if "_TODO" in newest:
        return [Check("local", "release notes", "WARN",
                      f"the {head.group(1)} release note is still a _TODO placeholder",
                      "describe the release before pushing")]
    if head and head.group(1) != ver:
        return [Check("local", "release notes", "WARN",
                      f"info.json is {ver}, newest release note is {head.group(1)}")]
    return [Check("local", "release notes", "PASS", f"{ver} documented")]


def check_identity_and_matrix() -> list[Check]:
    out = []
    svc = WIDGET / "widget" / "widgetAssets" / "js" / "fsrPbAgent.service.js"
    try:
        m = re.search(r"CONNECTOR_NAME\s*=\s*['\"]([^'\"]+)", svc.read_text())
    except OSError:
        m = None
    name = str(_connector_info().get("name") or "")
    if not m:
        out.append(Check("local", "connector identity", "SKIP", f"cannot read {svc}"))
    elif m.group(1) != name:
        out.append(Check("local", "connector identity", "FAIL",
                         f"widget calls {m.group(1)!r}, connector is {name!r}"))
    else:
        out.append(Check("local", "connector identity", "PASS", name))
    row, wv = _matrix_row(), _widget_version()
    if not row:
        out.append(Check("local", "compat matrix", "SKIP", "no agenticAssistant row"))
    elif row["widget"] != wv:
        out.append(Check("local", "compat matrix", "WARN",
                         f"ONBOARDING.md says widget {row['widget']}, info.json is {wv}",
                         "node scripts/sync-compat-matrix.js --connector <tested version>"))
    else:
        out.append(Check("local", "compat matrix", "PASS",
                         f"widget {wv} tested against connector >= {row['connector']}"))
    return out


def check_env_leaks() -> list[Check]:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return [Check("local", "anthropic key", "WARN",
                      "ANTHROPIC_API_KEY is exported in this shell -- Claude Code bills the API",
                      "~/.config/fsr/fix-claude-auth.sh")]
    return [Check("local", "anthropic key", "PASS", "not exported")]


def _frank_env() -> dict:
    env = {}
    fenv = FRAMEWORK / ".env"
    if fenv.exists():
        for line in fenv.read_text().splitlines():
            m = re.match(r'\s*([A-Z_]+)\s*=\s*"?([^"\n#]+)"?', line)
            if m:
                env[m.group(1)] = m.group(2).strip()
    return env


def _frank_creds() -> tuple[str, str]:
    env = _frank_env()
    base = os.environ.get("FRANK_BASE_URL") or env.get("FRANK_BASE_URL", "")
    key = os.environ.get("FRANK_API_KEY") or env.get("FRANK_API_KEY", "")
    return base, key


def check_llm_gateway() -> list[Check]:
    """The box-free LLM tiers default to Frank. Down Frank arrives in the
    harness pre-wrapped as an agent answer, not as a connection error."""
    base, key = _frank_creds()
    if not base or not key:
        return [Check("local", "LLM gateway (Frank)", "WARN",
                      "FRANK_BASE_URL/FRANK_API_KEY not set -- LLM=frank tiers cannot run")]
    url = base.rstrip("/") + ("/models" if base.rstrip("/").endswith("/v1") else "/v1/models")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            ids = {m.get("id") for m in json.loads(r.read()).get("data") or []}
        out = [Check("local", "LLM gateway (Frank)", "PASS",
                     f"{len(ids)} model(s), {time.monotonic() - t0:.1f}s")]
        # The list rotates. A model id that left it 403s as "not allowed for
        # this virtual key", which reads like a broken harness.
        # Every model a local tier will call, not just FRANK_MODEL: on 10-08
        # the key lost LOOP_MODEL's deepseek while doctor said PASS ("tested"
        # is a catalog fact; whether this key may CALL it is a gateway fact).
        wants = [("FRANK_MODEL",
                  os.environ.get("FRANK_MODEL") or _frank_env().get("FRANK_MODEL", ""),
                  "make models; set FRANK_MODEL in the framework .env"),
                 ("LOOP_MODEL", os.environ.get("LOOP_MODEL") or _loop_model_default(),
                  "make models; change LOOP_MODEL's default in scripts/loop.sh")]
        for label, want, fix in wants:
            if want and want not in ids:
                out.append(Check("local", label, "FAIL",
                                 f"{want!r} is not offered to this key by the gateway",
                                 fix))
            elif want:
                out.append(Check("local", label, "PASS", want))
        return out
    except Exception as e:  # noqa: BLE001 - any failure is the finding
        return [Check("local", "LLM gateway (Frank)", "WARN",
                      f"unreachable ({type(e).__name__}: {str(e)[:80]}) -- "
                      "LLM=frank tiers will score an outage as agent answers",
                      "check VPN / `make models`")]


# ---------------------------------------------------------------- models
# Every tier runs a DIFFERENT model, and a result is only as good as the model
# behind it. 2026-09-27: known-answer + the loop smoke ran deepseek, chat-sweep
# ran qwen, the box ran gpt-5.4-mini -- and doctor named only one of them, so
# failures could not be attributed. Each line says which model a tier uses and
# whether model_catalog has measured it (`matrix MODE=gate REPEAT=3`).

def _loop_model_default() -> str:
    """LOOP_MODEL's default, read from loop.sh -- the one place it is set."""
    import re
    m = re.search(r'LOOP_MODEL="\$\{LOOP_MODEL:-([^}"]+)\}"',
                  (ROOT / "scripts" / "loop.sh").read_text())
    return m.group(1) if m else ""


def _model_check(area: str, tier: str, model: str, provider: str | None) -> Check:
    from fsr_playbooks.llm import model_catalog as mc
    if not model:
        return Check(area, f"model: {tier}", "WARN", "no model resolved")
    v = mc.check(model, provider)
    status = {"tested": "PASS", "supported": "WARN",
              "discouraged": "FAIL"}.get(v.level, "WARN")
    label = {"unknown": "UNSCREENED"}.get(v.level, v.level)
    fix = ("" if status == "PASS" else
           f"screen it: {mc.GATE_SLICE} -- or use a tested model; "
           "a failure on it cannot be told apart from model weakness")
    return Check(area, f"model: {tier}", status, f"{model}  [{label}]", fix)


def check_local_models() -> list[Check]:
    frank = os.environ.get("FRANK_MODEL") or _frank_env().get("FRANK_MODEL", "")
    loop = os.environ.get("LOOP_MODEL") or _loop_model_default()
    return [_model_check("local", "sweeps (FRANK_MODEL)", frank, "frank"),
            _model_check("local", "loop smoke (LOOP_MODEL)", loop, "frank")]


def _box_models(c, test_config: str | None) -> list[Check]:
    """The model the live tiers drive (the harness's config) and the one the
    widget uses (the default config) -- often the same, not always."""
    try:
        cfgs = c.connectors.list_configurations(connector="connector-fsr-agentic-assistant")
    except Exception as e:  # noqa: BLE001
        return [Check("box", "model", "WARN", f"could not list configs: {str(e)[:80]}")]
    rows = [x if isinstance(x, dict) else x.model_dump() for x in cfgs]
    out = []
    picks = [("live tests", next((r for r in rows if r.get("name") == test_config), None)),
             ("widget default", next((r for r in rows if r.get("default")), None))]
    for tier, r in picks:
        if r is None:
            out.append(Check("box", f"model: {tier}", "WARN",
                             f"no {'config ' + repr(test_config) if tier == 'live tests' else 'default config'}"))
            continue
        conf = r.get("config") or {}
        prov = conf.get("llm_provider") or "anthropic"
        model = conf.get("openai_model") if prov == "openai" else conf.get("model")
        if prov == "fortiai-proxy":
            out.append(Check("box", f"model: {tier}", "WARN",
                             f"{r.get('name')}: fortiai-proxy -- the model is FortiAI's, not measured here"))
            continue
        chk = _model_check("box", tier, model or "", prov)
        chk.detail = f"{r.get('name')} -> {prov}/{chk.detail}"
        out.append(chk)
    return out


# ------------------------------------------------------------------- box

def _client(env: dict):
    from pyfsr import FortiSOAR
    port = env.get("FSR_PORT")
    return FortiSOAR(env["FSR_BASE_URL"], username=env.get("FSR_USERNAME"),
                     password=env.get("FSR_PASSWORD"),
                     port=int(port) if port else None,
                     verify_ssl=str(env.get("FSR_VERIFY_SSL", "false")).lower() == "true",
                     suppress_insecure_warnings=True, timeout=60)


def _health(c, config: str | None) -> dict:
    r = c.connectors.execute("connector-fsr-agentic-assistant", "health_check",
                             config=config, params={})
    data = getattr(r, "data", None)
    if data is None and isinstance(r, dict):
        data = r.get("data")
    return data if isinstance(data, dict) else {}


def check_box(env: dict, use_ssh: bool, ssh_host: str,
              release: bool = False, required_for: "str | None" = None) -> list[Check]:
    out: list[Check] = []
    try:
        c = _client(env)
    except Exception as e:  # noqa: BLE001
        hint = "set FSR_PORT in the env file" if "405" in str(e) else "is the box up / VPN on?"
        return [Check("box", "login", "FAIL", f"{type(e).__name__}: {str(e)[:120]}", hint)]
    out.append(Check("box", "login", "PASS", "authenticated"))
    config = env.get("FSR_BUILDER_CONFIG_NAME") or None

    # Sample the worker set: every worker must run the same build.
    healths: list[dict] = []
    errors: list[str] = []
    with ThreadPoolExecutor(HEALTH_PROBES) as ex:
        futs = [ex.submit(_health, c, config) for _ in range(HEALTH_PROBES)]
        for f in as_completed(futs):
            try:
                healths.append(f.result())
            except Exception as e:  # noqa: BLE001
                errors.append(str(e)[:100])
    healths = [h for h in healths if h]
    if not healths:
        return out + [Check("box", "connector health", "FAIL",
                            f"health_check failed: {errors[:1]}",
                            "is the connector installed + configured?")]
    h = healths[0]
    builds = {(x.get("connector_version"), x.get("build_id"), x.get("fsr_playbooks_version"))
              for x in healths}
    pids = {x.get("pid") for x in healths}
    if len(builds) > 1:
        out.append(Check("box", "worker builds", "FAIL",
                         f"{len(pids)} workers on {len(builds)} builds: {sorted(builds)}",
                         "make verify   (or re-ship to recycle)"))
    else:
        out.append(Check("box", "worker builds", "PASS",
                         f"{len(pids)} sampled worker(s), one build ({h.get('build_id')})"))

    local_v = str(_connector_info().get("version") or "")
    box_v = str(h.get("connector_version") or "")
    if box_v == local_v:
        out.append(Check("box", "connector version", "PASS", f"box = tree = {box_v}"))
    elif _vkey(box_v) < _vkey(local_v):
        out.append(Check("box", "connector version", "WARN",
                         f"box runs {box_v}, tree is {local_v}: box results are about {box_v}",
                         "make ship-install   (or deploy)"))
    else:
        out.append(Check("box", "connector version", "FAIL",
                         f"box runs {box_v}, NEWER than tree {local_v} -- tree is stale",
                         f"git -C {ROOT} pull"))

    markers = _probe_markers(ssh_host) if use_ssh else None
    out.append(_framework_on_box(
        str(h.get("fsr_playbooks_version") or ""), _framework_pin(),
        str(h.get("fsr_playbooks_install_origin") or ""),
        markers.get("framework") if isinstance(markers, dict) else None, release))

    # The question that matters while iterating: is the box running the code
    # in front of me? build_id hashes the worker's imported .py files; the
    # same hash over the local package answers it exactly.
    sys.path.insert(0, str(ROOT / "scripts"))
    import build_fingerprint
    mine = build_fingerprint.from_dir(PKG)
    if h.get("build_id") == mine:
        out.append(Check("box", "box runs this tree", "PASS", f"connector build {mine}"))
    else:
        out.append(Check("box", "box runs this tree", "WARN",
                         f"box build {h.get('build_id')} != local {mine}: local edits are "
                         "not on the box", "make dev-push"))

    if use_ssh:
        out.append(_dev_patch_marker(ssh_host, release, markers))

    llm_ok = h.get("llm_key_configured") and h.get("llm_reachable")
    out.append(Check("box", "box LLM", "PASS" if llm_ok else "FAIL",
                     f"{h.get('llm_provider')}: key={h.get('llm_key_configured')} "
                     f"reachable={h.get('llm_reachable')} {h.get('llm_error') or ''}".strip(),
                     "" if llm_ok else "fix the Agentic assistant config's LLM key/endpoint"))
    out.extend(_box_models(c, config))

    row = _matrix_row()
    if row.get("contract") and h.get("contract_version") and row["contract"] != h["contract_version"]:
        out.append(Check("box", "wire contract", "FAIL",
                         f"widget tested on contract {row['contract']}, box speaks "
                         f"{h['contract_version']}"))
    elif row.get("connector") and _vkey(box_v) < _vkey(row["connector"]):
        out.append(Check("box", "wire contract", "FAIL",
                         f"widget needs connector >= {row['connector']}, box runs {box_v}"))
    else:
        out.append(Check("box", "wire contract", "PASS",
                         f"contract {h.get('contract_version')}"))

    out.append(_widget_on_box(c))
    out.extend(_integrations(c))
    if required_for:
        out.append(_required_set(c, required_for))
    return out


def _required_set(c, alias: str) -> Check:
    """An experimental box must carry config_soar_demo.REQUIRED (one source), or
    live rows ENV-SKIP and two experimental boxes drift apart."""
    fix = f"make box-prep BOX={alias}"
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        import config_soar_demo as cs
        gaps = cs.required_gaps(c)
    except Exception as e:  # noqa: BLE001 - not knowing is not a pass
        return Check("box", "required connectors", "SKIP", f"{type(e).__name__}: {str(e)[:80]}")
    if not gaps:
        return Check("box", "required connectors", "PASS",
                     f"{len(cs.REQUIRED)} connectors + {', '.join(cs.REQUIRED_MODULES)} module configured, default, healthy")
    return Check("box", "required connectors", "WARN",
                 "; ".join(f"{g['key']}: {g['state'].lower()}" for g in gaps)[:200], fix)


_BUNDLED_FW = re.compile(r"^(?P<pin>\d+\.\d+\.\d+)\.post1\.dev\d+\+g(?P<sha>[0-9a-f]{7,40})$")


def _vt(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


def _framework_main_ref(repo: Path) -> str:
    """`<upstream>/main` of the framework repo: the promoted framework."""
    remote = subprocess.run(["git", "-C", str(repo), "for-each-ref", "--format=%(upstream:remotename)",
                             "refs/heads/main"], capture_output=True, text=True).stdout.strip()
    return f"refs/remotes/{remote}/main" if remote else "refs/heads/main"


def _bundled_framework(fw: str, pin: str, repo: Path) -> "str | None":
    """The framework sha when `fw` is a bundled wheel that carries `pin`, else None.

    The stable package bundles the wheel built from framework main:
    <base>.post1.devN+g<sha>. It carries the pinned release when <base> is at
    least the pin, <sha> is on the framework's remote main, and v<pin> is an
    ancestor of <sha>. Two older rules failed a good install: the base had to
    EQUAL the pin (main after a newer tag builds as <newer>.post1...), and
    <sha> had to be the local checkout's HEAD (the shared checkout is often on
    another session's branch), so live sanity on the stable box reported every
    worker as on the wrong framework while every op on them worked."""
    m = _BUNDLED_FW.match(fw)
    if not m or _vt(m.group("pin")) < _vt(pin):
        return None
    sha = m.group("sha")
    full = subprocess.run(["git", "-C", str(repo), "rev-parse", "-q", "--verify", f"{sha}^{{commit}}"],
                          capture_output=True, text=True).stdout.strip()
    if not full:
        return None
    on_main = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", full,
                              _framework_main_ref(repo)], capture_output=True).returncode == 0
    return sha if on_main and _commit_contains(repo, f"v{pin}", full) else None


def _framework_on_box(fw: str, pin: str, origin: str, patched: "str | None",
                      release: bool, repo: Path = FRAMEWORK) -> Check:
    """Is the framework the workers import the one the connector pins?

    `fw` is the INSTALLED wheel's version: a dev push copies source over the
    wheel and never touches its metadata, so after a release bumps the pin a
    dev-patched box reports the old number while running newer code. When the
    framework carries a dev-patch marker, the question is whether the pushed
    commit contains the pinned release -- answered by the local clone
    (`git merge-base --is-ancestor v<pin> <commit>`). If it does, that is the
    normal dev-loop state (WARN; FAIL for a release/proof run, which needs a
    clean box anyway). Older code, or a commit the clone can't resolve, stays
    a FAIL: that is the stale-build case the version guard exists for.
    """
    if fw != pin and _bundled_framework(fw, pin, repo):
        # The stable package bundles the framework built from main (build.sh
        # FSR_PLAYBOOKS_WHEEL): <pin>.post1.devN+g<sha>, and <sha> must be the
        # framework main HEAD the wheel was built from. Only that shape.
        return Check("box", "framework on box", "PASS",
                     f"{fw} is the bundled framework wheel built from main "
                     f"{_bundled_framework(fw, pin, repo)}")
    if fw != pin:
        if patched and _commit_contains(repo, f"v{pin}", patched):
            return Check("box", "framework on box", "FAIL" if release else "WARN",
                         f"dev-patched @{patched}, which includes v{pin}; the version "
                         f"label {fw} is the installed wheel's, not the running code",
                         "make deploy installs the v" + pin + " wheel")
        why = (f"; dev-patched @{patched} does not include v{pin}" if patched
               else "")
        return Check("box", "framework on box", "FAIL",
                     f"workers import fsr_playbooks {fw}, connector pins {pin}{why}")
    if origin not in ("index", "unknown"):
        return Check("box", "framework on box", "FAIL" if release else "WARN",
                     f"fsr_playbooks {fw} was installed from {origin}, not the index",
                     "make ship-dev ACTION=restore")
    return Check("box", "framework on box", "PASS", f"{fw} from the package index")


def _commit_contains(repo: Path, tag: str, commit: str) -> bool:
    """True when `tag` is an ancestor of (or is) `commit` in `repo`."""
    sha = commit.split()[0] if commit.split() else ""
    if not sha:
        return False
    try:
        r = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", tag, sha],
                           capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def _probe_markers(ssh_host: str) -> "dict[str, str] | str":
    """The box's `_DEV_PATCHED` markers as {"framework"|"connector": commit},
    or a string saying why the probe could not answer."""
    cglob = "/opt/cyops/configs/integrations/connectors/connector-fsr-agentic-assistant_*"
    cmd = (f"for f in {BOX_SITE}/_DEV_PATCHED $(ls -d {cglob} | sort -V | tail -1)/_DEV_PATCHED; "
           "do [ -f \"$f\" ] && echo \"$f: $(grep -m1 commit \"$f\")\"; done; true")
    try:
        r = subprocess.run(["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", ssh_host, cmd],
                           capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return f"ssh {ssh_host} timed out"
    if r.returncode != 0:
        return f"ssh {ssh_host}: {r.stderr.strip()[:80]}"
    out: dict[str, str] = {}
    for ln in r.stdout.splitlines():
        if ln.strip():
            kind = "framework" if "fsr_playbooks" in ln else "connector"
            out[kind] = ln.split("commit :")[-1].strip()
    return out


def _dev_patch_marker(ssh_host: str, release: bool,
                      markers: "dict[str, str] | str | None" = None) -> Check:
    """dev_push.sh / ship_dev.sh leave a `_DEV_PATCHED` marker in each package
    they overwrite. In the dev loop that is the normal state and only worth
    naming; before a release or a proof run it means the box is running code
    no published version contains."""
    if markers is None:
        markers = _probe_markers(ssh_host)
    if isinstance(markers, str):
        return Check("box", "dev push", "SKIP", markers)
    if not markers:
        return Check("box", "dev push", "PASS", "clean: box runs installed releases")
    which = ", ".join(f"{k} @{v}" for k, v in markers.items())
    if release:
        return Check("box", "dev push", "FAIL",
                     f"dev-pushed ({which}) -- not a release; a proof run needs a clean box",
                     "make deploy")
    return Check("box", "dev push", "WARN", f"dev-pushed working tree ({which})")


def _widget_on_box(c) -> Check:
    local = _widget_version()
    try:
        members = c.get("/api/3/widgets", params={"$limit": 500}).get("hydra:member", [])
    except Exception as e:  # noqa: BLE001
        return Check("box", "widget version", "SKIP", str(e)[:80])
    box = next((m.get("version") for m in members if m.get("name") == "agenticAssistant"),
               None)
    if not box:
        return Check("box", "widget version", "FAIL", "agenticAssistant not installed",
                     "make ship-widget WIDGET=agenticAssistant")
    if box == local:
        return Check("box", "widget version", "PASS", f"box = tree = {box}")
    return Check("box", "widget version", "WARN",
                 f"box runs {box}, tree is {local}: the live sweep drives {box}",
                 "make ship-widget WIDGET=agenticAssistant")


def _integrations(c) -> list[Check]:
    try:
        rows = c.connectors.list_configured(refresh=True)
    except Exception as e:  # noqa: BLE001
        return [Check("box", "integrations", "SKIP", str(e)[:80])]
    versions: dict[str, list[str]] = {}
    for x in rows:
        versions.setdefault(x.name, []).append(x.version or "?")
    installed = {n: max(v, key=_vkey) for n, v in versions.items()}
    names = [n for n in SWEEP_INTEGRATIONS if n in installed]
    missing = [n for n in SWEEP_INTEGRATIONS if n not in installed]
    out = []
    ex = ThreadPoolExecutor(len(names) or 1)
    futs = {ex.submit(c.connectors.healthcheck, n, agent_timeout=15): n for n in names}
    deadline = time.monotonic() + 60
    for f, n in futs.items():
        # A probe that never answers is a finding about that integration, not a
        # reason to lose every other box check (TimeoutError used to escape here).
        try:
            r = f.result(timeout=max(0.1, deadline - time.monotonic()))
            st, msg = r.status or "?", " ".join((r.message or "").split())[:80]
        except Exception as e:  # noqa: BLE001
            st, msg = ("timeout", "health check did not answer") if isinstance(e, TimeoutError) else ("error", str(e)[:80])
        ok = st == "Available"
        out.append(Check("integration", f"{n} {installed[n]}", "PASS" if ok else "WARN",
                         st if ok else f"{st} {msg} -- rows that need it will ENV-SKIP"))
    ex.shutdown(wait=False, cancel_futures=True)
    if missing:
        # Informational: fortisiem v1 and v2 are alternatives, so one of them
        # missing is the normal state.
        out.append(Check("integration", "not configured", "PASS", ", ".join(missing)))
    out = sorted(out, key=lambda x: x.name)
    # An upgrade by package import leaves the old version installed and
    # configured beside the new one. Anything that resolves a version-less
    # name -- older pyfsr, a playbook step without a pinned version -- can
    # land on the superseded code (found live: v2 6.0.0 answering for 6.1.1).
    doubled = sorted(f"{n} ({', '.join(sorted(v, key=_vkey))})"
                     for n, v in versions.items() if len(v) > 1)
    if doubled:
        out.append(Check("integration", "side-by-side installs", "WARN",
                         "; ".join(doubled)[:200],
                         "uninstall the superseded versions on the box"))
    out.append(_local_catalog_vs_box(c, names))
    return out


def _local_catalog_vs_box(c, names: list[str]) -> Check:
    """A LOCAL run_op (`--tools live`, record_world.py, the local tiers)
    validates op names against the laptop's reference DB `operations` table,
    not the box. When that table predates a connector upgrade, a real op is
    refused locally as `unknown_operation` -- found live: v2's
    execute_api_request refused because the local table held v2's 58 pre-6.1
    ops while the box ran 67."""
    import sqlite3
    try:
        os.environ.setdefault("FSRPB_DEV", "1")
        from fsr_playbooks._db import default_db_path
        db = default_db_path()
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        local = {n: {r[0] for r in con.execute(
            "select op_name from operations where connector_name=?", (n,))} for n in names}
    except Exception as e:  # noqa: BLE001
        return Check("integration", "local catalog", "SKIP", f"{type(e).__name__}: {e}"[:100])
    gaps = []
    for n in names:
        try:
            box_ops = {o.operation for o in c.connectors.operations(n)}
        except Exception:  # noqa: BLE001 - one unreadable definition is not a verdict
            continue
        missing = box_ops - local[n]
        # With NO local ops, run_op validates against the box's live
        # definition instead, which is right. The refusal needs a PARTIAL
        # table: some of the connector's ops, not the one asked for.
        if missing and local[n]:
            gaps.append(f"{n}: {len(missing)} of {len(box_ops)} box ops unknown locally")
    if not gaps:
        return Check("integration", "local catalog", "PASS",
                     "local op table agrees with the box (or defers to it live)")
    return Check("integration", "local catalog", "WARN",
                 f"{'; '.join(gaps)} -- a local run_op refuses those as unknown_operation",
                 "FSRPB_DB=<a DB warmed from this box> for --tools live / record_world")

def wait_for_build(env: dict, want: str, timeout: float = 120.0) -> int:
    """Poll health_check until every sampled worker reports build `want`.

    After a recycle the workers come back over several seconds, and an early
    probe can land on one still importing. Settled = one full round of
    HEALTH_PROBES answers, all on `want`."""
    c = _client(env)
    config = env.get("FSR_BUILDER_CONFIG_NAME") or None
    deadline = time.monotonic() + timeout
    seen: set = set()
    while True:
        with ThreadPoolExecutor(HEALTH_PROBES) as ex:
            got = []
            for f in [ex.submit(_health, c, config) for _ in range(HEALTH_PROBES)]:
                try:
                    got.append(f.result())
                except Exception:  # noqa: BLE001 - a worker still booting
                    got.append({})
        builds = {g.get("build_id") for g in got}
        seen |= {g.get("pid") for g in got if g}
        if builds == {want}:
            print(f"   {len(seen)} worker(s) on build {want}")
            return 0
        if time.monotonic() >= deadline:
            print(f"!! workers report {sorted(map(str, builds))}, want {want} -- the push did "
                  "not land on every worker", file=sys.stderr)
            return 1
        time.sleep(3)


# --------------------------------------------------------------- registry
# The ONE box registry is pyfsr's instances.toml; scripts/box.sh resolves BOX=<alias>
# through the `pyfsr instances` CLI. These rows say what is wrong with it before a
# push trips over it: no CLI with the commands box.sh needs, an allowlisted alias
# missing from the registry, an env file that is not there, no ssh profile.

def _pyfsr_cli(*args: str, timeout: float = 60.0) -> "subprocess.CompletedProcess[str]":
    return subprocess.run([sys.executable, "-m", "pyfsr.cli", "instances", *args],
                          capture_output=True, text=True, timeout=timeout)


def _box_allowlist() -> list[str]:
    r = subprocess.run(["bash", str(ROOT / "scripts" / "box.sh"), "--list"],
                       capture_output=True, text=True, timeout=10)
    return r.stdout.split()


def _box_role(alias: str) -> str:
    """stable | experimental | unset, from the registry via scripts/box.sh --role."""
    r = subprocess.run(["bash", str(ROOT / "scripts" / "box.sh"), "--role", alias],
                       capture_output=True, text=True, timeout=30)
    return (r.stdout.strip() or "unset") if r.returncode == 0 else "unset"


def check_registry(box: "str | None" = None, need_ssh: bool = True) -> list[Check]:
    fix_cli = "update the pyfsr editable install (pyfsr main has `instances env` / `ssh-target`)"
    try:
        r = _pyfsr_cli("env", "--help", timeout=30)
    except Exception as e:  # noqa: BLE001
        return [Check("registry", "pyfsr CLI", "FAIL", f"{type(e).__name__}: {e}", fix_cli)]
    if r.returncode != 0 or "--env-file-path" not in r.stdout:
        return [Check("registry", "pyfsr CLI", "FAIL",
                      f"{sys.executable} -m pyfsr.cli has no `instances env --env-file-path`", fix_cli)]
    out = [Check("registry", "pyfsr CLI", "PASS", f"{sys.executable} -m pyfsr.cli instances")]
    allow = _box_allowlist()
    r = _pyfsr_cli("check", *allow, "--offline", "--json")
    try:
        rows = {x["instance"]: x for x in json.loads(r.stdout or "[]")}
    except ValueError:
        rows = {}
    for alias in allow:
        x = rows.get(alias)
        if x is None:
            out.append(Check("registry", f"alias {alias}", "FAIL", "on scripts/box.sh's list but not in the pyfsr registry",
                             "add [instances." + alias + "] to ~/.pyfsr/instances.toml, or drop it from FSR_BOX_ALLOW"))
            continue
        if x.get("status") != "ok":
            out.append(Check("registry", f"alias {alias}", "FAIL", f"{x.get('status')}: {x.get('detail', '')}"[:120]))
            continue
        ssh = x.get("ssh") == "yes"
        tgt = alias == box
        status = "PASS" if ssh or not (tgt and need_ssh) else "WARN"
        out.append(Check("registry", f"alias {alias}" + (" (target)" if tgt else ""), status,
                         "resolves" + ("" if ssh else "; no ssh profile: connector/framework pushes refuse it"),
                         "" if ssh else f"add [instances.{alias}.appliance] to ~/.pyfsr/instances.toml"))
    if box:
        if box not in allow:
            out.append(Check("registry", f"target {box}", "FAIL", "not on scripts/box.sh's agent-targetable list"))
        else:
            role = _box_role(box)
            out.append(Check("registry", f"target {box} role", "PASS", f"role: {role}",
                             "" if role != "unset" else f'add role = "stable"|"experimental" to [instances.{box}]'))
            try:
                r = _pyfsr_cli("check", box, "--json", timeout=90)
                x = (json.loads(r.stdout or "[]") or [{}])[0]
                ok = x.get("status") == "ok"
                out.append(Check("registry", f"target {box} answers", "PASS" if ok else "FAIL",
                                 x.get("version") if ok else f"{x.get('status')}: {x.get('detail', '')}"[:120]))
            except Exception as e:  # noqa: BLE001
                out.append(Check("registry", f"target {box} answers", "FAIL", f"{type(e).__name__}: {e}"))
    return out


# ------------------------------------------------------------------- main

def _load_env(path: Path) -> dict:
    from dotenv import dotenv_values
    return {k: v for k, v in dotenv_values(path).items() if v is not None}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--env", default=os.environ.get("BOX_ENV") or None)
    ap.add_argument("--box", default=os.environ.get("BOX") or None,
                    help="registry alias of the target box (scripts/box.sh resolved it)")
    ap.add_argument("--local", action="store_true", help="skip every box check")
    ap.add_argument("--no-ssh", action="store_true", help="skip the dev-patch ssh probe")
    ap.add_argument("--ssh-host", default=os.environ.get("BOX_SSH") or None)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--release", action="store_true",
                    help="proof/release mode: a dev-pushed box is a FAIL, not a WARN")
    ap.add_argument("--expect-build", metavar="ID",
                    help="only wait until every worker reports this build_id (dev_push)")
    a = ap.parse_args()
    if not a.local and not a.env:
        ap.error("no target box: pass --env (make doctor BOX=<name>) or --local. There is no default box.")
    if not a.local and not a.no_ssh and not a.ssh_host:
        if a.box:
            a.no_ssh = True  # the box has no ssh profile: the registry row below says so
        else:
            ap.error("no ssh host: pass --ssh-host (make doctor BOX=<name>), --no-ssh or --local.")
    if a.expect_build:
        return wait_for_build(_load_env(Path(a.env)), a.expect_build)

    t0 = time.monotonic()
    local_fns = [check_dev_env, check_repos, check_stale_clones, check_framework_resolution,
                 check_framework_release, check_release_notes, check_identity_and_matrix,
                 check_env_leaks, check_llm_gateway, check_local_models]
    checks: list[Check] = []
    with ThreadPoolExecutor(len(local_fns) + 2) as ex:
        futs = [ex.submit(fn) for fn in local_fns]
        futs.append(ex.submit(check_registry, None if a.local else a.box))
        if not a.local:
            env_path = Path(a.env)
            if env_path.exists():
                futs.append(ex.submit(check_box, _load_env(env_path), not a.no_ssh, a.ssh_host,
                                      a.release,
                                      a.box if a.box and _box_role(a.box) == "experimental" else None))
            else:
                checks.append(Check("box", "env file", "FAIL", f"missing {env_path}"))
        for f in futs:
            try:
                checks.extend(f.result())
            except Exception as e:  # noqa: BLE001 - a crashed check is itself a finding
                checks.append(Check("doctor", "internal", "FAIL", f"{type(e).__name__}: {e}"))

    order = {"local": 0, "registry": 1, "box": 2, "integration": 3, "doctor": 4}
    checks.sort(key=lambda x: order.get(x.area, 9))
    fails = [x for x in checks if x.status == "FAIL"]
    if a.json:
        print(json.dumps({"ok": not fails, "checks": [asdict(x) for x in checks]}, indent=2))
        return 1 if fails else 0

    color = sys.stdout.isatty()
    paint = {"PASS": "32", "WARN": "33", "FAIL": "31", "SKIP": "90"}
    area = None
    for x in checks:
        if x.area != area:
            area = x.area
            print(f"\n{area}")
        tag = f"\033[{paint[x.status]}m{x.status}\033[0m" if color else x.status
        print(f"  {tag}  {x.name:28s} {x.detail}")
        if x.fix and x.status in ("WARN", "FAIL"):
            print(f"        {'':28s} -> {x.fix}")
    warns = sum(x.status == "WARN" for x in checks)
    print(f"\n[[DOCTOR-{'FAIL' if fails else 'OK'}]] {len(fails)} fail, {warns} warn, "
          f"{len(checks)} checks in {time.monotonic() - t0:.1f}s")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
