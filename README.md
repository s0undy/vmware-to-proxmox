# vmware-to-proxmox

Migrates Windows VMs from VMware vCenter to Proxmox VE.

The tool connects to both vCenter and Proxmox, then automates the pre-migration steps so the VM boots cleanly on its new host.

## What it does

The migration runs in six steps:

1. **Storage vMotion** the VM to a shared/accessible datastore
2. **Create the Proxmox VM** shell (matching CPU, RAM, disks, NICs)
3. **Export NIC configuration** inside the guest to `network.json`
4. **Enable VirtIO SCSI boot driver** so Windows can boot from VirtIO disks
5. **Install VirtIO guest tools** inside the guest
6. **Shut down the VM**

After that, you manually copy the VMDK files to Proxmox, boot the VM, and run the post-migration scripts.

## Prerequisites

- Python 3.10+
- VMware Tools running in the guest VM
- VirtIO drivers and guest tools installer staged inside the guest (default: `C:\TMP\pveMigration\`)
- The PowerShell scripts (`exportNicConfig.ps1`, `enable-vioscsi-to-load-on-boot.ps1`) staged in the same directory

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
```

Edit `config.yaml` with your vCenter, Proxmox, and guest credentials.

Passwords can also be set via environment variables: `VCENTER_PASSWORD`, `PROXMOX_PASSWORD`, `GUEST_PASSWORD`.

## Usage

```bash
python migrate.py
```

All settings from `config.yaml` can be overridden with CLI flags. Run `python migrate.py --help` for the full list.

Useful options:

| Flag | Purpose |
|---|---|
| `--dry-run` | Log what would happen without making changes |
| `--skip-to N` | Resume from step N (1-6) |
| `--parallel` | Migrate all VMs concurrently |
| `--verbose` | Debug-level logging |

## Migrating multiple VMs

Add a `vms:` list under `migration:` in your config file. Shared settings (datastore, storage, paths, etc.) go at the top of the `migration:` block; any migration field plus `guest_user`/`guest_password` can be overridden per VM.

```yaml
migration:
  migration_datastore: "shared-ds"
  proxmox_storage: "local-lvm"
  proxmox_bridges: "vmbr0"      # default bridge for all VMs

  vms:
    - vm_name: "web-server"
      proxmox_vmid: 200
      proxmox_bridges: "vmbr0"
    - vm_name: "db-server"
      proxmox_vmid: 201
      proxmox_bridges: "vmbr1"  # different bridge
      max_cores: 4               # different CPU topology
      max_sockets: 2
      guest_user: "OtherAdmin"   # different guest credentials
      guest_password: "secret"
```

By default the VMs are migrated **sequentially** (one after the other). To migrate them all at the same time, set `parallel: true` in your config or pass `--parallel`:

```bash
python migrate.py --parallel
```

In sequential mode, a failure on one VM is logged and the tool continues with the remaining VMs. In parallel mode, all VMs start simultaneously and any failures are reported at the end.

To migrate a single VM via CLI (overrides `vms:` in the config):

```bash
python migrate.py --vm-name "my-vm"
```

## Post-migration (manual)

1. Copy VMDK files from the migration datastore to Proxmox storage
2. Boot the VM on Proxmox
3. Run `importNicConfig.ps1` to restore network settings
4. Run `purge-vmware-tools.ps1 -Force` to remove VMware Tools
5. Reboot
