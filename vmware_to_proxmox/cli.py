"""Command-line interface and configuration loading."""

import argparse
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from .config import AppConfig, load_config
from .exceptions import ConfigurationError, MigrationError
from .migration import MigrationOrchestrator

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="migrate",
        description="Migrate Windows VMs from VMware vCenter to Proxmox VE",
    )

    parser.add_argument(
        "--config", "-c", default="config.yaml",
        help="Path to YAML config file (default: config.yaml)",
    )

    # Migration targets
    parser.add_argument("--vm-name", help="Name of the VM to migrate (overrides migration.vms)")
    parser.add_argument("--migration-datastore",
                        help="vCenter datastore for staging the migration")
    parser.add_argument("--proxmox-storage",
                        help="Proxmox storage target for VM disks")

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
    parser.add_argument("--proxmox-token-name",
                        help="Proxmox API token name (alternative to password)")
    parser.add_argument("--proxmox-token-value",
                        help="Proxmox API token value")

    # Proxmox VM options
    parser.add_argument("--proxmox-vmid", type=int, default=None,
                        help="Proxmox VMID to use (default: next available)")
    parser.add_argument("--proxmox-bridges",
                        help="Comma-separated bridge list for NICs in order "
                             "(e.g. vmbr0,vmbr1). Last bridge reused for extra NICs.")
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

    # Workflow control
    parser.add_argument(
        "--skip-to", type=int, default=None, choices=range(1, 7),
        help="Resume from step N (1-6, default: 1)",
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


def _run_sequential(orchestrators: list[MigrationOrchestrator]) -> list[str]:
    """Run migrations one at a time; return list of failed VM names."""
    failures = []
    for orch in orchestrators:
        try:
            orch.run()
        except MigrationError as exc:
            logger.error("")
            logger.error("MIGRATION FAILED [%s]: %s", orch.config.migration.vm_name, exc)
            logger.error(
                "You can resume with:  python migrate.py --vm-name %s --skip-to <step>",
                orch.config.migration.vm_name,
            )
            failures.append(orch.config.migration.vm_name)
    return failures


def _run_parallel(orchestrators: list[MigrationOrchestrator]) -> list[str]:
    """Run all migrations concurrently; return list of failed VM names."""
    failures = []
    with ThreadPoolExecutor(max_workers=len(orchestrators)) as executor:
        future_to_orch = {executor.submit(orch.run): orch for orch in orchestrators}
        for future in as_completed(future_to_orch):
            orch = future_to_orch[future]
            vm_name = orch.config.migration.vm_name
            try:
                future.result()
            except MigrationError as exc:
                logger.error("")
                logger.error("MIGRATION FAILED [%s]: %s", vm_name, exc)
                logger.error(
                    "You can resume with:  python migrate.py --vm-name %s --skip-to <step>",
                    vm_name,
                )
                failures.append(vm_name)
    return failures


def main():
    parser = build_parser()
    args = parser.parse_args()

    # Resolve verbose from CLI > YAML (need to peek at YAML before full load)
    import os, yaml
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

    orchestrators = [
        MigrationOrchestrator(cfg, skip_to=runtime["skip_to"], dry_run=runtime["dry_run"])
        for cfg in configs
    ]

    try:
        if runtime["parallel"] and len(orchestrators) > 1:
            logger.info("Running %d migrations in parallel.", len(orchestrators))
            failures = _run_parallel(orchestrators)
        else:
            failures = _run_sequential(orchestrators)
    except KeyboardInterrupt:
        logger.warning("\nInterrupted by user.")
        sys.exit(130)

    if failures:
        logger.error("")
        logger.error("FAILED: %s", ", ".join(failures))
        sys.exit(1)
