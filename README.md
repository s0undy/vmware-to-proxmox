# vmware-to-proxmox

Migrates Windows VMs from VMware vCenter to Proxmox VE.

The tool connects to both vCenter and Proxmox, then automates the pre-migration steps so the VM boots cleanly on its new host.

## What it does

The migration runs in fourteen steps:

1. **Storage vMotion** the VM to a shared/accessible datastore
2. **Create the Proxmox VM** shell (matching CPU, RAM, disks, NICs with SSD emulation, discard, and IO threads enabled)
3. **Export NIC configuration** inside the guest to `network.json`
4. **Enable VirtIO SCSI boot driver** so Windows can boot from VirtIO disks
5. **Install VirtIO guest tools** inside the guest
6. **Shut down the VM**
7. **Rewrite VMDK descriptors** on the Proxmox node to point at the original flat files
8. **Start the VM in Proxmox** (skipped if `start_vm_before_move: false`)
9. **Move disks to final storage** — moves each disk (including EFI disk) one at a time to `proxmox_final_storage`, converting to qcow2. Source disks are kept. If `start_vm_before_move: false`, the VM is started after all disks are moved.
10. **Verify migration** — confirms the VM is running and all disks reside on the final storage
11. **Install VirtIO drivers from ISO** — mounts the VirtIO ISO on the VM's CD/DVD drive, discovers the drive letter via QEMU guest agent, and runs `msiexec` to install `virtio-win-gt-x64.msi`
12. **Purge VMware Tools** — runs `purge-vmware-tools.ps1 -Force` via QEMU guest agent to remove VMware Tools
13. **Restore NIC configuration** — runs `importNicConfig.ps1` via QEMU guest agent to restore the network configuration exported in step 3
14. **Enable NICs and final reboot** — unmounts the ISO, enables all network interfaces (`link_down=0`), and reboots one last time

## Prerequisites

- Python 3.10+
- VMware Tools running in the guest VM
- VirtIO drivers and guest tools installer staged inside the guest (default: `C:\TMP\pveMigration\`)
- The PowerShell scripts (`exportNicConfig.ps1`, `enable-vioscsi-to-load-on-boot.ps1`, `importNicConfig.ps1`, `purge-vmware-tools.ps1`) staged in the same directory
- VirtIO ISO (`virtio-win-0.1.271-1.iso`) uploaded to a Proxmox storage (default: `local`)
- QEMU guest agent installed and running in the guest (required for steps 11-14)

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
| `--skip-to N` | Resume from step N (1-14) |
| `--parallel` | Migrate all VMs concurrently |
| `--verbose` | Debug-level logging |

## Migrating multiple VMs

Add a `vms:` list under `migration:` in your config file. Shared settings (datastore, storage, paths, etc.) go at the top of the `migration:` block; any migration field plus `guest_user`/`guest_password` can be overridden per VM.

```yaml
migration:
  migration_datastore: "shared-ds"
  proxmox_storage: "migration-nfs"
  proxmox_final_storage: "local-lvm"
  proxmox_bridges: "vmbr0"      # default bridge for all VMs
  start_vm_before_move: true     # start VM before moving disks (default)

  vms:
    - vm_name: "web-server"
      proxmox_vmid: 200
      proxmox_bridges: "vmbr0"
      proxmox_final_storage: "local-lvm"
    - vm_name: "db-server"
      proxmox_vmid: 201
      proxmox_bridges: "vmbr1"  # different bridge
      proxmox_final_storage: "ceph-pool"  # different final storage
      start_vm_before_move: false  # start after disks are moved
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

## Configuration

Steps 11-14 use the QEMU guest agent to run commands inside the Proxmox VM. The following settings control the VirtIO ISO and script paths:

| Setting | CLI flag | Default |
|---|---|---|
| `virtio_iso_storage` | `--virtio-iso-storage` | `local` |
| `virtio_iso_filename` | `--virtio-iso-filename` | `virtio-win-0.1.271-1.iso` |
| `purge_vmware_script` | `--purge-vmware-script` | `C:\TMP\pveMigration\purge-vmware-tools.ps1` |
| `import_nic_script` | `--import-nic-script` | `C:\TMP\pveMigration\importNicConfig.ps1` |
