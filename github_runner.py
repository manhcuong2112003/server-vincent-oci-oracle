#!/usr/bin/env python3
"""
Optimized Oracle A1 Hunter for GitHub Actions & Local execution.
- Uses native OCI Python SDK (fast, resilient, no subprocess).
- Session-based hunting: retries safely every 40-50s during a session (default 15 minutes).
- Backs off smoothly on HTTP 429 (TooManyRequests).
- Automatically captures public IP upon success and signals GitHub Actions to disable the workflow.
"""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import sys
import time

try:
    import oci
except ImportError:
    print("[ERROR] Thư viện 'oci' chưa được cài đặt. Vui lòng chạy: pip install oci")
    sys.exit(1)

# Cấu hình mặc định
STACK_ID = "ocid1.ormstack.oc1.ap-singapore-1.amaaaaaagpymwhaatowy44ouukibise6bcbe5zgjtkau2bkxk5olkstupzua"
REGION = "ap-singapore-1"

def log(msg: str):
    timestamp = dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {msg}", flush=True)

def output_github(key: str, value: str):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as stream:
                stream.write(f"{key}={value}\n")
        except Exception as e:
            log(f"Không ghi được GITHUB_OUTPUT: {e}")

def get_oci_clients(config_path=None, region=REGION):
    try:
        if config_path and Path(config_path).exists():
            config = oci.config.from_file(config_path)
        else:
            config = oci.config.from_file()
    except Exception:
        # Fallback đọc trực tiếp từ Environment Variables nếu có
        user = os.environ.get("OCI_USER_OCID")
        tenancy = os.environ.get("OCI_TENANCY_OCID")
        fingerprint = os.environ.get("OCI_FINGERPRINT")
        key_content = os.environ.get("OCI_PRIVATE_KEY")
        if user and tenancy and fingerprint and key_content:
            config = {
                "user": user.strip(),
                "tenancy": tenancy.strip(),
                "fingerprint": fingerprint.strip(),
                "key_content": key_content.strip(),
                "region": region,
            }
        else:
            raise RuntimeError("Không tìm thấy cấu hình OCI (~/.oci/config hoặc OCI_* env vars).")

    config["region"] = region
    rm_client = oci.resource_manager.ResourceManagerClient(config)
    compute_client = oci.core.ComputeClient(config)
    network_client = oci.core.VirtualNetworkClient(config)
    identity_client = oci.identity.IdentityClient(config)
    return config, rm_client, compute_client, network_client, identity_client

def check_existing_a1(config, compute_client):
    """Kiểm tra xem tài khoản đã có máy A1 nào đang chạy chưa."""
    instances = compute_client.list_instances(compartment_id=config["tenancy"]).data
    active_a1 = [
        inst for inst in instances
        if inst.shape == "VM.Standard.A1.Flex" and inst.lifecycle_state in ["RUNNING", "PROVISIONING", "STARTING"]
    ]
    return active_a1

def run_session(stack_id: str, region: str, session_minutes: int, retry_interval: int):
    log("=" * 65)
    log("   ORACLE CLOUD A1 ALWAYS FREE HUNTER - PHIÊN TỐI ƯU")
    log("=" * 65)
    log(f" - Stack ID: {stack_id}")
    log(f" - Region: {region}")
    log(f" - Thời gian phiên săn: {session_minutes} phút")
    log(f" - Giãn cách thử lại: {retry_interval} giây/lần")
    log("=" * 65)

    config, rm_client, compute_client, network_client, _ = get_oci_clients(region=region)
    tenancy_id = config["tenancy"]

    # 1. Kiểm tra xem đã có máy A1 tạo từ trước chưa
    existing_a1 = check_existing_a1(config, compute_client)
    if existing_a1:
        inst = existing_a1[0]
        log(f"🎉 Máy chủ A1 đã tồn tại trong tài khoản: {inst.display_name} (State: {inst.lifecycle_state})")
        output_github("terminal", "true")
        output_github("success", "true")
        return 0

    session_start = time.time()
    session_timeout = session_minutes * 60
    attempt = 1

    while True:
        elapsed = time.time() - session_start
        if elapsed >= session_timeout:
            log(f"\n[HẾT GIỜ PHIÊN] Đã chạy xong {session_minutes} phút của phiên hiện tại mà chưa có dung lượng.")
            log("Kết thúc phiên êm ái để đợi lần kích hoạt tiếp theo từ GitHub Actions.")
            output_github("terminal", "false")
            output_github("success", "false")
            return 0

        # Kiểm tra nếu đang có job khác đang chạy dở trên Stack
        try:
            recent_jobs = rm_client.list_jobs(compartment_id=tenancy_id, stack_id=stack_id).data
            active_jobs = [j for j in recent_jobs if j.lifecycle_state in ["ACCEPTED", "IN_PROGRESS"]]
            if active_jobs:
                running_job = active_jobs[0]
                log(f"⏳ Có Job {running_job.display_name} đang chạy, đợi 15s...")
                time.sleep(15)
                continue
        except Exception as e:
            log(f"Cảnh báo kiểm tra job cũ: {e}")

        # Gửi lệnh Apply
        job_name = f"a1-hunt-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}"
        log(f"\n[Lần thử #{attempt} | Phút {int(elapsed // 60)}/{session_minutes}] Gửi lệnh Apply vào Stack...")

        try:
            create_job_details = oci.resource_manager.models.CreateJobDetails(
                stack_id=stack_id,
                display_name=job_name,
                operation="APPLY",
                apply_job_plan_resolution=oci.resource_manager.models.CreateApplyJobPlanResolutionDetails(
                    is_auto_approved=True
                )
            )
            job = rm_client.create_job(create_job_details).data
            job_id = job.id

            # Chờ job hoàn tất
            while True:
                time.sleep(10)
                current_job = rm_client.get_job(job_id).data
                state = current_job.lifecycle_state

                if state == "SUCCEEDED":
                    log("\n" + "=" * 65)
                    log("🎉🎉🎉 CHÚC MỪNG! ĐÃ TẠO THÀNH CÔNG SERVER ARM A1! 🎉🎉🎉")
                    log("=" * 65)

                    # Tìm IP của máy vừa tạo
                    time.sleep(5)
                    active_a1 = check_existing_a1(config, compute_client)
                    for inst in active_a1:
                        log(f" - Tên máy: {inst.display_name}")
                        log(f" - Trạng thái: {inst.lifecycle_state}")
                        log(f" - OCID: {inst.id}")
                        try:
                            vnics = compute_client.list_vnic_attachments(compartment_id=tenancy_id, instance_id=inst.id).data
                            for vnic in vnics:
                                v = network_client.get_vnic(vnic.vnic_id).data
                                log(f" - IP Public: {v.public_ip}")
                                log(f" - IP Private: {v.private_ip}")
                        except Exception as ve:
                            log(f"Không lấy được IP: {ve}")

                    output_github("terminal", "true")
                    output_github("success", "true")
                    return 0

                elif state in ["FAILED", "CANCELED"]:
                    # Đọc log lỗi
                    is_out_of_capacity = False
                    is_rate_limited = False
                    try:
                        logs = rm_client.get_job_logs(job_id).data
                        for entry in logs:
                            msg = entry.message
                            if "Out of host capacity" in msg or "500-InternalError" in msg:
                                is_out_of_capacity = True
                            if "TooManyRequests" in msg or "429" in msg:
                                is_rate_limited = True
                    except Exception:
                        pass

                    if is_out_of_capacity:
                        log("⏳ Oracle báo: Hết chỗ trống tạm thời (Out of host capacity).")
                    elif is_rate_limited:
                        log("⚠️ Oracle cảnh báo tần suất (429). Tự động nghỉ 90s...")
                        time.sleep(90)
                    else:
                        log(f"⚠️ Job kết thúc với trạng thái: {state}")

                    break

        except oci.exceptions.ServiceError as se:
            if se.status == 429:
                log("⚠️ Bị Rate Limit (HTTP 429). Tự động nghỉ 90s...")
                time.sleep(90)
            else:
                log(f"Lỗi OCI Service ({se.status}): {se.message}")
        except Exception as ex:
            log(f"Lỗi ngoại lệ: {ex}")

        attempt += 1
        log(f"Chờ {retry_interval}s trước lượt tiếp theo...")
        try:
            time.sleep(retry_interval)
        except KeyboardInterrupt:
            log("Người dùng bấm dừng.")
            output_github("terminal", "false")
            return 0

def main():
    parser = argparse.ArgumentParser(description="Oracle A1 Capacity Hunter (Optimized)")
    parser.add_argument("mode", nargs="?", default="tick", choices=["tick", "setup", "status"],
                        help="Mode: tick (mặc định săn), status (kiểm tra), setup (kiểm tra kết nối)")
    parser.add_argument("--stack-id", default=STACK_ID, help="Stack OCID")
    parser.add_argument("--region", default=REGION, help="OCI Region")
    parser.add_argument("--session-minutes", type=int, default=15, help="Thời gian mỗi phiên săn (phút)")
    parser.add_argument("--retry-interval", type=int, default=45, help="Khoảng cách giữa các lần thử (giây)")
    parser.add_argument("--min-interval-seconds", type=int, default=0, help="Dành cho tương thích cũ")

    args = parser.parse_args()

    if args.mode == "status":
        config, rm_client, compute_client, _, identity_client = get_oci_clients(region=args.region)
        user = identity_client.get_user(config["user"]).data
        log(f"User: {user.name} ({user.lifecycle_state})")
        existing = check_existing_a1(config, compute_client)
        log(f"Số máy A1 đang chạy: {len(existing)}")
        for i in existing:
            log(f" - {i.display_name}: {i.lifecycle_state}")
        return 0

    if args.mode == "setup":
        config, rm_client, _, _, identity_client = get_oci_clients(region=args.region)
        user = identity_client.get_user(config["user"]).data
        stack = rm_client.get_stack(args.stack_id).data
        log(f"Kết nối thành công! User: {user.name}, Stack: {stack.display_name} ({stack.lifecycle_state})")
        return 0

    # Chế độ mặc định: Săn theo phiên
    return run_session(
        stack_id=args.stack_id,
        region=args.region,
        session_minutes=args.session_minutes,
        retry_interval=args.retry_interval
    )

if __name__ == "__main__":
    sys.exit(main() or 0)
