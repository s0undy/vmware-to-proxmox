<p align="center">
  <img src="https://github.com/s0undy/vmware-to-proxmox/blob/main/logo.png" alt="lmao"/>
</p>

<h1 align="center">Automate your VMware to Proxmox migration</h1>

Automates the [Attach Disk & Move Disk (minimal downtime)](https://pve.proxmox.com/wiki/Migrate_to_Proxmox_VE#Attach_Disk_&_Move_Disk_(minimal_downtime)) migration method from the Proxmox wiki. Optionally uses [NetApp Shift Toolkit](https://docs.netapp.com/us-en/netapp-solutions-virtualization/migration/shift-toolkit-overview.html#toolkit-overview) for disk conversion, which can reduce migration time by up to 99% on large VMs.

The base method has been used to migrate 80+ VMs from VMware to Proxmox, ranging from 20GB to 5TB in size. The NetApp Shift–based version has been used so far to migrate 30+ VMs, ranging from 20GB to 5TB.

## Migration steps

The migration is done in 15 distinct steps.

**Base method:**

1. Storage vMotion VM to a shared datastore
2. VM creation in Proxmox
3. NIC configuration export
4. Enablement of VirtIO SCSI boot driver
5. Installation of VirtIO guest tools
6. Shutdown of the VM
7. Rewrite VMDK descriptors on the Proxmox node to prepare for disk conversion
8. Start VM in Proxmox
9. Move disks to final storage (this does the VMDK to qcow2)
10. Import converted disks
11. Verify VM is running on final storage
12. Install VirtIO drivers from ISO via QEMU guest agent
13. Purge VMware Tools
14. Restore NIC configuration
15. Enable NICs and do a final reboot

**NetApp Shift method** (steps 7–10 replaced):

1. Storage vMotion VM to a shared datastore
2. VM creation in Proxmox
3. NIC configuration export
4. Enablement of VirtIO SCSI boot driver
5. Installation of VirtIO guest tools
6. Shutdown of the VM
7. Create Resource group for the VM in NetApp Shift
8. Create Blueprint referencing the Resource group
9. Trigger a disk conversion using the Blueprint and wait for it to finish
10. Move converted QCOW2 disks into correct VM directory, if VM uses OVMF create a efidisk and boot up the VM.
11. Verify VM is running on final storage
12. Install VirtIO drivers from ISO via QEMU guest agent
13. Purge VMware Tools
14. Restore NIC configuration
15. Enable NICs and do a final reboot

## Requirements

- Python 3.10+ on the host running the migration
- VMware Tools running on the guest to be migrated
- VirtIO drivers and guest tools staged in the guest (`C:\TMP\pveMigration\`)
- PowerShell scripts (`exportNicConfig.ps1`, `enable-vioscsi-to-load-on-boot.ps1`, `importNicConfig.ps1`, `purge-vmware-tools.ps1`) in the same directory
- VirtIO ISO uploaded to Proxmox storage
- The same NFS volume mounted on both Proxmox and in vCenter (can be a temporary migration volume)
- A "final" destination volume in Proxmox

### Additional requirements for NetApp Shift

- A server running [NetApp Shift Toolkit](https://docs.netapp.com/us-en/netapp-solutions-virtualization/migration/shift-toolkit-install-prepare.html#before-you-begin) that has access to the PVE API, vCenter and ONTAP API
- vCenter setup as a source site inside Shift
- All ONTAP arrays that will be used added as storage to vCenter inside Shift
- KVM (Conversion) setup as a source destination inside Shift
- NFS volume mounted on Proxmox
- The same NFS volume mounted inside vCenter

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
```

Edit `config.yaml` with your credentials and VM info.

## Usage

```bash
python migrate.py --config config.yaml
```

All settings in `config.yaml` can be overridden with CLI flags. Run `python migrate.py --help` for the full list.

| Flag | Purpose |
|---|---|
| `--dry-run` | Log actions without making changes |
| `--skip-to N` | Resume from step N (1-15) |
| `--parallel` | Migrate all VMs concurrently |
| `--verbose` | Debug-level logging |

### NIC boot mode

By default, NICs are created with `link_down=1` and enabled in step 15. This prevents odd things from happening during the migration.

Set `enable_nics_on_boot: true` (or `--enable-nics-on-boot`) to create NICs with link enabled from the start. This halves all boot wait timers in steps 8–15, significantly reducing total migration time for domain-joined servers that would otherwise wait for domain controller timeouts.

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
      cpu_type: "x86-64-v2-AES"
      max_cores: 4
      enable_nics_on_boot: true
      enable_ha: true
```

Sequential by default. Use `--parallel` for concurrent migration.

## Known issues

- See [Issues](https://github.com/s0undy/vmware-to-proxmox/issues)

## Credits

- `enable-vioscsi-to-load-on-boot.ps1` — [croit/derhanns](https://github.com/croit/load-virtio-scsi-on-boot)
- `importNicConfig.ps1` & `exportNicConfigs.ps1` — [lucavornheder](https://forum.proxmox.com/threads/netzwerksettings-bei-der-migration-von-windows-vms-zu-pve-%C3%BCbernehmen.175997/)
- `purge-vmware-tools.ps1` — [community gist](https://gist.github.com/broestls/f872872a00acee2fca02017160840624)
- NetApp for their Shift Toolkit and examples at [shift-api-automation](https://github.com/NetApp/shift-api-automation)

## Disclaimer

This project was built with assistance from AI. AI makes mistakes — and so do humans. Always have working backups before migrating and verify they work. Do a trial run with `--dry-run` and `--skip-to` before running on production workloads.

YMMV
