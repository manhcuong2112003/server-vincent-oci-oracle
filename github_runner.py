#!/usr/bin/env python3
"""A short, resumable deployment tick for GitHub Actions. No daemon or sleep loop."""
import argparse
import json
import os
from pathlib import Path
import tempfile
import time
import uuid

from oci_a1_hunter import API, Hunter, Stop, STACK, REGION, EMAIL, ACTIVE, log, rows


class Busy(Stop):
    pass


class RemoteState:
    """Persist intent before mutation; compare-and-swap protects remote state."""
    def __init__(self, client, namespace, bucket):
        self.client, self.namespace, self.bucket = client, namespace, bucket
        self.etag = None
        self.lease_etag = None
        self.expires = 0

    def get(self, name):
        try:
            result = self.client.get_object(self.namespace, self.bucket, name)
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                return None, None
            raise
        return json.loads(result.data.content.decode("utf-8")), result.headers["etag"]

    def put(self, name, data, etag):
        condition = {"if_match": etag} if etag else {"if_none_match": "*"}
        result = self.client.put_object(self.namespace, self.bucket, name,
                                        json.dumps(data, ensure_ascii=False).encode("utf-8"),
                                        content_type="application/json", **condition)
        return result.headers["etag"]

    def acquire(self):
        previous, etag = self.get("lease.json")
        if previous and previous.get("expires", 0) > time.time():
            raise Busy("Runner khac dang giu lease. Bo qua lan nay.")
        self.expires = time.time() + 900  # Workflow timeout is 5 minutes; lease lasts 15.
        try:
            self.lease_etag = self.put("lease.json", {"owner": uuid.uuid4().hex, "expires": self.expires}, etag)
        except Exception as exc:
            if getattr(exc, "status", None) == 412:
                raise Busy("Runner khac vua lay lease; bo qua.") from exc
            raise

    def load(self):
        data, self.etag = self.get("state.json")
        return data

    def save(self, data):
        if not self.lease_etag or time.time() >= self.expires:
            raise Stop("Lease het han; dung de tranh chay trung.")
        self.etag = self.put("state.json", data, self.etag)

    def release(self):
        if self.lease_etag:
            try:
                self.put("lease.json", {"expires": 0}, self.lease_etag)
            except Exception:
                log("Khong release duoc lease; no se tu het han sau toi da 15 phut.")


def planned_boot(plan):
    instance = next(item for item in plan["resource_changes"] if item.get("type") == "oci_core_instance")
    source = instance["change"]["after"]["source_details"][0]
    return float(source.get("boot_volume_size_in_gbs") or 50)


def audit_budget(api, tenancy, boot_gb):
    """Conservatively count existing A1 and volumes in the same home region."""
    compartments = rows(api.call("iam", "compartment", "list", "--compartment-id", tenancy,
                                 "--compartment-id-in-subtree", "true", "--access-level", "ANY", "--all"))
    ids = [tenancy] + [item["id"] for item in compartments if item.get("lifecycle-state") == "ACTIVE"]
    domains = rows(api.call("iam", "availability-domain", "list", "--compartment-id", tenancy, "--all"))
    used_cpu, used_ram, used_disk = 0.0, 0.0, 0.0
    for compartment in dict.fromkeys(ids):
        instances = rows(api.call("compute", "instance", "list", "--compartment-id", compartment, "--all"))
        for item in instances:
            if item.get("lifecycle-state") == "TERMINATED" or item.get("shape") != "VM.Standard.A1.Flex":
                continue
            config = item.get("shape-config") or {}
            if config.get("ocpus") is None or config.get("memory-in-gbs") is None:
                raise Stop("Khong doc duoc cau hinh A1 dang ton tai; khong Apply them.")
            used_cpu += float(config["ocpus"])
            used_ram += float(config["memory-in-gbs"])
        volumes = list(rows(api.call("bv", "volume", "list", "--compartment-id", compartment, "--all")))
        for domain in domains:
            volumes += rows(api.call("bv", "boot-volume", "list", "--compartment-id", compartment,
                                     "--availability-domain", domain["name"], "--all"))
        for item in volumes:
            if item.get("lifecycle-state") != "TERMINATED":
                if item.get("size-in-gbs") is None:
                    raise Stop("Khong doc duoc dung luong volume; khong Apply.")
                used_disk += float(item["size-in-gbs"])
    if used_cpu + 2 > 2 or used_ram + 12 > 12:
        raise Stop("Tai khoan da co A1 dung %.1f OCPU / %.1f GB. Khong tao them vuot 2/12." % (used_cpu, used_ram))
    if used_disk + boot_gb > 200:
        raise Stop("Da co %.1f GB volume; ke hoach them %.1f GB se vuot tong 200 GB." % (used_disk, boot_gb))
    log("Budget check OK: A1 hien co %.1f/%.1f; disk %.1f + %.1f <=200 GB."
        % (used_cpu, used_ram, used_disk, boot_gb))


def check_home(api, tenancy, region):
    subscriptions = rows(api.call("iam", "region-subscription", "list", "--tenancy-id", tenancy))
    if not any(item.get("region-name") == region and item.get("is-home-region") is True for item in subscriptions):
        raise Stop("Region nay khong phai home region cua tai khoan. Khong tu tao tai nguyen o region khac.")


class ShortHunter(Hunter):
    def __init__(self, args, api, store):
        self.store = store
        saved = store.load()
        if saved:
            Path(args.state_dir, "state.json").write_text(json.dumps(saved), encoding="utf-8")
        super().__init__(args, api)

    def save(self):
        super().save()
        self.store.save(self.state)

    def reconcile(self, jobs):
        super().reconcile(jobs)
        if not self.state.get("intent") and self.state.pop("intent_checks", None) is not None:
            self.save()

    def finish_plan(self, tenancy):
        deadline = time.monotonic() + 60
        while True:
            job = self.job(self.state["pending"])
            status = job.get("lifecycle-state")
            if status not in ACTIVE:
                break
            if time.monotonic() >= deadline:
                log("Plan van dang chay; lan schedule sau tiep tuc.")
                return False
            time.sleep(10)
        if status != "SUCCEEDED" or job.get("operation") != "PLAN":
            raise Stop("Plan chua thanh cong: " + self.job_url(job["id"]))
        self.fingerprint()
        self.plan(job["id"])
        audit_budget(self.api, tenancy, planned_boot(self.last_plan))
        self.submit("APPLY", job["id"])
        return False

    def tick(self, tenancy):
        if self.state.get("stopped"):
            self.flush_mail()
            log("Da dung vi loi; can review truoc khi chay lai: " + self.state["stopped"])
            return True
        if not self.state.get("subscription"):
            log("Chay mode setup truoc de tao email subscription.")
            return False
        subscription = self.api.call("ons", "subscription", "get",
                                     "--subscription-id", self.state["subscription"])["data"]
        if subscription.get("lifecycle-state") != "ACTIVE":
            log("Cho ban bam Confirm subscription trong Gmail; chua gui job moi.")
            return False
        self.require_email()
        if self.state.get("success"):
            self.success({"id": self.state["success"]})
            return True
        self.fingerprint()
        jobs = self.jobs()
        self.reconcile(jobs)
        if self.state.get("pending"):
            job = self.job(self.state["pending"])
            status, operation = job.get("lifecycle-state"), job.get("operation")
            if operation == "PLAN" and (status in ACTIVE or status == "SUCCEEDED"):
                return self.finish_plan(tenancy)
            if status in ACTIVE:
                log("%s dang %s; lan schedule sau se kiem tra tiep." % (operation, status))
                return False
            if status == "SUCCEEDED" and operation == "APPLY":
                self.success(job)
                return True
            if status != "FAILED" or operation != "APPLY":
                raise Stop("Job ket thuc %s/%s: %s" % (operation, status, self.job_url(job["id"])))
            self.check_capacity(job)
            self.state.pop("pending", None)
            self.save()
        else:
            applies = [item for item in jobs if item.get("operation") == "APPLY"]
            if applies:
                latest = applies[0]
                if latest.get("lifecycle-state") == "SUCCEEDED":
                    self.success(latest)
                    return True
                if latest.get("lifecycle-state") != "FAILED":
                    raise Stop("Apply gan nhat khong ro trang thai. Kiem tra Console.")
                self.check_capacity(latest)
        self.submit("PLAN")
        return self.finish_plan(tenancy)


def output_terminal(value):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as stream:
            stream.write("terminal=%s\n" % str(value).lower())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["setup", "tick", "status"])
    parser.add_argument("--stack-id", default=STACK)
    parser.add_argument("--region", default=REGION)
    parser.add_argument("--email", default=EMAIL)
    args = parser.parse_args()
    # Imported only for real execution; pure logic is testable without OCI credentials.
    import oci
    config = oci.config.from_file()
    config["region"] = args.region
    oci.config.validate_config(config)
    client = oci.object_storage.ObjectStorageClient(config, retry_strategy=oci.retry.NoneRetryStrategy(), timeout=(10, 60))
    namespace = client.get_namespace().data
    api = API(args.region)
    check_home(api, config["tenancy"], args.region)
    bucket = "vincent-a1-state-" + args.stack_id[-8:]
    try:
        client.get_bucket(namespace, bucket)
    except oci.exceptions.ServiceError as exc:
        if exc.status != 404 or args.command != "setup":
            raise Stop("Khong doc duoc bucket state; chay setup truoc hoac kiem tra IAM.") from exc
        compartment = api.call("resource-manager", "stack", "get", "--stack-id", args.stack_id)["data"]["compartment-id"]
        client.create_bucket(namespace, oci.object_storage.models.CreateBucketDetails(
            name=bucket, compartment_id=compartment, public_access_type="NoPublicAccess", storage_tier="Standard"))
    store = RemoteState(client, namespace, bucket)
    try:
        store.acquire()
    except Busy as exc:
        log(str(exc))
        return 0
    # Keep local state alive while the error handler persists the stop reason
    # and pending notification to Object Storage.
    with tempfile.TemporaryDirectory() as directory:
        args.state_dir = directory
        hunter = None
        try:
            hunter = ShortHunter(args, api, store)
            if args.command == "setup":
                hunter.setup_email()
                return 0
            if args.command == "status":
                log("Stack: %s; success=%s; pending=%s; stopped=%s" % (
                    args.stack_id, hunter.state.get("success"), hunter.state.get("pending"), hunter.state.get("stopped")))
                return 0
            terminal = hunter.tick(config["tenancy"])
            output_terminal(terminal)
            return 0
        except (Stop, oci.exceptions.ServiceError, ValueError) as exc:
            log("DUNG: " + str(exc))
            if hunter:
                if hunter.state.get("success") or hunter.state.get("outbox"):
                    # Leave delivery resumable. Do not overwrite an unsent success notice.
                    log("Thong bao chua gui xong; giu schedule de thu gui lai.")
                elif hunter.state.get("intent") and hunter.state.get("intent_checks", 0) < 3:
                    hunter.state["intent_checks"] = hunter.state.get("intent_checks", 0) + 1
                    hunter.save()
                    log("Chua ro ket qua gui job; giu schedule de tim lai, khong gui job trung.")
                else:
                    hunter.state["stopped"] = str(exc)
                    hunter.save()
                    hunter.notify("OCI A1: action required", str(exc), "stopped")
                    output_terminal(True)
            return 2
        finally:
            store.release()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (Stop, OSError) as error:
        log("DUNG: " + str(error))
        raise SystemExit(2)
