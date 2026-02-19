# vmware-to-proxmox

Migrates Windows VMs from VMware vCenter to Proxmox VE. Connects to both platforms and automates every step so the VM boots cleanly on its new host.

## Migration steps

1. Storage vMotion VM to a shared datastore
2. Create Proxmox VM shell (CPU, RAM, disks, NICs)
3. Export NIC configuration inside the guest
4. Enable VirtIO SCSI boot driver
5. Install VirtIO guest tools
6. Shut down the VM
7. Rewrite VMDK descriptors on the Proxmox node
8. Start VM in Proxmox
9. Move disks to final storage (VMDK to qcow2)
10. Verify VM is running on final storage
11. Install VirtIO drivers from ISO via QEMU guest agent
12. Purge VMware Tools
13. Restore NIC configuration
14. Enable NICs and final reboot

## Requirements

- Python 3.10+
- VMware Tools running in the guest
- VirtIO drivers and guest tools staged in the guest (`C:\TMP\pveMigration\`)
- PowerShell scripts (`exportNicConfig.ps1`, `enable-vioscsi-to-load-on-boot.ps1`, `importNicConfig.ps1`, `purge-vmware-tools.ps1`) in the same directory
- VirtIO ISO uploaded to Proxmox storage (default: `local`)
- QEMU guest agent installed in the guest (steps 11-14)

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
```

Edit `config.yaml` with your credentials. Passwords can also be set via `VCENTER_PASSWORD`, `PROXMOX_PASSWORD`, `GUEST_PASSWORD` environment variables.

## Usage

```bash
python migrate.py
```

All settings in `config.yaml` can be overridden with CLI flags. Run `python migrate.py --help` for the full list.

| Flag | Purpose |
|---|---|
| `--dry-run` | Log actions without making changes |
| `--skip-to N` | Resume from step N (1-14) |
| `--parallel` | Migrate all VMs concurrently |
| `--enable-nics-on-boot` | Boot with NICs enabled (halves wait timers, faster for domain-joined VMs) |
| `--verbose` | Debug-level logging |

### NIC boot mode

By default, NICs are created with `link_down=1` and enabled in step 14. This prevents domain authentication issues during driver installation.

Set `enable_nics_on_boot: true` (or `--enable-nics-on-boot`) to create NICs with link enabled from the start. This halves all boot wait timers in steps 8-14, significantly reducing total migration time for domain-joined servers that would otherwise wait for domain controller timeouts.

## Multiple VMs

Add a `vms:` list under `migration:` in your config. Shared settings go at the top; any field can be overridden per VM.

```yaml
migration:
  migration_datastore: "shared-ds"
  proxmox_storage: "migration-nfs"
  proxmox_final_storage: "local-lvm"
  proxmox_bridges: "vmbr0"
  enable_nics_on_boot: false

  vms:
    - vm_name: "web-server"
      proxmox_vmid: 200
    - vm_name: "db-server"
      proxmox_vmid: 201
      proxmox_bridges: "vmbr1"
      proxmox_final_storage: "ceph-pool"
      max_cores: 4
      enable_nics_on_boot: true
```

Sequential by default. Use `--parallel` for concurrent migration.

A single VM via CLI: `python migrate.py --vm-name "my-vm"`

## Configuration

| Setting | CLI flag | Default |
|---|---|---|
| `virtio_iso_storage` | `--virtio-iso-storage` | `local` |
| `virtio_iso_filename` | `--virtio-iso-filename` | `virtio-win-0.1.271-1.iso` |
| `purge_vmware_script` | `--purge-vmware-script` | `C:\TMP\pveMigration\purge-vmware-tools.ps1` |
| `import_nic_script` | `--import-nic-script` | `C:\TMP\pveMigration\importNicConfig.ps1` |
| `start_vm_before_move` | `--start-vm-before-move` | `true` |
| `enable_nics_on_boot` | `--enable-nics-on-boot` | `false` |

---

This project was built with assistance from AI (Claude).

