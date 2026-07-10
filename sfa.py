"""
This is a wizard whos goal is to help with scripting the setup and formatting of pools and vds on an SFA platform
This tool will use EMF so must be installed on a BoO with an EMF binary.

It's only real requirement currently is EMF, python > 3.7 and the InquirerPy package

Author: Peter Hering
!!!!! WORK IN PROGRESS !!!!!!
"""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Any
import json
import argparse
import subprocess
import re

from InquirerPy import inquirer
from InquirerPy.validator import NumberValidator


# ── Variables ────────────────────────────────────────────────────────────────

EMF = "/work/repo/bmlab/lustre/emf-2026062516-cli-x86_64-unknown-linux-gnu-el8/emf"
SFA_API_PASSWORD = "user"

# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class DriveInfo:
    slot: int
    vendor: str
    product_id: str
    drive_type: str
    capacity_tib: float
    serial: str
    pool: int
    health: str
    state: str


@dataclass
class DriveGrouping:
    drive_type: str
    capacity_tib: float
    count: int
    drives: list[DriveInfo] = field(default_factory=list)


@dataclass
class VirtualDisk:
    name: str
    raid_level: str
    drive_count: int
    drive_size_gb: int
    chunk_size_kb: int
    purpose: str
    hot_spare: bool
    vd_capacity_gb: int = field(init=False)

    def __post_init__(self):
        self.vd_capacity_gb = self.drive_count * self.drive_size_gb


@dataclass
class Pool:
    name: str
    tier: str
    disk_type: str = "NVMe"
    virtual_disks: list[VirtualDisk] = field(default_factory=list)


@dataclass
class StorageConfig:
    appliance_name: str
    fs_name: str
    usage: str
    has_mgs: bool
    pools: list[Pool] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── Helpers ────────────────────────────────────────────────────────────────────

RAID_LEVELS = ["RAID 1", "RAID 6"]
CHUNK_SIZES = ["16", "32", "64", "128", "256", "512", "1024"]
POOL_TIERS = ["NVMe", "SSD", "HDD", "Hybrid"]
VD_PURPOSES = ["Data", "Metadata", "Cache", "Log", "Spare Pool", "Mixed"]
SFA_USAGE = ["Data Only", "Metadata Only", "Mixed Use"]

HEADER = "\033[1;36m"  # bold cyan
RESET = "\033[0m"
DIM = "\033[2m"

def parse_args():
    parser = argparse.ArgumentParser(description="Storage Appliance Layout Wizard")
    parser.add_argument("--appliance-name", help="Skip appliance name prompt")
    parser.add_argument("--usage", help="Skip appliance usage prompt")
    parser.add_argument("--fs-name", help="Skip appliance fsname prompt")
    return parser.parse_args()

def section(title: str) -> None:
    print(f"\n{HEADER}{'─' * 50}")
    print(f"  {title}")
    print(f"{'─' * 50}{RESET}\n")


def confirm_or_back(prompt: str) -> bool:
    return inquirer.confirm(message=prompt, default=True).execute()


def resolve_appliance_ip(appliance_name: str) -> str:
    """Resolve appliance hostname to IP by pinging appliance-c0."""
    hostname = f"{appliance_name}-c0"
    try:
        result = subprocess.run(
            ["ping", "-c", "1", hostname],
            capture_output=True,
            text=True,
            timeout=5
        )
        match = re.search(r'\(([0-9.]+)\)', result.stdout)
        if match:
            return match.group(1)
    except Exception as e:
        print(f"{DIM}Warning: Failed to resolve {hostname}: {e}{RESET}")
    return ""


def run_emf_command(appliance_ip: str, emf_resource: str) -> str:
    """Execute an EMF command against the appliance."""
    try:
        result = subprocess.run(
            [EMF, "sfa", emf_resource, "list", "--ips", appliance_ip, "--sfa-api-password", SFA_API_PASSWORD],
            capture_output=True,
            text=True,
            timeout=30
        )
        return result.stdout
    except subprocess.TimeoutExpired:
        return ""
    except Exception as e:
        print(f"{DIM}Warning: Failed to run EMF command: {e}{RESET}")
        return ""


def parse_drive_table(table_output: str) -> list[DriveInfo]:
    """Parse EMF drive table output into DriveInfo objects."""
    drives = []
    lines = table_output.strip().split('\n')

    for line in lines:
        if '|' not in line:
            continue

        # Skip header and separator lines
        if any(x in line for x in ['Source IPs', '========', '--------', 'Idx', 'Pos']):
            continue

        # Look for data lines (should have IP addresses)
        if not any(char.isdigit() for char in line.split('|')[0]):
            continue

        parts = [p.strip() for p in line.split('|')]
        if len(parts) < 17:
            continue

        try:
            capacity_str = parts[8]
            capacity_val = float(capacity_str.replace('TiB', '').replace('GiB', '').strip())
            if 'GiB' in capacity_str:
                capacity_val /= 1024

            pool_str = parts[12]
            pool = -1 if pool_str == "--" else int(pool_str)

            drive = DriveInfo(
                slot=int(parts[3]),
                vendor=parts[4],
                product_id=parts[5],
                drive_type=parts[6],
                capacity_tib=capacity_val,
                serial=parts[11],
                pool=pool,
                health=parts[13],
                state=parts[15]
            )
            drives.append(drive)
        except (ValueError, IndexError):
            continue

    return drives


def group_drives_by_type(drives: list[DriveInfo]) -> dict[str, DriveGrouping]:
    """Group drives by type and capacity."""
    groups = {}
    for drive in drives:
        key = f"{drive.drive_type}_{drive.capacity_tib}"
        if key not in groups:
            groups[key] = DriveGrouping(
                drive_type=drive.drive_type,
                capacity_tib=drive.capacity_tib,
                count=0,
                drives=[]
            )
        groups[key].count += 1
        groups[key].drives.append(drive)

    return groups


def calculate_vd_size_for_metadata(data_pool_size_gb: int) -> int:
    """Calculate metadata VD size as 1% of data pool size."""
    return max(100, int(data_pool_size_gb * 0.01))


def format_capacity(gb: int) -> str:
    """Format GB to human-readable string."""
    if gb >= 1024:
        return f"{gb / 1024:.1f} TiB"
    return f"{gb} GB"


def calculate_vd_layout(
    total_drives: int,
    drive_size_gb: int,
    drives_per_vd: int = 10,
    needs_metadata: bool = True,
    mgs_size_gb: int = 0
) -> dict[str, Any]:
    """Calculate optimal VD layout with metadata/data split (1% metadata rule)."""
    total_capacity_gb = total_drives * drive_size_gb - mgs_size_gb

    if not needs_metadata:
        num_data_vds = max(1, total_drives // drives_per_vd)
        return {
            "data_vds": num_data_vds,
            "data_vd_size_gb": drive_size_gb,
            "metadata_vds": 0,
            "metadata_vd_size_gb": 0,
            "total_data_capacity_gb": total_capacity_gb,
            "total_metadata_capacity_gb": 0,
        }

    metadata_capacity_gb = max(100, int(total_capacity_gb * 0.01))
    data_capacity_gb = total_capacity_gb - metadata_capacity_gb

    num_metadata_vds = max(1, (metadata_capacity_gb + (drive_size_gb * drives_per_vd - 1)) // (drive_size_gb * drives_per_vd))
    num_data_vds = max(1, (data_capacity_gb + (drive_size_gb * drives_per_vd - 1)) // (drive_size_gb * drives_per_vd))

    return {
        "data_vds": num_data_vds,
        "data_vd_size_gb": drive_size_gb,
        "metadata_vds": num_metadata_vds,
        "metadata_vd_size_gb": drive_size_gb,
        "total_data_capacity_gb": data_capacity_gb,
        "total_metadata_capacity_gb": metadata_capacity_gb,
    }

# ── Wizard steps ───────────────────────────────────────────────────────────────

def step_appliance(defaults=None) -> tuple[str, str, str]:
    defaults = defaults or {}
    section("Step 1 — Appliance Details")

    appliance_name = inquirer.text(
        message="Appliance name / hostname:",
        default=defaults.get("appliance_name", ""),
        validate=lambda x: len(x.strip()) > 0,
        invalid_message="Name cannot be empty.",
    ).execute()

    fs_name = inquirer.text(
        message="Filesystem Name:",
        default=defaults.get("fs_name", ""),
        validate=lambda x: len(x.strip()) > 0,
        invalid_message="Filesystem Name cannot be empty.",
    ).execute()

    usage = inquirer.select(
        message="SFA Usage:",
        choices=SFA_USAGE,
        default=defaults.get("usage", "Mixed Use"),
    ).execute()

    return appliance_name.strip(), fs_name.strip(), usage.strip()


def step_check_existing_config(appliance_name: str) -> bool:
    """Check for existing config on appliance and prompt user."""
    section("Step 2 — Check Existing Configuration")

    appliance_ip = resolve_appliance_ip(appliance_name)
    if not appliance_ip:
        print("Warning: Could not resolve appliance IP. Skipping config check.\n")
        return False

    print(f"{DIM}Querying appliance ({appliance_ip}) for existing configuration...{RESET}")
    pool_output = run_emf_command(appliance_ip, "pool")
    vd_output = run_emf_command(appliance_ip, "vd")

    has_pools = pool_output and ("Optimal" in pool_output or "ready" in pool_output.lower())
    has_vds = vd_output and "ready" in vd_output.lower()
    has_config = has_pools or has_vds

    if not has_config:
        print(f"{DIM}Debug: pool_output length={len(pool_output) if pool_output else 0}{RESET}")
        print(f"{DIM}Debug: vd_output length={len(vd_output) if vd_output else 0}{RESET}")
        if pool_output:
            print(f"{DIM}Debug: pool_output first 200 chars:\n{pool_output[:200]}{RESET}\n")
        if vd_output:
            print(f"{DIM}Debug: vd_output first 200 chars:\n{vd_output[:200]}{RESET}\n")
        print("No existing configuration found.\n")
        return False

    print("Existing pool/VD configuration found:\n")
    if has_vds:
        vd_lines = vd_output.split('\n')
        vd_summary = [line for line in vd_lines if '10.36' in line][:5]
        print(f"{DIM}Virtual Disks:{RESET}")
        for line in vd_summary:
            print(f"{DIM}{line}{RESET}")
        if len([l for l in vd_lines if '10.36' in l]) > 5:
            print(f"{DIM}... and more ...{RESET}")

    if has_pools:
        pool_lines = pool_output.split('\n')
        pool_summary = [line for line in pool_lines if '10.36' in line][:5]
        print(f"\n{DIM}Pools:{RESET}")
        for line in pool_summary:
            print(f"{DIM}{line}{RESET}")
        if len([l for l in pool_lines if '10.36' in l]) > 5:
            print(f"{DIM}... and more ...{RESET}")

    print()
    keep = inquirer.confirm(
        message="Keep existing configuration?",
        default=False,
    ).execute()

    return keep


def step_detect_drives(appliance_name: str) -> list[DriveInfo]:
    """Detect and parse drives from appliance."""
    section("Step 3 — Detecting Drives")

    appliance_ip = resolve_appliance_ip(appliance_name)
    if not appliance_ip:
        print("Warning: Could not resolve appliance IP.\n")
        return []

    print(f"{DIM}Querying appliance ({appliance_ip}) for drive information...{RESET}")
    output = run_emf_command(appliance_ip, "physical-disk")

    if not output:
        print("Warning: Could not retrieve drive information from appliance.")
        print(f"{DIM}Debug: EMF command returned empty output{RESET}\n")
        return []

    drives = parse_drive_table(output)

    if not drives:
        has_headers = "Source IPs" in output and "Capacity" in output
        if has_headers:
            print("Warning: No drive data found (appliance may have no physical drives).")
            print(f"{DIM}Debug: Headers present but no data rows in output{RESET}\n")
        else:
            print("Warning: Could not parse drive information from EMF output.")
            print(f"{DIM}Debug: Raw output (first 500 chars):\n{output[:500]}{RESET}\n")
        return []

    groups = group_drives_by_type(drives)

    print(f"Found {len(drives)} drive(s):\n")
    for key, group in groups.items():
        print(f"  {group.drive_type}: {group.count} × {format_capacity(int(group.capacity_tib * 1024))} ({group.capacity_tib} TiB)")
    print()

    return drives


def step_ask_mgs() -> bool:
    """Ask if MGS is needed on the system."""
    section("Step 4 — Management Server (MGS)")

    print("The MGS stores Lustre filesystem configuration.\n")

    mgs_needed = inquirer.confirm(
        message="Will this appliance host the MGS?",
        default=False,
    ).execute()

    return mgs_needed


def step_configure_nvme_pools(drives: list[DriveInfo], usage: str, fs_name: str, has_mgs: bool = False) -> list[Pool]:
    """Configure NVMe pools with automatic metadata/data sizing."""
    section("Step 5a — Configure NVMe Pools")

    nvme_drives = [d for d in drives if d.drive_type == "NVMe"]
    if not nvme_drives:
        return []

    capacity_gb = int(nvme_drives[0].capacity_tib * 1024)
    total_nvme_drives = len(nvme_drives)
    print(f"Found {total_nvme_drives} NVMe drive(s) at {format_capacity(capacity_gb)} each.\n")

    use_standard_layout = inquirer.confirm(
        message="Use standard NVMe layout (2 pools × 12 drives)?",
        default=True,
    ).execute()

    if use_standard_layout:
        num_pools = 2
        drives_per_pool = 12
        print("Standard layout: 2 pools × 12 drives\n")
    else:
        num_pools = int(inquirer.text(
            message="Number of NVMe pools:",
            default="2",
            validate=NumberValidator(float_allowed=False, message="Enter a whole number."),
        ).execute())

        drives_per_pool = int(inquirer.text(
            message="Drives per pool:",
            default="12",
            validate=NumberValidator(float_allowed=False, message="Enter a whole number."),
        ).execute())

    needs_metadata = usage != "Data Only"
    mgs_size_gb = 128 if has_mgs else 0

    total_pool_capacity = num_pools * drives_per_pool * capacity_gb
    layout = calculate_vd_layout(num_pools * drives_per_pool, capacity_gb, drives_per_vd=10, needs_metadata=needs_metadata, mgs_size_gb=mgs_size_gb if has_mgs else 0)

    print(f"\n{HEADER}Total NVMe Capacity:{RESET}")
    print(f"  Total capacity: {format_capacity(total_pool_capacity)}")
    if has_mgs:
        print(f"  MGS VD:         {format_capacity(mgs_size_gb)}")
    print(f"  Data capacity:  {format_capacity(layout['total_data_capacity_gb'])}")
    if needs_metadata:
        print(f"  Metadata capacity: {format_capacity(layout['total_metadata_capacity_gb'])} (1% rule)")
    print()

    suggested_metadata_vds = 4 if num_pools >= 2 else 1
    suggested_data_vds = 8 if num_pools >= 2 else 4

    num_metadata_vds = int(inquirer.text(
        message="Number of metadata VDs:",
        default=str(suggested_metadata_vds) if needs_metadata else "0",
        validate=NumberValidator(float_allowed=False, message="Enter a whole number."),
    ).execute()) if needs_metadata else 0

    num_data_vds = int(inquirer.text(
        message="Number of data VDs:",
        default=str(suggested_data_vds),
        validate=NumberValidator(float_allowed=False, message="Enter a whole number."),
    ).execute())

    print(f"\n{HEADER}Virtual Disk Settings:{RESET}")
    print(f"  Default: Chunk 128KB, RAID 6, 10 drives per VD")
    print()

    use_standard_vd = inquirer.confirm(
        message="Use standard VD settings?",
        default=True,
    ).execute()

    if use_standard_vd:
        chunk_size_kb = 128
        raid_level = "RAID 6"
        drives_per_vd = 10
    else:
        chunk_size_kb = int(inquirer.select(
            message="Chunk size (KB):",
            choices=CHUNK_SIZES,
            default="128",
        ).execute())

        raid_level = inquirer.select(
            message="RAID level:",
            choices=RAID_LEVELS,
            default="RAID 6",
        ).execute()

        drives_per_vd = int(inquirer.text(
            message="Drives per VD:",
            default="10",
            validate=NumberValidator(float_allowed=False, message="Enter a whole number."),
        ).execute())

    pools = []
    mgs_created = False
    mdt_index = 0
    ost_index = 0

    for i in range(num_pools):
        pool = Pool(name=f"nvme_pool_{i+1}", tier="NVMe", disk_type="NVMe")

        if i == 0 and has_mgs and not mgs_created:
            mgs_vd = VirtualDisk(
                name="mgs",
                raid_level=raid_level,
                drive_count=2,
                drive_size_gb=64,
                chunk_size_kb=chunk_size_kb,
                purpose="Metadata",
                hot_spare=False
            )
            pool.virtual_disks.append(mgs_vd)
            mgs_created = True

        metadata_per_vd_gb = layout['total_metadata_capacity_gb'] // max(1, num_metadata_vds)
        data_per_vd_gb = layout['total_data_capacity_gb'] // max(1, num_data_vds)

        for j in range(num_metadata_vds):
            metadata_drive_size_gb = metadata_per_vd_gb // drives_per_vd
            vd = VirtualDisk(
                name=f"{fs_name}_mdt{mdt_index:04d}_s0",
                raid_level=raid_level,
                drive_count=drives_per_vd,
                drive_size_gb=metadata_drive_size_gb,
                chunk_size_kb=chunk_size_kb,
                purpose="Metadata",
                hot_spare=False
            )
            pool.virtual_disks.append(vd)
            mdt_index += 1

        for j in range(num_data_vds):
            data_drive_size_gb = data_per_vd_gb // drives_per_vd
            vd = VirtualDisk(
                name=f"{fs_name}_ost{ost_index:04d}",
                raid_level=raid_level,
                drive_count=drives_per_vd,
                drive_size_gb=data_drive_size_gb,
                chunk_size_kb=chunk_size_kb,
                purpose="Data",
                hot_spare=False
            )
            pool.virtual_disks.append(vd)
            ost_index += 1

        pools.append(pool)

    return pools


def step_configure_hdd_pools(drives: list[DriveInfo], usage: str, fs_name: str, has_mgs: bool = False) -> list[Pool]:
    """Configure HDD pools (flexible configuration with sizing suggestions)."""
    section("Step 5b — Configure HDD Pools")

    hdd_drives = [d for d in drives if d.drive_type.lower() in ["hdd", "sas", "sata", "sat"]]

    if not hdd_drives:
        return []

    capacity_gb = int(hdd_drives[0].capacity_tib * 1024)
    total_hdd_drives = len(hdd_drives)
    print(f"Found {total_hdd_drives} HDD drive(s) at {format_capacity(capacity_gb)} each.\n")
    print("Configure HDD pools (flexible layout)\n")

    pools = []
    pool_index = 0
    needs_metadata = usage != "Data Only"
    mgs_size_gb = 128 if has_mgs else 0
    mgs_created = False
    mdt_index = 0
    ost_index = 0

    print(f"\n{HEADER}Virtual Disk Settings:{RESET}")
    print(f"  Default: Chunk 256KB, RAID 6, 10 drives per VD, 1 VD per pool")
    print()

    use_standard_hdd = inquirer.confirm(
        message="Use standard HDD VD settings?",
        default=True,
    ).execute()

    if use_standard_hdd:
        chunk_size_kb = 256
        raid_level = "RAID 6"
        drives_per_vd = 10
    else:
        chunk_size_kb = int(inquirer.select(
            message="Chunk size (KB):",
            choices=CHUNK_SIZES,
            default="256",
        ).execute())

        raid_level = inquirer.select(
            message="RAID level:",
            choices=RAID_LEVELS,
            default="RAID 6",
        ).execute()

        drives_per_vd = int(inquirer.text(
            message="Drives per VD:",
            default="10",
            validate=NumberValidator(float_allowed=False, message="Enter a whole number."),
        ).execute())

    pools = []
    mgs_created = False
    pool_index = 0
    mdt_index = 0
    ost_index = 0

    while True:
        pool_index += 1
        pool_name = inquirer.text(
            message=f"HDD Pool {pool_index} name:",
            default=f"hdd_pool_{pool_index}",
        ).execute()

        disk_type = inquirer.select(
            message=f"Disk type for {pool_name}:",
            choices=["HDD", "SSD", "NVMe"],
            default="HDD",
        ).execute()

        num_drives_in_pool = inquirer.text(
            message=f"Number of drives in {pool_name}:",
            default="12",
            validate=NumberValidator(float_allowed=False, message="Enter a whole number."),
        ).execute()

        num_drives_in_pool = int(num_drives_in_pool)

        mgs_for_this_pool = mgs_size_gb if pool_index == 1 and not mgs_created else 0
        layout = calculate_vd_layout(num_drives_in_pool, capacity_gb, drives_per_vd=drives_per_vd, needs_metadata=needs_metadata, mgs_size_gb=mgs_for_this_pool)

        pool = Pool(name=pool_name.strip(), tier="HDD", disk_type=disk_type)

        if mgs_for_this_pool and not mgs_created:
            mgs_vd = VirtualDisk(
                name="mgs",
                raid_level=raid_level,
                drive_count=2,
                drive_size_gb=64,
                chunk_size_kb=chunk_size_kb,
                purpose="Metadata",
                hot_spare=False
            )
            pool.virtual_disks.append(mgs_vd)
            mgs_created = True

        metadata_per_vd_gb = layout['total_metadata_capacity_gb'] // max(1, layout['metadata_vds'])
        data_per_vd_gb = layout['total_data_capacity_gb'] // max(1, layout['data_vds'])

        for j in range(layout['metadata_vds']):
            metadata_drive_size_gb = metadata_per_vd_gb // drives_per_vd
            vd = VirtualDisk(
                name=f"{fs_name}_mdt{mdt_index:04d}_s0",
                raid_level=raid_level,
                drive_count=drives_per_vd,
                drive_size_gb=metadata_drive_size_gb,
                chunk_size_kb=chunk_size_kb,
                purpose="Metadata",
                hot_spare=False
            )
            pool.virtual_disks.append(vd)
            mdt_index += 1

        for j in range(layout['data_vds']):
            data_drive_size_gb = data_per_vd_gb // drives_per_vd
            vd = VirtualDisk(
                name=f"{fs_name}_ost{ost_index:04d}",
                raid_level=raid_level,
                drive_count=drives_per_vd,
                drive_size_gb=data_drive_size_gb,
                chunk_size_kb=chunk_size_kb,
                purpose="Data",
                hot_spare=False
            )
            pool.virtual_disks.append(vd)
            ost_index += 1

        pools.append(pool)

        add_another = inquirer.confirm(
            message="Add another HDD pool?",
            default=False,
        ).execute()

        if not add_another:
            break

    return pools


def step_review(config: StorageConfig) -> None:
    section("Step 6 — Review Configuration")
    print(json.dumps(config.to_dict(), indent=2))
    print()



# ── Entry point ────────────────────────────────────────────────────────────────

def run_wizard(defaults=None) -> StorageConfig:
    defaults = defaults or {}
    print(f"\n{HEADER}{'═' * 50}")
    print("  Storage Appliance Layout Wizard")
    print(f"{'═' * 50}{RESET}")
    print(f"{DIM}  Use arrow keys to navigate, Enter to select.{RESET}\n")

    appliance_name, fs_name, usage = step_appliance(defaults)

    keep_existing = step_check_existing_config(appliance_name)
    if keep_existing:
        print(f"{DIM}Using existing configuration. Skipping drive detection.{RESET}\n")
        drives = []
    else:
        drives = step_detect_drives(appliance_name)

    has_mgs = step_ask_mgs()

    config = StorageConfig(
        appliance_name=appliance_name,
        fs_name=fs_name,
        usage=usage,
        has_mgs=has_mgs
    )

    if not keep_existing and drives:
        nvme_pools = step_configure_nvme_pools(drives, usage, fs_name, has_mgs)
        config.pools.extend(nvme_pools)

        hdd_pools = step_configure_hdd_pools(drives, usage, fs_name, has_mgs)
        config.pools.extend(hdd_pools)

        if not nvme_pools and not hdd_pools:
            print("\nNo pools configured. Skipping to review.\n")
    elif not keep_existing:
        print("\nNo drives detected. Skipping pool configuration.\n")

    step_review(config)

    confirmed = inquirer.confirm(
        message="Confirm and submit this configuration?",
        default=True,
    ).execute()

    if not confirmed:
        print("\nWizard cancelled. No config returned.\n")
        return None

    print(f"\n{HEADER}✔  Configuration complete.{RESET}\n")
    return config


if __name__ == "__main__":
    args = parse_args()
    defaults = {k: v for k, v in vars(args).items() if v is not None}
    result = run_wizard(defaults=defaults)
    if result:
        # Hand off to your downstream script here, e.g.:
        # apply_config(result)
        print("Returned StorageConfig dict:")
        print(json.dumps(result.to_dict(), indent=2))
