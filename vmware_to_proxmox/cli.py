"""Command-line interface and configuration loading."""

import argparse
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import yaml

from .config import AppConfig, load_config
from .exceptions import ConfigurationError, MigrationError
from .migration import MigrationOrchestrator
from .os_handlers import get_os_handler

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="migrate",
        description="Migrate VMs from VMware vCenter to Proxmox VE",
    )

    parser.add_argument(
        "--config", "-c", default="config.yaml",
        help="Path to YAML config file (default: config.yaml)",
    )

    # OS type
    parser.add_argument(
        "--os-type",
        choices=["auto", "windows", "ubuntu", "other"],
        default=None,
        help="Guest OS type (default: auto-detect from vCenter guestId). "
             "'other' skips all OS-specific steps.",
    )

    # Migration targets
    parser.add_argument("--vm-name", help="Name of the VM to migrate (overrides migration.vms)")
    parser.add_argument("--migration-datastore",
                        help="vCenter datastore for staging the migration")
    parser.add_argument("--proxmox-storage",
                        help="Proxmox storage target for VM disks")
    parser.add_argument("--proxmox-final-storage",
                        help="Proxmox storage for final disk location (step 9 moves disks here as qcow2)")
    parser.add_argument("--disk-move-timeout", type=int, default=None,
                        help="Timeout in seconds for each disk move in step 9 (default: 14400 = 4 hours)")
    parser.add_argument("--disk-conversion-backend",
                        choices=["proxmox-native", "netapp-shift"],
                        default=None,
                        help="Backend that owns steps 6-11 (default: proxmox-native)")
    parser.add_argument("--start-vm-before-move", action="store_true", default=None,
                        help="Start VM before moving disks (default: true)")
    parser.add_argument("--no-start-vm-before-move", dest="start_vm_before_move",
                        action="store_false",
                        help="Start VM after disks are moved to final storage")
    parser.add_argument("--enable-nics-on-boot", action="store_true", default=None,
                        help="Create NICs with link enabled (faster boot for domain-joined VMs, "
                             "halves wait timers)")
    parser.add_argument("--no-enable-nics-on-boot", dest="enable_nics_on_boot",
                        action="store_false",
                        help="Create NICs with link disabled (default, enables in step 15)")
    parser.add_argument("--enable-ha", action="store_true", default=None,
                        help="Add VM to Proxmox HA after migration completes (default: false)")
    parser.add_argument("--no-enable-ha", dest="enable_ha", action="store_false",
                        help="Do not add VM to Proxmox HA (default)")

    # vCenter
    parser.add_argument("--vcenter-host", help="vCenter hostname or IP")
    parser.add_argument("--vcenter-user", help="vCenter username")
    parser.add_argument("--vcenter-password", help="vCenter password")
    parser.add_argument("--vcenter-port", type=int, default=None,
                        help="vCenter port (default: 443)")
    parser.add_argument("--vcenter-insecure", action="store_true", default=None,
                        help="Skip SSL certificate verification for vCenter")

    # Proxmox
    parser.add_argument("--proxmox-host", help="Proxmox hostname or IP")
    parser.add_argument("--proxmox-user", help="Proxmox username")
    parser.add_argument("--proxmox-password", help="Proxmox password")
    parser.add_argument("--proxmox-node", help="Proxmox node name")
    parser.add_argument("--proxmox-port", type=int, default=None,
                        help="Proxmox API port (default: 8006)")
    parser.add_argument("--proxmox-verify-ssl", action="store_true", default=None,
                        help="Verify SSL certificate for Proxmox")
    parser.add_argument("--proxmox-ssh-user",
                        help="SSH username for Proxmox node (default: API user without realm)")
    parser.add_argument("--proxmox-ssh-port", type=int, default=None,
                        help="SSH port for Proxmox node (default: 22)")
    parser.add_argument("--proxmox-token-name",
                        help="Proxmox API token name (alternative to password)")
    parser.add_argument("--proxmox-token-value",
                        help="Proxmox API token value")

    # NetApp Shift (required when --disk-conversion-backend=netapp-shift)
    parser.add_argument("--netapp-shift-host",
                        help="NetApp Shift hostname or IP")
    parser.add_argument("--netapp-shift-user",
                        help="NetApp Shift username")
    parser.add_argument("--netapp-shift-password",
                        help="NetApp Shift password (or NETAPP_SHIFT_PASSWORD env var)")
    parser.add_argument("--netapp-shift-port", type=int, default=None,
                        help="NetApp Shift API port (default: 443)")
    parser.add_argument("--netapp-shift-verify-ssl", action="store_true", default=None,
                        help="Verify SSL certificate for NetApp Shift")
    parser.add_argument("--netapp-source-site",
                        help="Source site name registered in NetApp Shift")
    parser.add_argument("--netapp-destination-site",
                        help="Destination site name registered in NetApp Shift")
    parser.add_argument("--netapp-destination-volume",
                        help="Destination NetApp volume backing the qtree")
    parser.add_argument("--netapp-destination-qtree",
                        help="Destination QTree name for converted disks")

    # Proxmox VM options
    parser.add_argument("--proxmox-vmid", type=int, default=None,
                        help="Proxmox VMID to use (default: next available)")
    parser.add_argument("--proxmox-bridges",
                        help="Comma-separated bridge list for NICs in order "
                             "(e.g. vmbr0,vmbr1). Last bridge reused for extra NICs.")
    parser.add_argument("--cpu-type", default=None,
                        help="CPU type for the VM (default: host)")
    parser.add_argument("--cpu-flags", default=None,
                        help="Extra CPU flags to add or remove "
                             "(e.g. '+aes,-vmx')")
    parser.add_argument("--max-cores", type=int, default=None,
                        help="Maximum cores per socket (0 = no limit, default: 0)")
    parser.add_argument("--max-sockets", type=int, default=None,
                        help="Maximum CPU sockets (default: 1)")

    # Guest
    parser.add_argument("--guest-user", help="Guest OS administrator username")
    parser.add_argument("--guest-password", help="Guest OS password")

    # Guest script paths
    parser.add_argument("--staging-dir",
                        help="Staging directory inside the guest "
                             r"(default: C:\TMP\pveMigration)")
    parser.add_argument("--virtio-driver-path",
                        help="Path to vioscsi driver folder inside the guest")
    parser.add_argument("--virtio-tools-path",
                        help="Path to virtio-win-guest-tools.exe inside the guest")
    parser.add_argument("--export-nic-script",
                        help="Path to exportNicConfig.ps1 inside the guest")
    parser.add_argument("--vioscsi-script",
                        help="Path to enable-vioscsi-to-load-on-boot.ps1 inside the guest")
    parser.add_argument("--virtio-iso-storage",
                        help="Proxmox storage containing the VirtIO ISO (default: local)")
    parser.add_argument("--virtio-iso-filename",
                        help="VirtIO ISO filename (default: virtio-win-0.1.271-1.iso)")
    parser.add_argument("--purge-vmware-script",
                        help="Path to purge-vmware-tools.ps1 inside the guest")
    parser.add_argument("--import-nic-script",
                        help="Path to importNicConfig.ps1 inside the guest")

    # Workflow control
    parser.add_argument(
        "--skip-to", type=int, default=None, choices=range(1, 16),
        help="Resume from step N (1-15, default: 1)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", default=None,
        help="Log what would happen without making any changes",
    )
    parser.add_argument(
        "--parallel", action="store_true", default=None,
        help="Migrate all VMs concurrently (default: sequential)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", default=None,
        help="Enable debug-level logging",
    )

    return parser


def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)-7s] %(message)s",
        datefmt="%H:%M:%S",
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)

    # Quiet noisy libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)


def _run_sequential(orchestrators: list[MigrationOrchestrator]) -> tuple[list[str], list[dict]]:
    """Run migrations one at a time; return (failed VM names, results)."""
    failures = []
    results = []
    for orch in orchestrators:
        try:
            result = orch.run()
            results.append(result)
        except Exception as exc:
            logger.error("")
            logger.error("MIGRATION FAILED [%s]: %s", orch.config.migration.vm_name, exc)
            logger.error(
                "You can resume with:  python migrate.py --vm-name %s --skip-to <step>",
                orch.config.migration.vm_name,
            )
            failures.append(orch.config.migration.vm_name)
    return failures, results


def _run_parallel(orchestrators: list[MigrationOrchestrator]) -> tuple[list[str], list[dict]]:
    """Run all migrations concurrently; return (failed VM names, results)."""
    failures = []
    results = []
    with ThreadPoolExecutor(max_workers=len(orchestrators)) as executor:
        future_to_orch = {executor.submit(orch.run): orch for orch in orchestrators}
        for future in as_completed(future_to_orch):
            orch = future_to_orch[future]
            vm_name = orch.config.migration.vm_name
            try:
                result = future.result()
                results.append(result)
            except Exception as exc:
                logger.error("")
                logger.error("MIGRATION FAILED [%s]: %s", vm_name, exc)
                logger.error(
                    "You can resume with:  python migrate.py --vm-name %s --skip-to <step>",
                    vm_name,
                )
                failures.append(vm_name)
    return failures, results


def _print_summary(results: list[dict]) -> None:
    """Print a final summary table of all migrated VMs."""
    if not results:
        return
    logger.info("")
    logger.info("=" * 60)
    logger.info("MIGRATION SUMMARY")
    logger.info("=" * 60)
    logger.info("  %-25s %-15s %-16s %s", "VM Name", "Storage", "IP Address", "Duration")
    logger.info("  %-25s %-15s %-16s %s", "-" * 25, "-" * 15, "-" * 16, "-" * 10)
    for r in results:
        minutes, seconds = divmod(r["elapsed_seconds"], 60)
        duration = f"{minutes}m {seconds}s"
        ip = r.get("ip_address") or "N/A"
        logger.info("  %-25s %-15s %-16s %s", r["vm_name"], r["final_storage"], ip, duration)
    logger.info("=" * 60)


def main():
    parser = build_parser()
    args = parser.parse_args()

    # Resolve verbose from CLI > YAML (need to peek at YAML before full load)
    yaml_data = {}
    if os.path.exists(args.config):
        with open(args.config) as f:
            yaml_data = yaml.safe_load(f) or {}
    verbose = args.verbose if args.verbose is not None else yaml_data.get("verbose", False)
    setup_logging(verbose=verbose)

    try:
        configs, runtime = load_config(args, yaml_data)
    except ConfigurationError as exc:
        logger.error("Configuration error: %s", exc)
        sys.exit(1)

    orchestrators = []
    for cfg in configs:
        os_type = cfg.migration.os_type
        handler = get_os_handler(os_type) if os_type != "auto" else None
        orchestrators.append(
            MigrationOrchestrator(
                cfg,
                skip_to=runtime["skip_to"],
                dry_run=runtime["dry_run"],
                os_handler=handler,
            )
        )

    try:
        if runtime["parallel"] and len(orchestrators) > 1:
            logger.info("Running %d migrations in parallel.", len(orchestrators))
            failures, results = _run_parallel(orchestrators)
        else:
            failures, results = _run_sequential(orchestrators)
    except KeyboardInterrupt:
        logger.warning("\nInterrupted by user.")
        sys.exit(130)

    _print_summary(results)

    if failures:
        logger.error("")
        logger.error("FAILED: %s", ", ".join(failures))
        sys.exit(1)
