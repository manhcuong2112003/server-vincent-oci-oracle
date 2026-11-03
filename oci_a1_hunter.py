#!/usr/bin/env python3
"""Retry one existing OCI Resource Manager stack, with checked plans and email.

Python 3.8+ standard library + an authenticated OCI CLI. No passwords in this file.
Run `python3 oci_a1_hunter.py --help`. Nothing happens on import.
"""
import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile

STACK = "ocid1.ormstack.oc1.ap-singapore-1.amaaaaaagpymwhaatowy44ouukibise6bcbe5zgjtkau2bkxk5olkstupzua"
EMAIL = "manhcuong2112003@gmail.com"
REGION = "ap-singapore-1"
ACTIVE = {"ACCEPTED", "IN_PROGRESS", "CANCELING", "CANCELLING"}
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class Stop(RuntimeError):
    pass


class RateLimited(Stop):
    """Oracle explicitly rejected a request with HTTP 429; defer the next tick."""
    pass


def cli_error_payload(text):
    """Read a structured OCI error even when CLI warnings precede its JSON."""
    decoder = json.JSONDecoder()
    for match in re.finditer(r"(?m)^\s*\{", text):
        try:
            payload, _ = decoder.raw_decode(text[match.start():].lstrip())
        except ValueError:
            continue
        if isinstance(payload, dict) and "status" in payload and "code" in payload:
            return payload
    return {}


def log(message):
    print("[%s] %s" % (dt.datetime.now().astimezone().isoformat(timespec="seconds"), message), flush=True)


def rows(response):
    data = response.get("data", [])
    return data.get("items", []) if isinstance(data, dict) else data


def capacity_only(messages):
    """A job with any other recognized error must not be blindly re-applied."""
    text = ANSI.sub("", "\n".join(messages)).lower()
    capacity = re.compile(r"out of (?:host )?capacity")
    if not capacity.search(text):
        return False
    forbidden = ("limitexceeded", "limit exceeded", "quota exceeded", "notauthorized",
                 "not authorized", "invalidparameter", "invalid parameter", "permission denied",
                 "authentication failed", "error acquiring the state lock", "authorization failed")
    if any(word in text for word in forbidden):
        return False
    # Terraform may report several independent resource errors in the same job.
    blocks = re.split(r"\berror:\s*", text)[1:]
    return all(capacity.search(block) for block in blocks)


def validate_plan(plan):
    """Allow a single new 2-OCPU/12-GB A1 VM; reject other cloud mutations."""
    targets = []
    for change in plan.get("resource_changes", []):
        if change.get("mode") == "data":
            continue
        resource_type = change.get("type", "")
        action = change.get("change", {}).get("actions", [])
        if resource_type == "oci_core_instance":
            after = change.get("change", {}).get("after") or {}
            shape = after.get("shape")
            config = after.get("shape_config") or []
            if len(config) != 1:
                raise Stop("Plan khong xac dinh duoc CPU/RAM. Khong Apply.")
            if (shape != "VM.Standard.A1.Flex" or config[0].get("ocpus") != 2
                    or config[0].get("memory_in_gbs") != 12):
                raise Stop("Plan khong dung A1 2 OCPU / 12 GB. Khong Apply.")
            source = after.get("source_details") or []
            if len(source) != 1:
                raise Stop("Plan khong xac dinh duoc boot volume.")
            size = source[0].get("boot_volume_size_in_gbs")
            vpu = source[0].get("boot_volume_vpus_per_gb")
            if size is not None and not 0 < float(size) <= 200:
                raise Stop("Boot volume vuot 200 GB hoac khong hop le.")
            if vpu is not None and float(vpu) > 10:
                raise Stop("Boot volume performance vuot 10 VPU/GB.")
            if action != ["create"]:
                raise Stop("Instance khong phai thao tac CREATE moi. Hay kiem tra Console.")
            targets.append(change.get("address", "instance"))
        elif action not in (["no-op"], ["read"]):
            raise Stop("Plan co thay doi tai nguyen khac: %s %s. Khong Apply."
                       % (change.get("address", resource_type), action))
    if len(targets) != 1:
        raise Stop("Plan phai tao dung 1 may A1; thay %s may." % len(targets))
    return targets[0]


class API:
    def __init__(self, region, profile=None):
        self.base = [shutil.which("oci") or "oci", "--region", region, "--output", "json",
                     "--max-retries", "0", "--connection-timeout", "10", "--read-timeout", "60"]
        if profile:
            self.base += ["--profile", profile]

    def call(self, *args, write=False, file_output=False):
        # Read calls can be retried. Mutations are never retried after an ambiguous response.
        for attempt in range(4 if not write else 1):
            try:
                result = subprocess.run(self.base + list(args), capture_output=True, text=True,
                                        encoding="utf-8", errors="replace", timeout=180)
            except (subprocess.TimeoutExpired, OSError) as exc:
                raise Stop("OCI CLI khong tra loi: %s" % exc) from exc
            if result.returncode == 0:
                if file_output:
                    return None
                # OCI CLI emits no stdout for a successful list with zero results.
                # Only list reads may use this representation; missing get/write
                # responses must still stop rather than hide an unknown outcome.
                if not result.stdout.strip() and not write and args[2:3] == ("list",):
                    return {"data": []}
                try:
                    return json.loads(result.stdout)
                except ValueError as exc:
                    command = " ".join(args[:3])
                    raise Stop("OCI CLI tra ve du lieu JSON khong hop le (%s)." % command) from exc
            error_output = result.stderr or result.stdout
            payload = cli_error_payload(error_output)
            rate_limited = (payload.get("status") == 429
                            and payload.get("code") in ("TooManyRequests", "TooManyRequestsPerUser"))
            error = error_output[-1800:]
            transient = re.search(r"429|TooManyRequests|TooManyRequestsPerUser|\b50[0234]\b|throttl", error, re.I)
            if not write and transient and attempt < 3:
                delay = min(60, 5 * 2 ** attempt) + random.randint(0, 5)
                log("API tam thoi ban; cho %s giay." % delay)
                time.sleep(delay)
                continue
            if rate_limited:
                raise RateLimited("Oracle gioi han tan suat (HTTP 429 TooManyRequests); cho luot sau.")
            raise Stop("OCI CLI error: " + error)


class Hunter:
    def __init__(self, args, api):
        self.args, self.api = args, api
        self.directory = Path(args.state_dir)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "state.json"
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        if self.state and (self.state.get("stack") != args.stack_id or self.state.get("region") != args.region):
            raise Stop("Thu muc state thuoc stack/region khac. Dung --state-dir khac.")
        self.state.update(stack=args.stack_id, region=args.region)

    def save(self):
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state, indent=2, ensure_ascii=False), encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)

    def stack(self):
        return self.api.call("resource-manager", "stack", "get", "--stack-id", self.args.stack_id)["data"]

    def jobs(self):
        jobs = rows(self.api.call("resource-manager", "job", "list", "--stack-id", self.args.stack_id, "--all"))
        return sorted(jobs, key=lambda job: job.get("time-created", ""), reverse=True)

    def job(self, job_id):
        return self.api.call("resource-manager", "job", "get", "--job-id", job_id)["data"]

    def job_url(self, job_id):
        return "https://cloud.oracle.com/resourcemanager/jobs/%s?region=%s" % (job_id, self.args.region)

    def notify(self, title, body, event):
        if event in self.state.get("sent", []):
            return
        self.state["outbox"] = {"title": title, "body": body, "event": event}
        self.save()
        self.flush_mail()

    def flush_mail(self):
        pending = self.state.get("outbox")
        if not pending:
            return
        topic = self.state.get("topic")
        if not topic:
            log("Chua co Notifications topic; chay setup-email truoc.")
            return
        self.api.call("ons", "message", "publish", "--topic-id", topic,
                      "--title", pending["title"], "--body", pending["body"], write=True)
        self.state.setdefault("sent", []).append(pending["event"])
        self.state.pop("outbox", None)
        self.save()
        log("Da gui yeu cau thong bao qua OCI Notifications.")

    def setup_email(self):
        compartment = self.stack()["compartment-id"]
        name = "vincent-a1-alerts-" + self.args.stack_id[-8:]
        topics = rows(self.api.call("ons", "topic", "list", "--compartment-id", compartment, "--all"))
        found = next((topic for topic in topics if topic.get("name") == name), None)
        if found is None:
            found = self.api.call("ons", "topic", "create", "--compartment-id", compartment,
                                  "--name", name, write=True)["data"]
        topic_id = found.get("topic-id") or found.get("id")
        if not topic_id:
            raise Stop("Khong doc duoc OCID cua Notifications topic.")
        self.state.update(topic=topic_id, email=self.args.email, compartment=compartment)
        self.save()
        subscriptions = rows(self.api.call("ons", "subscription", "list", "--compartment-id", compartment, "--all"))
        found = next((item for item in subscriptions if item.get("topic-id") == topic_id
                      and item.get("protocol") == "EMAIL" and item.get("endpoint", "").lower() == self.args.email.lower()
                      and item.get("lifecycle-state") not in ("DELETED", "DELETING")), None)
        if found is None:
            found = self.api.call("ons", "subscription", "create", "--compartment-id", compartment,
                                  "--topic-id", topic_id, "--protocol", "EMAIL",
                                  "--subscription-endpoint", self.args.email, write=True)["data"]
        self.state["subscription"] = found.get("id") or found.get("subscription-id")
        self.save()
        log("Email: %s; subscription: %s" % (self.args.email, found.get("lifecycle-state", "PENDING")))
        log("Mo Gmail (ca Spam), bam Confirm subscription trong email Oracle. Sau do chay run.")

    def require_email(self):
        if not self.state.get("subscription"):
            raise Stop("Chay setup-email va xac nhan email truoc khi run.")
        subscription = self.api.call("ons", "subscription", "get",
                                     "--subscription-id", self.state["subscription"])["data"]
        if subscription.get("lifecycle-state") != "ACTIVE":
            raise Stop("Email chua ACTIVE. Mo Gmail va bam Confirm subscription, roi chay lai.")
        self.flush_mail()
        self.notify("OCI A1: email ready", "Script da ket noi Notifications. Chi bao khi thanh cong, dung vi loi, hoac het phien.", "ready")

    def fingerprint(self):
        stack = self.stack()
        if stack.get("lifecycle-state") != "ACTIVE":
            raise Stop("Stack khong o trang thai ACTIVE.")
        source = stack.get("config-source", {})
        if source.get("config-source-type") != "ZIP_UPLOAD":
            raise Stop("Chi ho tro stack ZIP_UPLOAD tao bang Save stack; khong tu chay ma nguon Git thay doi.")
        stable = {key: stack.get(key) for key in ("variables", "terraform-version", "config-source")}
        digest_state = hashlib.sha256(json.dumps(stable, sort_keys=True).encode())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.zip"
            self.api.call("resource-manager", "stack", "get-stack-tf-config", "--stack-id", self.args.stack_id,
                          "--file", str(path), file_output=True)
            with zipfile.ZipFile(path) as archive:
                # Hash contents, not ZIP timestamps, and never extract executable files.
                if sum(item.file_size for item in archive.infolist()) > 32 * 1024 * 1024:
                    raise Stop("Stack config qua lon cho script nay.")
                for name in sorted(archive.namelist()):
                    digest_state.update(name.encode())
                    digest_state.update(hashlib.sha256(archive.read(name)).digest())
        digest = digest_state.hexdigest()
        previous = self.state.get("fingerprint")
        if previous and previous != digest:
            raise Stop("Stack da bi sua sau khi bat dau. Dung de ban review cau hinh moi.")
        self.state["fingerprint"] = digest
        self.save()

    def submit(self, operation, plan_id=None):
        name = "a1-hunt-%s-%s" % (operation.lower(), uuid.uuid4().hex[:16])
        # Persist BEFORE the API call: a lost response must not create duplicate jobs.
        self.state["intent"] = {"name": name, "operation": operation}
        self.state.pop("intent_checks", None)
        self.save()
        command = ["resource-manager", "job", "create-" + operation.lower() + "-job",
                   "--stack-id", self.args.stack_id, "--display-name", name]
        if operation == "APPLY":
            command += ["--execution-plan-strategy", "FROM_PLAN_JOB_ID", "--execution-plan-job-id", plan_id]
        try:
            response = self.api.call(*command, write=True)["data"]
        except RateLimited:
            # Check for an accepted job by name on the next tick before retrying.
            # Keep the checked PLAN in pending when an APPLY was throttled.
            self.state["intent"]["throttled"] = True
            self.save()
            raise
        self.state["pending"] = response["id"]
        self.state.pop("intent", None)
        self.save()
        log("Da tao %s: %s" % (operation, self.job_url(response["id"])))

    def reconcile(self, jobs):
        intent = self.state.get("intent")
        if intent:
            matches = [job for job in jobs if job.get("display-name") == intent["name"]]
            if not matches and intent.get("throttled"):
                self.state.pop("intent", None)
                self.state.pop("intent_checks", None)
                self.save()
                log("Yeu cau truoc bi gioi han tan suat; khong co job da nhan. Tiep tuc tu plan da luu.")
            elif len(matches) != 1:
                raise Stop("Lan gui job truoc chua ro ket qua (%s). Kiem tra Console; khong tu gui job trung."
                           % intent["name"])
            else:
                self.state["pending"] = matches[0]["id"]
                self.state.pop("intent", None)
                self.save()
        active = [job for job in jobs if job.get("lifecycle-state") in ACTIVE]
        if len(active) > 1:
            raise Stop("Stack co nhieu job dang chay. Hay cho chung ket thuc.")
        if active:
            current = active[0]
            if self.state.get("pending") not in (None, current["id"]):
                raise Stop("Phat hien job khac dang chay. Khong Apply song song.")
            if current.get("operation") != "APPLY" and not current.get("display-name", "").startswith("a1-hunt-plan-"):
                raise Stop("Co job khac dang chay tren stack. Cho job do ket thuc roi chay lai.")
            self.state["pending"] = current["id"]
            self.save()

    def plan(self, job_id):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            self.api.call("resource-manager", "job", "get-job-tf-plan", "--job-id", job_id,
                          "--tf-plan-format", "JSON", "--file", str(path), file_output=True)
            plan = json.loads(path.read_text(encoding="utf-8"))
        self.last_plan = plan
        return validate_plan(plan)

    def check_capacity(self, job):
        entries = rows(self.api.call("resource-manager", "job", "get-job-logs", "--job-id", job["id"], "--all"))
        messages = [entry.get("message", "") for entry in entries]
        if not capacity_only(messages):
            raise Stop("Job FAILED khong chi do capacity. Mo log de sua: " + self.job_url(job["id"]))

    def success(self, job):
        # Mark complete first: notification problems must never launch another VM.
        self.state["success"] = job["id"]
        self.state.pop("pending", None)
        self.save()
        body = ("APPLY SUCCEEDED. Oracle da hoan tat job. Mo Console de xem instance, IP va trang thai RUNNING.\n"
                "Script da dung va se khong tao them may.\n" + self.job_url(job["id"]))
        self.notify("OCI A1: APPLY SUCCEEDED", body, "success-" + job["id"])
        log(body)

    def run(self):
        self.require_email()
        if self.state.get("success"):
            self.success({"id": self.state["success"]})
            return 0
        deadline = time.monotonic() + self.args.hours * 3600
        self.fingerprint()
        while time.monotonic() < deadline:
            jobs = self.jobs()
            self.reconcile(jobs)
            pending = self.state.get("pending")
            if pending:
                job = self.job(pending)
                status, operation = job.get("lifecycle-state"), job.get("operation")
                log("%s: %s" % (operation, status))
                if status in ACTIVE:
                    time.sleep(min(30, max(0, deadline - time.monotonic())))
                    continue
                if status == "SUCCEEDED" and operation == "APPLY":
                    self.success(job)
                    return 0
                if status == "SUCCEEDED" and operation == "PLAN":
                    self.fingerprint()
                    target = self.plan(job["id"])
                    log("Plan OK: 1 may A1 2 OCPU / 12 GB, boot <=200 GB: " + target)
                    self.submit("APPLY", job["id"])
                    continue
                if status == "FAILED" and operation == "APPLY":
                    self.check_capacity(job)
                    self.state.pop("pending", None)
                    self.state["next_at"] = time.time() + self.args.interval + random.randint(0, 120)
                    self.save()
                else:
                    raise Stop("Job ket thuc voi %s / %s: %s" % (operation, status, self.job_url(job["id"])))
            else:
                applies = [job for job in jobs if job.get("operation") == "APPLY"]
                if applies:
                    latest = applies[0]
                    if latest.get("lifecycle-state") == "SUCCEEDED":
                        self.success(latest)
                        return 0
                    if latest.get("lifecycle-state") == "FAILED":
                        self.check_capacity(latest)
                    else:
                        raise Stop("Apply gan nhat bi huy/khong ro. Kiem tra Console truoc khi retry.")
            wait = max(0, self.state.get("next_at", 0) - time.time())
            if wait:
                log("Het capacity; con khoang %s giay truoc lan thu tiep." % int(wait))
                while time.time() < self.state["next_at"] and time.monotonic() < deadline:
                    time.sleep(max(0, min(60, self.state["next_at"] - time.time(), deadline - time.monotonic())))
            if time.monotonic() >= deadline:
                break
            self.fingerprint()
            # Re-check active jobs after the wait before starting a new plan.
            if any(job.get("lifecycle-state") in ACTIVE for job in self.jobs()):
                continue
            self.submit("PLAN")
        self.notify("OCI A1: retry session ended", "Da het thoi gian phien. Chay lai lenh run de tiep tuc; stack va tien do van con.",
                    "session-" + uuid.uuid4().hex)
        log("Het phien; chay lai run de tiep tuc. Khong huy job dang chay.")
        return 0


@contextlib.contextmanager
def process_lock(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    handle = (directory / "runner.lock").open("a+b")
    try:
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise Stop("Script khac dang chay trong thu muc state nay.") from exc
    try:
        yield
    finally:
        handle.close()  # OS releases the lock, including after an interrupted process.


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["setup-email", "run", "status"])
    parser.add_argument("--stack-id", default=STACK)
    parser.add_argument("--region", default=REGION)
    parser.add_argument("--email", default=EMAIL)
    parser.add_argument("--profile", help="OCI CLI profile; omit in Cloud Shell")
    parser.add_argument("--state-dir", default=str(Path(__file__).resolve().parent / ".a1-hunter"))
    parser.add_argument("--interval", type=int, default=600, help="Capacity retry seconds (minimum 300); adds 0-120s jitter")
    parser.add_argument("--hours", type=float, default=20, help="Maximum run duration; rerun to resume")
    args = parser.parse_args(argv)
    if args.interval < 300 or args.hours <= 0:
        parser.error("--interval must be >=300; --hours must be >0")
    hunter = None
    try:
        with process_lock(args.state_dir):
            hunter = Hunter(args, API(args.region, args.profile))
            if args.command == "status":
                print(json.dumps(hunter.state, indent=2, ensure_ascii=False))
                return 0
            if not shutil.which("oci"):
                raise Stop("Khong tim thay OCI CLI. Dung Cloud Shell hoac cai OCI CLI va xac thuc truoc.")
            if args.command == "setup-email":
                hunter.setup_email()
                return 0
            return hunter.run()
    except KeyboardInterrupt:
        log("Da dung. Job tren Oracle neu dang chay van tiep tuc; run lai se theo doi job cu.")
        return 130
    except (Stop, ValueError, OSError, zipfile.BadZipFile) as exc:
        message = str(exc)
        log("DUNG: " + message)
        if hunter and hunter.state.get("outbox"):
            log("Thong bao chua gui duoc van nam trong state; run lai de gui tiep.")
        elif hunter and args.command == "run" and hunter.state.get("topic"):
            try:
                hunter.notify("OCI A1: action required", message,
                              "error-" + hashlib.sha256(message.encode()).hexdigest()[:16])
            except (Stop, OSError) as mail_error:
                log("Chua gui duoc email; thong bao duoc luu trong state: " + str(mail_error))
        return 2


if __name__ == "__main__":
    sys.exit(main())
