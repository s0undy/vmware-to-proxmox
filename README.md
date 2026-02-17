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
| `--verbose` | Debug-level logging |

## Post-migration (manual)

1. Copy VMDK files from the migration datastore to Proxmox storage
2. Boot the VM on Proxmox
3. Run `importNicConfig.ps1` to restore network settings
4. Run `purge-vmware-tools.ps1 -Force` to remove VMware Tools
5. Reboot
