<p align="center">
  <img src="https://github.com/s0undy/vmware-to-proxmox/blob/main/logo.png" alt="lmao"/>
</p>

<h1 align="center">Automate your VMware to Proxmox migration</h1>

Migrating from VMware to Proxmox VE doesn't have to be a big hassle. There are multiple ways to [migrate to Proxmox VE](https://pve.proxmox.com/wiki/Migrate_to_Proxmox_VE) and each comes with its own advantages and disadvantages. When researching how to migrate, I drew the conclusion that [Attach Disk & Move Disk (minimal downtime)](https://pve.proxmox.com/wiki/Migrate_to_Proxmox_VE#Attach_Disk_&_Move_Disk_(minimal_downtime)) is the best way to migrate. It's rather quick and it also allows you to keep a fully working copy inside your VMware cluster just in case something goes wrong during the migration.

However the method described in the Proxmox wiki is the manual way, and involves quite a few steps that the administrator has to do in order to migrate just one VM. It allows for the human behind the levers to make critical mistakes that could result in a failed migration. Also when doing large migrations of hundreds of VMs you generally don't want to have this manual approach.

The code inside of this repo implements the [Attach Disk & Move Disk (minimal downtime)](https://pve.proxmox.com/wiki/Migrate_to_Proxmox_VE#Attach_Disk_&_Move_Disk_(minimal_downtime)) method from the Proxmox wiki as a base that can be used to migrate off VMware provided you have access to a shared NFS storage. Although I tried to keep it neutral I realise that the approach is highly opinionated and might not suit every environment. Feel free to clone and adapt the code to your needs.

The base method has been used to migrate 80+ VMs from VMware to Proxmox, ranging from 20GB to 5TB in size.

### Notes about NetApp
For those of you that have access to a NetApp Filer(NFS) I have good news. Utilizing [NetApp Shift Toolkit](https://docs.netapp.com/us-en/netapp-solutions-virtualization/migration/shift-toolkit-overview.html#toolkit-overview) it is possible to reduce the migration time with by up to 99%. As an example migrating a 100GB VM using the base method would take about 12 minutes, with most of the time spent converting the disk. Using Shift to do the disk conversion the same migration only takes about 5 minutes. This is even more impactful for large VMs as a multi TB VM could take hours to convert. Using Shift it only takes about 1 minute to convert a 1TB VM from VMDK to QCOW2.

NetApp have also created a full [Migrate VMs from VMware to Proxmox VE](https://docs.netapp.com/us-en/netapp-solutions-virtualization/migration/shift-toolkit-migrate-esxi2proxmox.html) that does everything for you. As of writing this on 2026-04-12 I would advise against using it. It doesn't allow you to adjust many of the settings you want to set during the creation of the VM in Proxmox. As an example it doesn't support networks created by SDN, and it sets the machine type to i440fx making it a hassle to change after the VM has booted.

I decided to use the Shift Toolkit to convert the VMDK to QCOW, eliminating much of the time as well as having close to zero impact on the storage system's performance during the migration. It was then easy to use the already written plumbing to finalize the migration.

## Migration steps

The migration is done in 15 distinct steps. When using NetApp Shift backend steps 7-10 are replaced by NetApp Shift–specific steps that handle the disk conversion.

1. Storage vMotion VM to a shared datastore
2. VM creation in Proxmox
3. NIC configuration export
4. Enablement of VirtIO SCSI boot driver 
5. Installation of VirtIO guest tools
6. Shutdown of the VM
7. Rewrite VMDK descriptors on the Proxmox node to prepare for disk conversion
8. Start VM in Proxmox
9. Move disks to final storage (this does the VMDK to qcow2)
10. Import converted disks (this happens with NetApp Shift backend only)
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

Set `enable_nics_on_boot: true` (or `--enable-nics-on-boot`) to create NICs with link enabled from the start. This halves all boot wait timers in steps 8-15, significantly reducing total migration time for domain-joined servers that would otherwise wait for domain controller timeouts before allowing actions to be made on them.

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

# Known issues

- See [Issues](https://github.com/s0undy/vmware-to-proxmox/issues)
- When using the base method high random write I/O can be observed during the disk move/conversion from VMDK to QCOW2 on the final storage. Depending on what storage you are using this MIGHT cause latency. This seems to be a problem on NFSv3, works better on NFSv4.2 for reasons I cannot explain.

# Credits
The content in this folder has been heavily inspired by different community members. I want to say thank you for making the initial scripts public. They have been modified to fit the workflow of this project.

- enable-vioscsi-to-load-on-boot.ps1 | Credits to [croit/derhanns](https://github.com/croit/load-virtio-scsi-on-boot)
- importNicConfig.ps1 & exportNicConfigs.ps1 | Credits to [lucavornheder](https://forum.proxmox.com/threads/netzwerksettings-bei-der-migration-von-windows-vms-zu-pve-%C3%BCbernehmen.175997/)
- purge-vmware-tools.ps1 | Credits to all the people over at this [gist](https://gist.github.com/broestls/f872872a00acee2fca02017160840624)
- NetApp for their Shift Toolkit as well as the examples over at [shift-api-automation](https://github.com/NetApp/shift-api-automation) (Worth mentioning that these are not updated and I had to reverse engineer parts of the API in order to get it working...)


# This project was built with assistance from AI (Claude).
All code has been read by a human. AI makes mistakes... and so do humans. If you intend to use this in a production environment please do your own code review(by human hands).
