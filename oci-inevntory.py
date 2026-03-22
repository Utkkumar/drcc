#!/usr/bin/env python3
"""
OCI resource inventory across all subscribed regions ? Excel workbook

Sheets:
- Instances
- VCNs_Subnets
- VolumeBackups
- Databases
"""

import json
import sys
import pandas as pd
import time
import re
import oci
import subprocess


# ---------------------------
# Helper: get all subscribed regions
# ---------------------------
def get_all_regions(config, tenancy_id):
    identity = oci.identity.IdentityClient(config)
    subs = identity.list_region_subscriptions(tenancy_id).data
    return [r.region_name for r in subs]

#Run command Helper#



def _extract_text_output(exec_resp):
    """
    Robustly extract TEXT output from OCI SDK response.
    Handles object attribute and dict-like variants.
    """
    out_obj = getattr(exec_resp, "output", None)

    # Preferred: SDK model object has .text [1](https://docs.oracle.com/en-us/iaas/tools/python/2.143.0/api/compute_instance_agent/models/oci.compute_instance_agent.models.InstanceAgentCommandExecutionOutputViaTextDetails.html)
    if out_obj is not None:
        text = getattr(out_obj, "text", None)
        if text:
            return str(text).strip()

        msg = getattr(out_obj, "message", None)
        if msg:
            return str(msg).strip()

        if isinstance(out_obj, dict):
            if out_obj.get("text"):
                return str(out_obj["text"]).strip()
            if out_obj.get("message"):
                return str(out_obj["message"]).strip()

    return ""






def oci_run_command(cia_client, compartment_id, instance_id, script_text,
                    timeout_sec=300, display_name="AgentStatus"):
    """
    Runs OCI Run Command and returns (state, output_text).

    Key change: After execution finishes, convert the whole response to dict/string
    and search for QUALYS= / MDE= anywhere inside it. This matches what you see
    in the OCI console 'Command details' Output pane.
    """
    command_details = oci.compute_instance_agent.models.CreateInstanceAgentCommandDetails(
        compartment_id=compartment_id,
        execution_time_out_in_seconds=timeout_sec,
        display_name=display_name,
        target=oci.compute_instance_agent.models.InstanceAgentCommandTarget(instance_id=instance_id),
        content=oci.compute_instance_agent.models.InstanceAgentCommandContent(
            source=oci.compute_instance_agent.models.InstanceAgentCommandSourceViaTextDetails(text=script_text),
            output=oci.compute_instance_agent.models.InstanceAgentCommandOutputViaTextDetails(output_type="TEXT")
        )
    )

    create_resp = cia_client.create_instance_agent_command(command_details)
    command_id = create_resp.data.id

    start = time.time()
    grace_after_success_sec = 30
    success_time = None

    while True:
        exec_resp = cia_client.get_instance_agent_command_execution(
            instance_agent_command_id=command_id,
            instance_id=instance_id
        ).data

        state = getattr(exec_resp, "lifecycle_state", "UNKNOWN")

        if state in ("SUCCEEDED", "FAILED", "CANCELED", "CANCELLED"):
            # Convert the entire object to dict then to string
            try:
                raw_dict = oci.util.to_dict(exec_resp)
            except Exception:
                raw_dict = {"repr": repr(exec_resp)}

            raw_text = json.dumps(raw_dict, ensure_ascii=False)

            # Search for QUALYS= and MDE= anywhere
            q = re.search(r"QUALYS\s*=\s*([A-Za-z]+)", raw_text, re.IGNORECASE)
            m = re.search(r"MDE\s*=\s*([A-Za-z]+)", raw_text, re.IGNORECASE)

            # If SUCCEEDED but patterns not found yet, wait a bit (propagation delay)
            if state == "SUCCEEDED" and (not q or not m):
                if success_time is None:
                    success_time = time.time()
                if (time.time() - success_time) < grace_after_success_sec:
                    time.sleep(2)
                    continue

            # Return only the relevant lines if found; else return empty string
            out_lines = []
            if q:
                out_lines.append("QUALYS=" + q.group(1))
            if m:
                out_lines.append("MDE=" + m.group(1))

            return state, "\n".join(out_lines).strip()

        if time.time() - start > timeout_sec:
            return "TIMEOUT", f"Run command timed out. Last state={state}"

        time.sleep(3)




# ---------------------------
# Instances sheet
# ---------------------------
def get_instances(compute, network, plugin_client, block_client, cia_client, comp_id):
    rows = []
    try:
        instances = oci.pagination.list_call_get_all_results(
            compute.list_instances, compartment_id=comp_id
        ).data
    except Exception as e:
        print(f"Error listing instances: {e}")
        return rows

    for inst in instances:
        ocid = inst.id
        name = inst.display_name or ""
        shape = inst.shape or ""
        ocpus = getattr(inst.shape_config, "ocpus", "")
        mem_gb = getattr(inst.shape_config, "memory_in_gbs", "")

        # Private & Public IPs
        private_ips, public_ip_flag = [], "No"
        try:
            attachments = oci.pagination.list_call_get_all_results(
                compute.list_vnic_attachments,
                compartment_id=comp_id,
                instance_id=ocid
            ).data
            for att in attachments:
                vnic = network.get_vnic(att.vnic_id).data
                if vnic.private_ip:
                    private_ips.append(vnic.private_ip)
                if vnic.public_ip:
                    public_ip_flag = "Yes"
        except Exception as e:
            print(f"Error fetching VNICs for {name}: {e}")

       # Boot volume encryption
        boot_enc_flag = "Unknown"
        try:
            boot_atts = oci.pagination.list_call_get_all_results(
                compute.list_boot_volume_attachments,
                availability_domain=inst.availability_domain,
                compartment_id=comp_id,
                instance_id=ocid
            ).data
            for ba in boot_atts:
       #         bv = block_client.get_boot_volume(ba.boot_volume_id).data
                if hasattr(ba, "is_pv_encryption_in_transit_enabled"):
                    boot_enc_flag = "Yes" if ba.is_pv_encryption_in_transit_enabled else "No"
        except Exception as e:
            print(f"Error fetching boot volume for {name}: {e}")


        # Image
        image_name = inst.image_id
        os_type = "Unknown"

        try:
            img = compute.get_image(inst.image_id).data
            image_name = img.display_name
            os_type = img.operating_system  # ? THIS IS THE KEY FIX
        except Exception:
            pass

        # Agent checks
        agent_status = {"Qualys": "Skipped", "MDE": "Skipped"}
        if private_ips:
            if os_type and "windows" in os_type.lower():
                agent_status = check_windows_agents_runcommand(cia_client, comp_id, ocid)
            else:
        # Try each private IP until one works (prevents false SSH errors)
                for ip in private_ips:
                    s = check_linux_agents(ip)
                    agent_status = s
            # if SSH succeeded (not SSH Error), stop
                    if not str(s.get("MDE", "")).startswith("SSH Error"):
                        break

        rows.append({
              "Instance Name": name,
              "Instance OCID": ocid,
              "OS Type": os_type,
              "Shape": shape,
              "OCPU": ocpus,
              "Memory (GB)": mem_gb,
              "Private IPs": " ".join(private_ips),
              "Public IP?": public_ip_flag,
              "Boot Vol Encryption": boot_enc_flag,
              "Image": image_name,
              "Qualys Agent Installed": agent_status["Qualys"],
              "MDE Agent Installed": agent_status["MDE"],
              "Defined Tags": json.dumps(inst.defined_tags or {}),
              "Freeform Tags": json.dumps(inst.freeform_tags or {})
        })

    return rows


# ---------------------------
# VCNs & Subnets sheet
# ---------------------------
def get_vcns_subnets(network, comp_id):
    rows = []
    try:
        vcns = oci.pagination.list_call_get_all_results(
            network.list_vcns, compartment_id=comp_id
        ).data
        for v in vcns:
            rows.append({
                "Type": "VCN",
                "Name": v.display_name,
                "OCID": v.id,
                "CIDR": ",".join(v.cidr_blocks),
                "Lifecycle": v.lifecycle_state
            })
        subnets = oci.pagination.list_call_get_all_results(
            network.list_subnets, compartment_id=comp_id
        ).data
        for s in subnets:
            rows.append({
                "Type": "Subnet",
                "Name": s.display_name,
                "OCID": s.id,
                "CIDR": s.cidr_block,
                "VCN OCID": s.vcn_id,
                "Lifecycle": s.lifecycle_state
            })
    except Exception as e:
        print(f"Error fetching VCNs/Subnets: {e}")
    return rows


# ---------------------------
# Volume backups sheet
# ---------------------------
def get_volume_backups(block_client, comp_id):
    rows = []
    try:
        boot_baks = oci.pagination.list_call_get_all_results(
            block_client.list_boot_volume_backups, compartment_id=comp_id
        ).data
        for b in boot_baks:
            rows.append({
                "Type": "BootVolumeBackup",
                "Name": b.display_name,
                "OCID": b.id,
                "Source Volume": b.boot_volume_id,
                "Time Created": str(b.time_created),
                "Expiration": str(b.expiration_time)
            })
        vol_baks = oci.pagination.list_call_get_all_results(
            block_client.list_volume_backups, compartment_id=comp_id
        ).data
        for v in vol_baks:
            rows.append({
                "Type": "BlockVolumeBackup",
                "Name": v.display_name,
                "OCID": v.id,
                "Source Volume": v.volume_id,
                "Time Created": str(v.time_created),
                "Expiration": str(v.expiration_time)
            })
    except Exception as e:
        print(f"Error fetching backups: {e}")
    return rows


# ---------------------------
# Databases sheet
# ---------------------------
def get_databases(db_client, comp_id):
    rows = []
    try:
        adbs = oci.pagination.list_call_get_all_results(
            db_client.list_autonomous_databases, compartment_id=comp_id
        ).data
        for adb in adbs:
            rows.append({
                "Type": "AutonomousDB",
                "Name": adb.db_name,
                "OCID": adb.id,
                "CPU": adb.cpu_core_count,
                "Storage": adb.data_storage_size_in_tbs,
                "Lifecycle": adb.lifecycle_state
            })
        dbs = oci.pagination.list_call_get_all_results(
            db_client.list_db_systems, compartment_id=comp_id
        ).data
        for db in dbs:
            rows.append({
                "Type": "DBSystem",
                "Name": db.display_name,
                "OCID": db.id,
                "Shape": db.shape,
                "Lifecycle": db.lifecycle_state
            })
        exas = oci.pagination.list_call_get_all_results(
            db_client.list_cloud_exadata_infrastructures, compartment_id=comp_id
        ).data
        for exa in exas:
            rows.append({
                "Type": "ExadataInfra",
                "Name": exa.display_name,
                "OCID": exa.id,
                "Shape": exa.shape,
                "Lifecycle": exa.lifecycle_state
            })
    except Exception as e:
        print(f"Error fetching DBs: {e}")
    return rows

def run_ssh_command(ip, command, user="opc", timeout=15):
    """
    Runs a remote command via system OpenSSH client.
    Uses legacy options compatible with older ssh versions.
    """
    ssh_cmd = [
        "ssh",
        "-q",  # suppress banners like 'Permanently added...'
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={timeout}",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        # Allow ssh-rsa for older estates (client-side)
        "-o", "HostKeyAlgorithms=+ssh-rsa",
        "-o", "PubkeyAcceptedKeyTypes=+ssh-rsa",  # old OpenSSH name
        f"{user}@{ip}",
        command
    ]

    p = subprocess.run(
        ssh_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True
    )
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def check_linux_agents(ip):
    result = {"Qualys": "Unknown", "MDE": "Unknown"}

    # ---- MDE check (absolute path avoids PATH issues on non-interactive SSH) ----
    rc, out, err = run_ssh_command(ip, "/bin/systemctl is-active mdatp 2>/dev/null")
    if rc == 0 and out.strip() == "active":
        result["MDE"] = "Yes"
    elif rc == 0:
        result["MDE"] = "No"
    else:
        # If SSH fails, return error for both (so you can troubleshoot reachability)
        result["Qualys"] = f"SSH Error: {err or 'failed'}"
        result["MDE"] = f"SSH Error: {err or 'failed'}"
        return result

    # ---- Qualys check (use pgrep to avoid grep false positives) ----
    rc, out, err = run_ssh_command(ip, "pgrep -fa 'qualys-cloud-agent|qualys-cep' | head -n 1")
    if rc == 0 and out.strip():
        result["Qualys"] = "Yes"
    else:
        result["Qualys"] = "No"

    return result



def check_windows_agents_runcommand(cia_client, comp_id, instance_id):
    result = {"Qualys": "Unknown", "MDE": "Unknown"}

    ps = r"""#ps1
$ErrorActionPreference = "SilentlyContinue"

function Get-StateFromSC($serviceName) {
    $out = sc.exe query $serviceName 2>$null
    if (-not $out) { return "NotInstalled" }

    $state = ($out | Select-String "STATE").Line
    if ($state -match "RUNNING") { return "Running" }
    if ($state -match "STOPPED") { return "Stopped" }

    return "Unknown"
}

# ---- MDE ----
$mde = Get-StateFromSC "Sense"

# ---- Qualys (service names vary, so check by name match) ----
$qualysState = "NotInstalled"
$qualysServices = sc.exe query type= service state= all | Select-String -Pattern "Qualys"

if ($qualysServices) {
    $qualysState = "Running"
}

Write-Output "QUALYS=$qualysState"
Write-Output "MDE=$mde"

"""

    state, out = oci_run_command(
        cia_client,
        compartment_id=comp_id,
        instance_id=instance_id,
        script_text=ps,
        timeout_sec=800,             # ? increased for Windows
        display_name="QualysMDEStatus"
    )

    if state != "SUCCEEDED":
        result["Qualys"] = f"RunCmd {state}"
        result["MDE"] = f"RunCmd {state}"
        return result

    def normalize(v):
        v = (v or "").strip()
        if v.lower() == "running":
            return "Yes"
        if v.lower() in ("stopped", "notinstalled", "notfound"):
            return "No"
        if v == "":
            return "Unknown"
        return v

    for line in out.splitlines():
        line = line.strip()
        if line.startswith("QUALYS="):
            result["Qualys"] = normalize(line.split("=", 1)[1])
        elif line.startswith("MDE="):
            result["MDE"] = normalize(line.split("=", 1)[1])

    return result



# ---------------------------
# OSMH Updates sheet (Pending updates per managed instance)
# ---------------------------
def get_osmh_updates(osmh_client, comp_id):
    rows = []
    try:
        managed_instances = oci.pagination.list_call_get_all_results(
            osmh_client.list_managed_instances,
            compartment_id=comp_id
        ).data
    except Exception as e:
        print(f"Error listing OSMH managed instances: {e}")
        return rows

    for mi in managed_instances:
        mi_id = mi.id
        mi_name = getattr(mi, "display_name", "") or ""
        os_name = getattr(mi, "os_name", "")
        os_ver  = getattr(mi, "os_version", "")
        status  = getattr(mi, "status", "")

        # Updatable packages = pending updates/patches
        try:
            upd_resp = oci.pagination.list_call_get_all_results(
                osmh_client.list_managed_instance_updatable_packages,
                managed_instance_id=mi_id
                # Optional filters (uncomment if you want only SECURITY):
                # classification_type=["SECURITY"]
            ).data
            upd_items = getattr(upd_resp, "items", upd_resp)
        except Exception as e:
            rows.append({
                "Managed Instance Name": mi_name,
                "Managed Instance OCID": mi_id,
                "OS Name": os_name,
                "OS Version": os_ver,
                "Status": status,
                "Update Package": "",
                "Installed Version": "",
                "Available Version": "",
                "Update Type": "",
                "Classification": "",
                "Errata": "",
                "Related CVEs": "",
                "Error": str(e)
            })
            continue

        # If no updates, still write a row so the sheet shows the instance is "clean"
        if not upd_items:
            rows.append({
                "Managed Instance Name": mi_name,
                "Managed Instance OCID": mi_id,
                "OS Name": os_name,
                "OS Version": os_ver,
                "Status": status,
                "Update Package": "",
                "Installed Version": "",
                "Available Version": "",
                "Update Type": "",
                "Classification": "",
                "Errata": "",
                "Related CVEs": "",
                "Error": ""
            })
            continue

        # One row per pending package update
        for p in upd_items:
            rows.append({
                "Managed Instance Name": mi_name,
                "Managed Instance OCID": mi_id,
                "OS Name": os_name,
                "OS Version": os_ver,
                "Status": status,
                "Update Package": getattr(p, "display_name", "") or getattr(p, "name", ""),
                "Installed Version": getattr(p, "installed_version", ""),
                "Available Version": getattr(p, "version", ""),
                "Update Type": getattr(p, "update_type", ""),
                "Classification": getattr(p, "package_classification", ""),
                "Errata": ", ".join(getattr(p, "errata", []) or []),
                "Related CVEs": ", ".join(getattr(p, "related_cves", []) or []),
                "Error": ""
            })

    return rows
    
# ---------------------------
# OSMH Summary sheet (NEW)
# ---------------------------
def build_osmh_summary(osmh_rows):
    if not osmh_rows:
        return []

    df = pd.DataFrame(osmh_rows)

    # --- Normalize column names (handle variations) ---
    # Pick the CVE column if present, else create it empty
    possible_cve_cols = ["CVEs", "CVE", "Related CVEs", "Related CVE", "related_cves"]
    cve_col = next((c for c in possible_cve_cols if c in df.columns), None)
    if cve_col is None:
        df["CVEs"] = ""
    else:
        df["CVEs"] = df[cve_col].fillna("").astype(str)

    # Pick classification column if present, else empty
    possible_class_cols = ["Classification", "package_classification", "classification_type"]
    class_col = next((c for c in possible_class_cols if c in df.columns), None)
    if class_col is None:
        df["Classification"] = ""
    else:
        df["Classification"] = df[class_col].fillna("").astype(str)

    # Pick update package column if present, else empty
    possible_pkg_cols = ["Update Package", "Package", "display_name", "name"]
    pkg_col = next((c for c in possible_pkg_cols if c in df.columns), None)
    if pkg_col is None:
        df["Update Package"] = ""
    else:
        df["Update Package"] = df[pkg_col].fillna("").astype(str)

    # Ensure these exist (your grouping keys)
    for col in ["Region", "Managed Instance Name", "OS Name", "OS Version"]:
        if col not in df.columns:
            df[col] = ""

    # --- Derivations ---
    df["Has Update"] = df["Update Package"].astype(str).str.len() > 0
    df["Has Security Update"] = df["Classification"].str.upper().str.contains("SECURITY", na=False)
    df["Has CVE"] = df["CVEs"].astype(str).str.len() > 0

    summary = (
        df.groupby(["Region", "Managed Instance Name", "OS Name", "OS Version"], as_index=False)
          .agg(
              Pending_Updates=("Has Update", "sum"),
              Security_Updates=("Has Security Update", "sum"),
              CVE_Rows=("Has CVE", "sum")
          )
    )

    def risk(row):
        if row["Security_Updates"] > 0 and row["CVE_Rows"] > 0:
            return "CRITICAL"
        if row["Pending_Updates"] > 0:
            return "MEDIUM"
        return "CLEAN"

    summary["Patch Risk"] = summary.apply(risk, axis=1)
    return summary.to_dict(orient="records")   

# ---------------------------
# Main
# ---------------------------
def main():
    print("Starting OCI inventory collection...")

    comp_id = input("Enter Compartment OCID: ").strip()
    if not comp_id:
        print("Compartment OCID is required.", file=sys.stderr)
        sys.exit(1)

    out_file = input("Enter output Excel filename (default oci_inventory.xlsx): ").strip()
    if not out_file:
        out_file = "oci_inventory.xlsx"
    # Prompt for Windows ociadmin password once
   # win_password = getpass.getpass("Enter Windows ociadmin password: ")


    try:
        config = oci.config.from_file("~/.oci/config", profile_name="DEFAULT")
    except Exception as e:
        print(f"Failed to load OCI config: {e}", file=sys.stderr)
        sys.exit(1)

    tenancy_id = config.get("tenancy")
    regions = get_all_regions(config, tenancy_id)

    all_instances, all_vcns_subnets, all_backups, all_dbs, all_osmh, all_osmh_summary = [], [], [], [], [], []

    for region in regions:
        print(f"Processing region: {region}")
        config["region"] = region
        compute = oci.core.ComputeClient(config)
        network = oci.core.VirtualNetworkClient(config)
        plugin_client = oci.compute_instance_agent.PluginClient(config)
        cia_client = oci.compute_instance_agent.ComputeInstanceAgentClient(config)
        block_client = oci.core.BlockstorageClient(config)
        db_client = oci.database.DatabaseClient(config)
        osmh_client = oci.os_management_hub.ManagedInstanceClient(config)


        inst_rows = get_instances(compute, network, plugin_client, block_client, cia_client, comp_id)
        print(f"  Found {len(inst_rows)} instances")
        for row in inst_rows:
            row["Region"] = region
            all_instances.append(row)

        vcn_rows = get_vcns_subnets(network, comp_id)
        print(f"  Found {len(vcn_rows)} VCN/Subnet entries")
        for row in vcn_rows:
            row["Region"] = region
            all_vcns_subnets.append(row)

        bak_rows = get_volume_backups(block_client, comp_id)
        print(f"  Found {len(bak_rows)} backups")
        for row in bak_rows:
            row["Region"] = region
            all_backups.append(row)

        db_rows = get_databases(db_client, comp_id)
        print(f"  Found {len(db_rows)} databases")
        for row in db_rows:
            row["Region"] = region
            all_dbs.append(row)
            
        osmh_rows = get_osmh_updates(osmh_client, comp_id)
        print(f"  Found {len(osmh_rows)} OSMH update rows")
        for row in osmh_rows:
            row["Region"] = region
            all_osmh.append(row)
            
    all_osmh_summary = build_osmh_summary(all_osmh)


      # Write to Excel
    with pd.ExcelWriter(out_file, engine="openpyxl") as writer:
        pd.DataFrame(all_instances).to_excel(writer, sheet_name="Instances", index=False)
        pd.DataFrame(all_vcns_subnets).to_excel(writer, sheet_name="VCNs_Subnets", index=False)
        pd.DataFrame(all_backups).to_excel(writer, sheet_name="VolumeBackups", index=False)
        pd.DataFrame(all_dbs).to_excel(writer, sheet_name="Databases", index=False)
        pd.DataFrame(all_osmh).to_excel(writer, sheet_name="OSMH_Updates", index=False)
        pd.DataFrame(all_osmh_summary).to_excel(writer, "OSMH_Summary", index=False)

    print(f"Exported {len(all_instances)} instances, "
          f"{len(all_vcns_subnets)} VCN/Subnets, "
          f"{len(all_backups)} backups, "
          f"{len(all_dbs)} databases "
          f"across {len(regions)} regions to {out_file}")


if __name__ == "__main__":
    main()
