"""Configuration dataclasses and loading logic."""

import getpass
import os
from dataclasses import dataclass, fields, replace

import yaml

from .exceptions import ConfigurationError


@dataclass
class VCenterConfig:
    host: str
    user: str
    password: str
    port: int = 443
    insecure: bool = True


@dataclass
class ProxmoxConfig:
    host: str
    user: str
    node: str
    password: str = ""
    token_name: str = ""
    token_value: str = ""
    port: int = 8006
    verify_ssl: bool = False
    ssh_user: str = ""
    ssh_port: int = 22


@dataclass
class GuestConfig:
    user: str
    password: str


@dataclass
class MigrationConfig:
    vm_name: str
    migration_datastore: str
    proxmox_storage: str
    proxmox_final_storage: str = ""
    start_vm_before_move: bool = True
    proxmox_vmid: int = 0
    proxmox_bridges: str = "vmbr0"
    max_cores: int = 0
    max_sockets: int = 1
    staging_dir: str = r"C:\TMP\pveMigration"
    virtio_driver_path: str = r"C:\TMP\pveMigration\vioscsi"
    virtio_tools_path: str = r"C:\TMP\pveMigration\virtio-win-guest-tools.exe"
    export_nic_script: str = r"C:\TMP\pveMigration\exportNicConfig.ps1"
    vioscsi_script: str = r"C:\TMP\pveMigration\enable-vioscsi-to-load-on-boot.ps1"
    virtio_iso_storage: str = "local"
    virtio_iso_filename: str = "virtio-win-0.1.271-1.iso"
    purge_vmware_script: str = r"C:\TMP\pveMigration\purge-vmware-tools.ps1"
    import_nic_script: str = r"C:\TMP\pveMigration\importNicConfig.ps1"


_MIGRATION_FIELD_NAMES = {f.name for f in fields(MigrationConfig)}


@dataclass
class AppConfig:
    vcenter: VCenterConfig
    proxmox: ProxmoxConfig
    guest: GuestConfig
    migration: MigrationConfig


def _resolve_password(cli_value, env_var, yaml_value, prompt_label):
    """Resolve a password from CLI > env > YAML > interactive prompt."""
    value = cli_value or os.environ.get(env_var) or yaml_value
    if value:
        return value
    return getpass.getpass(f"{prompt_label}: ")


def _pick(cli_value, yaml_value, default):
    """Return the first non-None value: CLI > YAML > default."""
    if cli_value is not None:
        return cli_value
    if yaml_value is not None:
        return yaml_value
    return default


def load_config(args, yaml_data: dict | None = None) -> tuple[list["AppConfig"], dict]:
    """Build a list of AppConfig (one per VM) by merging YAML, CLI arguments, and env vars.

    Returns:
        A tuple of (list[AppConfig], runtime_dict) where runtime_dict contains
        workflow flags: skip_to, dry_run, verbose, parallel.
    """
    if yaml_data is None:
        yaml_data = {}
        if os.path.exists(args.config):
            with open(args.config) as f:
                yaml_data = yaml.safe_load(f) or {}

    vc_yaml = yaml_data.get("vcenter", {})
    px_yaml = yaml_data.get("proxmox", {})
    guest_yaml = yaml_data.get("guest", {})
    mig_yaml = yaml_data.get("migration", {})

    # ------------------------------------------------------------------
    # vCenter
    # ------------------------------------------------------------------
    vc_host = args.vcenter_host or vc_yaml.get("host")
    vc_user = args.vcenter_user or vc_yaml.get("user")
    if not vc_host or not vc_user:
        raise ConfigurationError("vCenter host and user are required")
    vc_password = _resolve_password(
        args.vcenter_password, "VCENTER_PASSWORD",
        vc_yaml.get("password"), "vCenter password",
    )
    vc_port = _pick(args.vcenter_port, vc_yaml.get("port"), 443)
    vc_insecure = _pick(args.vcenter_insecure, vc_yaml.get("insecure"), True)

    # ------------------------------------------------------------------
    # Proxmox
    # ------------------------------------------------------------------
    px_host = args.proxmox_host or px_yaml.get("host")
    px_user = args.proxmox_user or px_yaml.get("user")
    px_node = args.proxmox_node or px_yaml.get("node")
    if not px_host or not px_user or not px_node:
        raise ConfigurationError("Proxmox host, user, and node are required")

    px_token_name = args.proxmox_token_name or px_yaml.get("token_name", "")
    px_token_value = args.proxmox_token_value or px_yaml.get("token_value", "")
    if px_token_name and px_token_value:
        px_password = ""
    else:
        px_password = _resolve_password(
            args.proxmox_password, "PROXMOX_PASSWORD",
            px_yaml.get("password"), "Proxmox password",
        )
    px_port = _pick(args.proxmox_port, px_yaml.get("port"), 8006)
    px_verify_ssl = _pick(args.proxmox_verify_ssl, px_yaml.get("verify_ssl"), False)
    px_ssh_user = args.proxmox_ssh_user or px_yaml.get("ssh_user", "")
    px_ssh_port = int(_pick(args.proxmox_ssh_port, px_yaml.get("ssh_port"), 22))

    # ------------------------------------------------------------------
    # Guest
    # ------------------------------------------------------------------
    guest_user = args.guest_user or guest_yaml.get("user")
    if not guest_user:
        raise ConfigurationError("Guest OS user is required")
    guest_password = _resolve_password(
        args.guest_password, "GUEST_PASSWORD",
        guest_yaml.get("password"), "Guest OS password",
    )

    # ------------------------------------------------------------------
    # Shared infrastructure config (same for all VMs)
    # ------------------------------------------------------------------
    vcenter_config = VCenterConfig(
        host=vc_host,
        user=vc_user,
        password=vc_password,
        port=int(vc_port),
        insecure=bool(vc_insecure),
    )
    proxmox_config = ProxmoxConfig(
        host=px_host,
        user=px_user,
        node=px_node,
        password=px_password,
        token_name=px_token_name,
        token_value=px_token_value,
        port=int(px_port),
        verify_ssl=bool(px_verify_ssl),
        ssh_user=px_ssh_user,
        ssh_port=px_ssh_port,
    )
    guest_config = GuestConfig(
        user=guest_user,
        password=guest_password,
    )

    # ------------------------------------------------------------------
    # Migration — shared template (defaults for all VMs)
    # ------------------------------------------------------------------
    migration_ds = args.migration_datastore or mig_yaml.get("migration_datastore")
    px_storage = args.proxmox_storage or mig_yaml.get("proxmox_storage")
    px_final_storage = args.proxmox_final_storage or mig_yaml.get("proxmox_final_storage", "")
    start_vm_before_move = bool(_pick(
        args.start_vm_before_move, mig_yaml.get("start_vm_before_move"), True
    ))
    if not migration_ds:
        raise ConfigurationError("--migration-datastore is required")
    if not px_storage:
        raise ConfigurationError("--proxmox-storage is required")

    px_bridges = args.proxmox_bridges or mig_yaml.get("proxmox_bridges", "vmbr0")
    max_cores = int(_pick(args.max_cores, mig_yaml.get("max_cores"), 0))
    max_sockets = int(_pick(args.max_sockets, mig_yaml.get("max_sockets"), 1))

    staging_dir = (args.staging_dir
                   or mig_yaml.get("staging_dir", r"C:\TMP\pveMigration"))
    virtio_driver_path = (args.virtio_driver_path
                          or mig_yaml.get("virtio_driver_path",
                                          r"C:\TMP\pveMigration\vioscsi"))
    virtio_tools_path = (args.virtio_tools_path
                         or mig_yaml.get("virtio_tools_path",
                                         r"C:\TMP\pveMigration\virtio-win-guest-tools.exe"))
    export_nic_script = (args.export_nic_script
                         or mig_yaml.get("export_nic_script",
                                         r"C:\TMP\pveMigration\exportNicConfig.ps1"))
    vioscsi_script = (args.vioscsi_script
                      or mig_yaml.get("vioscsi_script",
                                      r"C:\TMP\pveMigration\enable-vioscsi-to-load-on-boot.ps1"))
    virtio_iso_storage = (args.virtio_iso_storage
                          or mig_yaml.get("virtio_iso_storage", "local"))
    virtio_iso_filename = (args.virtio_iso_filename
                           or mig_yaml.get("virtio_iso_filename",
                                           "virtio-win-0.1.271-1.iso"))
    purge_vmware_script = (args.purge_vmware_script
                           or mig_yaml.get("purge_vmware_script",
                                           r"C:\TMP\pveMigration\purge-vmware-tools.ps1"))
    import_nic_script = (args.import_nic_script
                         or mig_yaml.get("import_nic_script",
                                         r"C:\TMP\pveMigration\importNicConfig.ps1"))

    # ------------------------------------------------------------------
    # Build per-VM configs
    # ------------------------------------------------------------------
    vms_yaml = mig_yaml.get("vms")
    cli_vm_name = args.vm_name
    cli_vmid = args.proxmox_vmid

    if cli_vm_name:
        # CLI --vm-name always selects a single VM, overriding any vms: list.
        vm_targets = [{"vm_name": cli_vm_name, "proxmox_vmid": cli_vmid or 0}]
    elif vms_yaml:
        # Multi-VM from YAML.
        vm_targets = vms_yaml
    else:
        # Backward-compatible single vm_name from YAML.
        single_name = mig_yaml.get("vm_name")
        if not single_name:
            raise ConfigurationError(
                "Specify at least one VM: set migration.vm_name, migration.vms, or --vm-name"
            )
        single_vmid = int(_pick(None, mig_yaml.get("proxmox_vmid"), 0))
        vm_targets = [{"vm_name": single_name, "proxmox_vmid": single_vmid}]

    # Shared template — vm_name is a placeholder; overridden per VM below.
    migration_template = MigrationConfig(
        vm_name="",
        migration_datastore=migration_ds,
        proxmox_storage=px_storage,
        proxmox_final_storage=px_final_storage,
        start_vm_before_move=start_vm_before_move,
        proxmox_vmid=0,
        proxmox_bridges=px_bridges,
        max_cores=max_cores,
        max_sockets=max_sockets,
        staging_dir=staging_dir,
        virtio_driver_path=virtio_driver_path,
        virtio_tools_path=virtio_tools_path,
        export_nic_script=export_nic_script,
        vioscsi_script=vioscsi_script,
        virtio_iso_storage=virtio_iso_storage,
        virtio_iso_filename=virtio_iso_filename,
        purge_vmware_script=purge_vmware_script,
        import_nic_script=import_nic_script,
    )

    app_configs: list[AppConfig] = []
    for entry in vm_targets:
        if not entry.get("vm_name"):
            raise ConfigurationError("Each vms entry must have a vm_name")
        # Pick only known MigrationConfig fields from the entry, cast numeric types.
        overrides: dict = {}
        for key, val in entry.items():
            if key not in _MIGRATION_FIELD_NAMES:
                continue
            if key in ("proxmox_vmid", "max_cores", "max_sockets"):
                val = int(val)
            if key == "start_vm_before_move":
                val = bool(val)
            overrides[key] = val
        vm_migration = replace(migration_template, **overrides)

        # Guest credentials can be overridden per VM.
        vm_guest_user = entry.get("guest_user") or guest_config.user
        vm_guest_password = entry.get("guest_password") or guest_config.password
        vm_guest = (
            GuestConfig(user=vm_guest_user, password=vm_guest_password)
            if (vm_guest_user != guest_config.user or vm_guest_password != guest_config.password)
            else guest_config
        )

        app_configs.append(AppConfig(
            vcenter=vcenter_config,
            proxmox=proxmox_config,
            guest=vm_guest,
            migration=vm_migration,
        ))

    # ------------------------------------------------------------------
    # Runtime flags (CLI > YAML > defaults)
    # ------------------------------------------------------------------
    skip_to = int(_pick(args.skip_to, yaml_data.get("skip_to"), 1))
    dry_run = bool(_pick(args.dry_run, yaml_data.get("dry_run"), False))
    parallel = bool(_pick(args.parallel, yaml_data.get("parallel"), False))

    runtime = {
        "skip_to": skip_to,
        "dry_run": dry_run,
        "parallel": parallel,
    }

    return app_configs, runtime
