"""The Google Cloud deploy scripts (deploy/*.sh), tested offline: bash syntax, and a dry run of 01..04 with
stand-in gcloud / bq / gsutil commands that record their arguments (and stdin) instead of calling Google Cloud.

The stand-ins work like the real launchers: a small shell script that starts a native Python with the POSIX
path of a script next to it. Under Git Bash on Windows this puts them on the same argument-conversion path as
the real gcloud: Git Bash rewrites arguments that look like POSIX paths ('/mnt/gcs') when it starts a native
program, and deploy/env.sh must stop that for the gcloud flags but not for the launcher's own script path.

Skipped when no bash is available. On Windows only Git for Windows' bash is used, never System32\\bash.exe
(WSL), which would run the scripts in Linux.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest

REPO = Path(__file__).resolve().parents[1]
DEPLOY = REPO / "deploy"
SCRIPTS = sorted(path.name for path in DEPLOY.glob("*.sh"))

PROJECT = "velox-test"
BUCKET = f"{PROJECT}-velox-p2p"
SA_EMAIL = f"velox-p2p-sim@{PROJECT}.iam.gserviceaccount.com"
IMAGE = f"europe-west1-docker.pkg.dev/{PROJECT}/velox/velox-p2p-sim:latest"
SERVICE_URL = "https://velox-p2p-sim-abc123-ew.a.run.app"

# The flags whose values may carry POSIX paths; deploy/env.sh exempts exactly these from Git Bash's conversion.
EXEMPT_PREFIXES = {"--add-volume=", "--add-volume-mount=", "--set-env-vars=", "--update-env-vars=", "--set-secrets="}

# Everything the scripts read from the environment: cleared, so a developer's shell cannot change the dry run.
DEPLOY_VARS = (
    "PROJECT_ID", "REGION", "VERTEX_LOCATION", "GEMINI_MODEL", "SERVICE", "JOB", "SCHEDULER_JOB", "AR_REPO",
    "IMAGE_TAG", "IMAGE", "SA_NAME", "BUCKET", "MOUNT_PATH", "APP_UID", "APP_USERNAME", "BQ_DATASET", "BQ_LOCATION",
    "BQ_EXPORT", "DB_ON_BUCKET", "UPLOAD_CACHE", "SKIP_BUILD", "LOAD_LOCAL_EXPORT", "SKIP_SERVICE_UPDATE",
    "APP_PASSWORD", "IMAP_PASSWORD", "IMAP2_PASSWORD", "IMAP_USER", "IMAP2_USER", "IMAP_HOST", "IMAP2_HOST",
    "IMAP_FOLDER", "IMAP2_FOLDER", "IMAP_SINCE", "IMAP2_SINCE", "IMAP_ALLOWED_SENDERS", "IMAP2_ALLOWED_SENDERS",
    "INTAKE_SCENARIO", "SCHEDULE", "RETRY_ATTEMPTS", "RETRY_DELAY_S",
    "MSYS_NO_PATHCONV", "MSYS2_ARG_CONV_EXCL", "MSYS2_ENV_CONV_EXCL",
)


def find_bash() -> Optional[str]:
    if os.name != "nt":
        return shutil.which("bash")
    roots: list[Path] = []
    git = shutil.which("git")
    if git:  # <Git>\cmd\git.exe or <Git>\mingw64\bin\git.exe
        roots += list(Path(git).resolve().parents)[:3]
    roots += [Path(os.environ[name]) / "Git" for name in ("ProgramW6432", "ProgramFiles") if os.environ.get(name)]
    if os.environ.get("LOCALAPPDATA"):
        roots.append(Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Git")
    for root in roots:
        bash = root / "bin" / "bash.exe"  # the launcher that puts /usr/bin (dirname, sleep, ...) on PATH
        if bash.is_file():
            return str(bash)
    return None


BASH = find_bash()
needs_bash = pytest.mark.skipif(BASH is None, reason="no bash (on Windows: Git for Windows) to run the scripts")


def clean_env(**overrides: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in DEPLOY_VARS and not k.startswith("CLOUDSDK_")}
    env.update(overrides)
    return env


def bash(command: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run([BASH, "-c", command], cwd=REPO, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=120)


# --------------------------------------------------------------------------------------------
# Stand-ins for gcloud / bq / gsutil
# --------------------------------------------------------------------------------------------

RECORDER = r'''"""Stand-in for gcloud / bq / gsutil (tests/test_deploy.py): records the call, answers like a fake
project.

rules.json: existing (every resource exists), service_url (the Cloud Run service exists), sa_lag (describes
that still fail after the service account is created), fail ({substring of the call: times to fail first}).
"""
import json
import sys
from pathlib import Path

here = Path(__file__).resolve().parent
tool, args = sys.argv[1], sys.argv[2:]
rules = json.loads((here / "rules.json").read_text(encoding="utf-8"))
state_file = here / "state.json"
state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
stdin = sys.stdin.buffer.read().decode("utf-8") if "--data-file=-" in args else None
with open(here / "calls.jsonl", "a", encoding="utf-8", newline="\n") as log:
    log.write(json.dumps({"tool": tool, "args": args, "stdin": stdin}) + "\n")


def finish(code, out=None):
    state_file.write_text(json.dumps(state), encoding="utf-8")
    if out:
        print(out)
    sys.exit(code)


line = " ".join([tool] + args)
for key, times in rules.get("fail", {}).items():
    if key in line and state.get("failed " + key, 0) < times:
        state["failed " + key] = state.get("failed " + key, 0) + 1
        print(f"ERROR: (stub) simulated failure: {key}", file=sys.stderr)
        finish(1)
words = [a for a in args if not a.startswith("-")]
if tool == "gcloud" and words[:3] == ["run", "services", "describe"]:
    if rules.get("service_url"):
        finish(0, rules["service_url"])
    finish(1)
if tool == "gcloud" and words[:3] == ["iam", "service-accounts", "create"]:
    state["sa_created"], state["sa_lag"] = True, rules.get("sa_lag", 0)
    finish(0)
if tool == "gcloud" and words[:3] == ["iam", "service-accounts", "describe"]:
    if rules.get("existing") or (state.get("sa_created") and state["sa_lag"] == 0):
        finish(0)
    if state.get("sa_created"):
        state["sa_lag"] -= 1
    finish(1)
if "describe" in words or (tool == "bq" and "show" in words):
    finish(0 if rules.get("existing") else 1)
finish(0)
'''

LAUNCHER = """#!/bin/sh
# Stand-in for {tool} (tests/test_deploy.py). Like the real launcher: a native Python runs a script next to it,
# named by its POSIX path.
here="$(cd "$(dirname "$0")" && pwd -P)"
exec {python} -S "$here/recorder.py" {tool} "$@"
"""


@dataclass
class Call:
    tool: str
    args: list[str]
    stdin: Optional[str]

    @property
    def words(self) -> list[str]:
        return [a for a in self.args if not a.startswith("-")]

    def flag(self, name: str) -> Optional[str]:
        """The value of --name=VALUE (None if absent)."""
        values = [a.split("=", 1)[1] for a in self.args if a.startswith(f"--{name}=")]
        return values[-1] if values else None


def env_map(value: str) -> dict[str, str]:
    """KEY=VALUE pairs of --set-env-vars / --update-env-vars, honouring gcloud's ^DELIM^ prefix."""
    delimiter = ","
    custom = re.match(r"\^([^^]+)\^", value)
    if custom:
        delimiter, value = custom.group(1), value[custom.end():]
    return dict(item.split("=", 1) for item in value.split(delimiter))


class DryRun:
    """Runs one deploy script with the stand-ins first on PATH and returns what they recorded."""

    def __init__(self, folder: Path):
        self.stubs = folder / "stubs"
        self.stubs.mkdir()
        (self.stubs / "recorder.py").write_text(RECORDER, encoding="utf-8", newline="\n")
        python = shlex.quote(Path(sys.executable).as_posix())
        for tool in ("gcloud", "bq", "gsutil"):
            launcher = self.stubs / tool
            launcher.write_text(LAUNCHER.format(tool=tool, python=python), encoding="utf-8", newline="\n")
            launcher.chmod(0o755)
        self.calls: list[Call] = []

    def env(self, **overrides: str) -> dict[str, str]:
        return clean_env(PATH=str(self.stubs) + os.pathsep + os.environ.get("PATH", ""), **overrides)

    def run(self, script: str, rules: Optional[dict] = None, **env: str) -> subprocess.CompletedProcess:
        (self.stubs / "rules.json").write_text(json.dumps(rules or {}), encoding="utf-8")
        for name in ("calls.jsonl", "state.json"):
            (self.stubs / name).unlink(missing_ok=True)
        done = subprocess.run([BASH, f"deploy/{script}"], cwd=REPO,
                              env=self.env(PROJECT_ID=PROJECT, RETRY_DELAY_S="0", **env), stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
        log = self.stubs / "calls.jsonl"
        lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        self.calls = [Call(**json.loads(line)) for line in lines]
        return done

    def find(self, *words: str, tool: str = "gcloud") -> list[Call]:
        return [c for c in self.calls if c.tool == tool and c.words[:len(words)] == list(words)]

    def one(self, *words: str, tool: str = "gcloud") -> Call:
        found = self.find(*words, tool=tool)
        assert len(found) == 1, f"{tool} {' '.join(words)}: {len(found)} calls in {[c.args for c in self.calls]}"
        return found[0]


@pytest.fixture()
def dry(tmp_path) -> DryRun:
    return DryRun(tmp_path)


def ok(done: subprocess.CompletedProcess) -> None:
    assert done.returncode == 0, f"exit {done.returncode}\n--- stdout\n{done.stdout}\n--- stderr\n{done.stderr}"


# --------------------------------------------------------------------------------------------
# Static checks (no bash needed)
# --------------------------------------------------------------------------------------------


def test_env_sh_exempts_only_the_path_carrying_gcloud_flags_from_git_bash_conversion():
    """Not MSYS_NO_PATHCONV=1 or MSYS2_ARG_CONV_EXCL='*': those also stop the conversion of the launcher's own
    script path, which a gcloud shell launcher can need under Git Bash."""
    lines = [line.strip() for line in (DEPLOY / "env.sh").read_text(encoding="utf-8").splitlines()]
    code = [line for line in lines if line and not line.startswith("#")]
    (excl,) = [line for line in code if line.startswith("export MSYS2_ARG_CONV_EXCL=")]
    assert set(excl.split("=", 1)[1].strip("\"'").split(";")) == EXEMPT_PREFIXES
    assert not any("MSYS_NO_PATHCONV" in line for line in code)


@pytest.mark.parametrize("script", SCRIPTS)
def test_deploy_scripts_are_lf_only_bash(script):
    raw = (DEPLOY / script).read_bytes()
    assert b"\r" not in raw  # a CRLF script fails in bash ($'\r': command not found)
    assert raw.startswith(b"#!/usr/bin/env bash\n")
    if script != "env.sh":
        text = raw.decode("utf-8")
        assert "set -euo pipefail" in text and 'source "$(dirname "$0")/env.sh"' in text


def test_every_path_carrying_flag_of_the_scripts_is_exempt():
    """A new --flag=... whose value starts a path with '/' must be added to MSYS2_ARG_CONV_EXCL in env.sh."""
    checked = []
    for script in SCRIPTS:
        for flag, value in re.findall(r"(--[a-z-]+=)\"?([^\s\"]*)", (DEPLOY / script).read_text(encoding="utf-8")):
            if re.search(r"(^|[=,])/|\$\{MOUNT_PATH\}|DATABASE_URL", value):
                assert flag in EXEMPT_PREFIXES, f"{script}: {flag}{value}"
                checked.append(flag)
    assert "--add-volume-mount=" in checked  # the pattern still finds the known case


# --------------------------------------------------------------------------------------------
# bash -n and env.sh
# --------------------------------------------------------------------------------------------


@needs_bash
@pytest.mark.parametrize("script", SCRIPTS)
def test_bash_syntax(script):
    done = subprocess.run([BASH, "-n", f"deploy/{script}"], cwd=REPO, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr


@needs_bash
def test_env_sh_exports_its_settings_and_needs_a_project():
    done = bash('source deploy/env.sh && env', clean_env(PROJECT_ID=PROJECT))
    ok(done)
    exported = dict(line.split("=", 1) for line in done.stdout.splitlines() if "=" in line)
    assert set(exported["MSYS2_ARG_CONV_EXCL"].split(";")) == EXEMPT_PREFIXES
    assert exported["CLOUDSDK_CORE_PROJECT"] == PROJECT
    assert "MSYS_NO_PATHCONV" not in exported

    missing = bash("source deploy/env.sh", clean_env())
    assert missing.returncode != 0 and "set PROJECT_ID" in missing.stderr


@needs_bash
def test_the_stand_in_is_exposed_to_git_bash_path_conversion(dry):
    """Guards the harness itself: under Git Bash, without env.sh the stand-in receives converted paths, and a
    global MSYS_NO_PATHCONV=1 breaks its launcher (as it can break gcloud's)."""
    if not bash("uname -s", clean_env()).stdout.startswith(("MINGW", "MSYS", "CYGWIN")):
        pytest.skip("no path conversion outside Git Bash / MSYS2")
    (dry.stubs / "rules.json").write_text("{}", encoding="utf-8")
    call = "gcloud --add-volume-mount=volume=data,mount-path=/mnt/gcs"

    plain = bash(call, dry.env())
    ok(plain)
    log = (dry.stubs / "calls.jsonl").read_text(encoding="utf-8")
    (recorded,) = [json.loads(line)["args"] for line in log.splitlines()]
    assert recorded != ["--add-volume-mount=volume=data,mount-path=/mnt/gcs"]  # rewritten to a Windows path

    assert bash(call, dry.env(MSYS_NO_PATHCONV="1")).returncode != 0  # the launcher's script path is not converted


# --------------------------------------------------------------------------------------------
# 01_setup.sh
# --------------------------------------------------------------------------------------------

SETUP_SECRETS = {"APP_PASSWORD": "app pass 1", "IMAP_PASSWORD": "imap-secret", "IMAP2_PASSWORD": ""}


@needs_bash
def test_setup_on_a_new_project_waits_for_the_service_account_and_retries_bindings(dry):
    done = dry.run("01_setup.sh", {"sa_lag": 2, "fail": {"add-iam-policy-binding": 2}}, **SETUP_SECRETS)
    ok(done)
    apis = dry.one("services", "enable").words[2:]
    assert {"run.googleapis.com", "aiplatform.googleapis.com", "secretmanager.googleapis.com",
            "cloudscheduler.googleapis.com", "artifactregistry.googleapis.com", "bigquery.googleapis.com"} <= set(apis)

    bucket = dry.one("storage", "buckets", "create")
    assert bucket.words[3] == f"gs://{BUCKET}" and bucket.flag("location") == "europe-west1"
    assert {"--uniform-bucket-level-access", "--public-access-prevention"} <= set(bucket.args)

    # The new account is "not found" twice after its creation: 01 waits for it (1 check + 3 after the create).
    assert dry.one("iam", "service-accounts", "create").words[3] == "velox-p2p-sim"
    assert len(dry.find("iam", "service-accounts", "describe")) == 4

    # The first binding fails twice and is retried; every role is granted to the service account.
    bindings = [c for c in dry.calls if "add-iam-policy-binding" in c.words]
    assert all(c.flag("member") == f"serviceAccount:{SA_EMAIL}" for c in bindings)
    assert [c.flag("role") for c in bindings[:3]] == ["roles/aiplatform.user"] * 3
    project_roles = {c.flag("role") for c in dry.find("projects", "add-iam-policy-binding")}
    assert project_roles == {"roles/aiplatform.user", "roles/bigquery.dataEditor", "roles/bigquery.jobUser"}
    assert dry.one("storage", "buckets", "add-iam-policy-binding").flag("role") == "roles/storage.objectAdmin"
    assert "retrying in 0 s" in done.stderr

    # Secrets: on stdin without a trailing newline, never on a command line; an empty IMAP2_PASSWORD is skipped.
    versions = {c.words[3]: c.stdin for c in dry.find("secrets", "versions", "add")}
    assert versions == {"APP_PASSWORD": "app pass 1", "IMAP_PASSWORD": "imap-secret"}
    assert all(c.flag("data-file") == "-" for c in dry.find("secrets", "versions", "add"))
    readers = {c.words[2]: c.flag("role") for c in dry.find("secrets", "add-iam-policy-binding")}
    assert readers == {"APP_PASSWORD": "roles/secretmanager.secretAccessor",
                       "IMAP_PASSWORD": "roles/secretmanager.secretAccessor"}
    assert not any(secret in arg for c in dry.calls for arg in c.args for secret in ("app pass 1", "imap-secret"))
    assert "no second (store) mailbox" in done.stdout
    assert dry.one("artifacts", "repositories", "create").flag("repository-format") == "docker"


@needs_bash
def test_setup_gives_up_when_a_binding_keeps_failing(dry):
    done = dry.run("01_setup.sh", {"existing": True, "fail": {"roles/aiplatform.user": 99}}, RETRY_ATTEMPTS="3",
                   **SETUP_SECRETS)
    assert done.returncode != 0
    assert len(dry.find("projects", "add-iam-policy-binding")) == 3  # three attempts, then set -e stops the script
    assert "giving up after 3 attempts" in done.stderr
    assert not dry.find("artifacts") and not dry.find("secrets")


@needs_bash
def test_setup_on_an_existing_project_creates_nothing(dry):
    ok(dry.run("01_setup.sh", {"existing": True}, **dict(SETUP_SECRETS, IMAP2_PASSWORD="store-secret")))
    assert not [c for c in dry.calls if "create" in c.words]
    assert len(dry.find("iam", "service-accounts", "describe")) == 1  # no wait: the account was already there
    versions = {c.words[3]: c.stdin for c in dry.find("secrets", "versions", "add")}
    assert versions["IMAP2_PASSWORD"] == "store-secret"  # re-running adds a new version of each secret


# --------------------------------------------------------------------------------------------
# 02_deploy_app.sh
# --------------------------------------------------------------------------------------------


@needs_bash
def test_deploy_app_builds_and_deploys_with_unconverted_mount_paths(dry):
    done = dry.run("02_deploy_app.sh", {"service_url": SERVICE_URL}, UPLOAD_CACHE="1")
    ok(done)
    build = dry.one("builds", "submit")
    assert build.flag("tag") == IMAGE and build.flag("ignore-file") == "deploy/.gcloudignore"
    assert build.words[-1] == "."

    cache = REPO / "data" / "cache"
    uploads = dry.find("storage", "rsync")
    if cache.is_dir() and any(cache.iterdir()):
        assert [c.words[2:4] for c in uploads] == [["data/cache", f"gs://{BUCKET}/cache"]]
    else:
        assert not uploads and "data/cache is empty" in done.stderr

    deploy = dry.one("run", "deploy")
    assert deploy.words[2] == "velox-p2p-sim"
    assert deploy.flag("timeout") == "900"  # loading a set may make several model calls in one request
    assert deploy.flag("image") == IMAGE and deploy.flag("service-account") == SA_EMAIL
    assert deploy.flag("execution-environment") == "gen2"
    assert (deploy.flag("min-instances"), deploy.flag("max-instances")) == ("1", "1")
    assert deploy.flag("add-volume") == (f"name=data,type=cloud-storage,bucket={BUCKET},"
                                         f"mount-options=uid=10001;gid=10001")
    assert deploy.flag("add-volume-mount") == "volume=data,mount-path=/mnt/gcs"  # absolute, as typed
    assert deploy.flag("set-secrets") == "APP_PASSWORD=APP_PASSWORD:latest"
    assert "--allow-unauthenticated" in deploy.args

    # --update-env-vars, not --set-env-vars: a redeploy keeps the variables it does not set (BQ_EXPORT from 04).
    assert deploy.flag("set-env-vars") is None
    env = env_map(deploy.flag("update-env-vars"))
    assert env["INBOUND_DIR"] == "/mnt/gcs/inbound" and env["CACHE_DIR"] == "/mnt/gcs/cache"
    assert env["EXPORT_DIR"] == "/mnt/gcs/export" and env["DATABASE_URL"] == "sqlite:////tmp/velox.db"
    assert (env["GEMINI_BACKEND"], env["GOOGLE_CLOUD_PROJECT"], env["EXTRACTOR"]) == ("vertex", PROJECT, "gemini")
    assert (env["BQ_PROJECT"], env["BQ_DATASET"]) == (PROJECT, "velox_p2p")
    assert "BQ_EXPORT" not in env and "BQ_EXPORT not set" in done.stdout
    assert not any("KEY" in name for name in env)  # Vertex AI uses the service account: no API key
    assert SERVICE_URL in done.stdout


@needs_bash
def test_deploy_app_options(dry):
    ok(dry.run("02_deploy_app.sh", {"service_url": SERVICE_URL}, DB_ON_BUCKET="1", BQ_EXPORT="1", SKIP_BUILD="1"))
    assert not dry.find("builds") and not dry.find("storage", "rsync")
    env = env_map(dry.one("run", "deploy").flag("update-env-vars"))
    assert env["DATABASE_URL"] == "sqlite:////mnt/gcs/velox.db" and env["BQ_EXPORT"] == "1"


@needs_bash
def test_deploy_app_needs_the_generated_documents(dry, tmp_path):
    """Without data/invoices_v2 the script stops before any gcloud call (checked on a copy of deploy/)."""
    repo = tmp_path / "repo"
    shutil.copytree(DEPLOY, repo / "deploy")
    (repo / "data").mkdir()
    done = subprocess.run([BASH, "deploy/02_deploy_app.sh"], cwd=repo, env=dry.env(PROJECT_ID=PROJECT),
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120)
    assert done.returncode != 0 and "make pdfs" in done.stderr
    assert not (dry.stubs / "calls.jsonl").exists()


# --------------------------------------------------------------------------------------------
# 03_deploy_intake_job.sh
# --------------------------------------------------------------------------------------------


@needs_bash
def test_intake_job_and_scheduler(dry):
    ok(dry.run("03_deploy_intake_job.sh", {"service_url": SERVICE_URL}, IMAP_USER="ap@example.com",
               IMAP2_USER="store@example.com", IMAP_SINCE="2026-09-25",
               IMAP_ALLOWED_SENDERS="you@example.com,@velox.com"))
    job = dry.one("run", "jobs", "deploy")
    assert job.words[3] == "velox-intake" and job.flag("image") == IMAGE
    assert (job.flag("command"), job.flag("args")) == ("python", "-m,app.intake_imap,--once")
    assert (job.flag("max-retries"), job.flag("task-timeout")) == ("0", "600s")
    env = env_map(job.flag("set-env-vars"))
    assert env["INTAKE_WEBHOOK_URL"] == f"{SERVICE_URL}/intake/webhook"
    assert (env["IMAP_USER"], env["IMAP_CHANNEL"]) == ("ap@example.com", "ap_mailbox")
    assert (env["IMAP2_USER"], env["IMAP2_CHANNEL"]) == ("store@example.com", "store_mailbox")
    assert env["IMAP_SINCE"] == "2026-09-25"
    assert env["IMAP_ALLOWED_SENDERS"] == "you@example.com,@velox.com"  # the commas survive (^|^ delimiter)
    assert "IMAP2_SINCE" not in env and "IMAP2_ALLOWED_SENDERS" not in env  # only what is set is passed
    assert set(job.flag("set-secrets").split(",")) == {"APP_PASSWORD=APP_PASSWORD:latest",
                                                        "IMAP_PASSWORD=IMAP_PASSWORD:latest",
                                                        "IMAP2_PASSWORD=IMAP2_PASSWORD:latest"}

    invoker = dry.one("run", "jobs", "add-iam-policy-binding")
    assert (invoker.flag("member"), invoker.flag("role")) == (f"serviceAccount:{SA_EMAIL}", "roles/run.invoker")

    schedule = dry.one("scheduler", "jobs", "create", "http")
    assert schedule.words[4] == "velox-intake-every-minute"
    assert schedule.flag("schedule") == "* * * * *" and schedule.flag("time-zone") == "Etc/UTC"
    assert schedule.flag("uri") == (f"https://europe-west1-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/"
                                    f"{PROJECT}/jobs/velox-intake:run")
    assert schedule.flag("oauth-service-account-email") == SA_EMAIL


@needs_bash
def test_intake_job_without_the_second_mailbox_updates_an_existing_schedule(dry):
    ok(dry.run("03_deploy_intake_job.sh", {"service_url": SERVICE_URL, "existing": True}, IMAP_USER="ap@example.com"))
    env = env_map(dry.one("run", "jobs", "deploy").flag("set-env-vars"))
    assert not any(name.startswith("IMAP2_") for name in env)
    assert "IMAP_SINCE" not in env and "IMAP_ALLOWED_SENDERS" not in env
    assert dry.one("scheduler", "jobs", "update", "http") and not dry.find("scheduler", "jobs", "create")


@needs_bash
@pytest.mark.parametrize("rules, env, message", [
    ({"service_url": SERVICE_URL}, {}, "set IMAP_USER"),
    ({}, {"IMAP_USER": "ap@example.com"}, None),  # the service is not deployed yet
])
def test_intake_job_stops_before_deploying(dry, rules, env, message):
    done = dry.run("03_deploy_intake_job.sh", rules, **env)
    assert done.returncode != 0
    assert not dry.find("run", "jobs") and not dry.find("scheduler")
    if message:
        assert message in done.stderr


# --------------------------------------------------------------------------------------------
# 04_bigquery.sh
# --------------------------------------------------------------------------------------------


@needs_bash
def test_bigquery_dataset_and_export_switch(dry):
    ok(dry.run("04_bigquery.sh"))
    dry.one("show", tool="bq")
    create = dry.one("mk", tool="bq")
    assert create.words[-1] == f"{PROJECT}:velox_p2p" and "--dataset" in create.args
    assert create.flag("location") == "EU" and create.flag("project_id") == PROJECT
    update = dry.one("run", "services", "update")
    assert update.words[3] == "velox-p2p-sim"
    assert env_map(update.flag("update-env-vars")) == {
        "BQ_EXPORT": "1", "BQ_PROJECT": PROJECT, "BQ_DATASET": "velox_p2p"}


@needs_bash
def test_bigquery_on_an_existing_dataset_without_the_service_update(dry):
    ok(dry.run("04_bigquery.sh", {"existing": True}, SKIP_SERVICE_UPDATE="1"))
    assert [c.words[0] for c in dry.calls] == ["show"]  # bq show only: no mk, no gcloud
