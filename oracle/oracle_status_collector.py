#!/usr/bin/env python3
"""
Oracle Status Collector
=======================
Connects to every database listed in databases.json, collects
  - instance / open mode / Data Guard role
  - database size (allocated, used, free, max with autoextend)
  - tablespace usage
  - FRA (fast recovery area) usage
  - RMAN backup status (last full / incremental / archivelog + 14-day history)
  - standby status (transport / apply lag, MRP, archive gap)
and pushes the result to the Status Dashboard GAS web app (action=pushOracleStatus).

Usage:
  pip install oracledb requests
  python oracle_status_collector.py                 # collect + push
  python oracle_status_collector.py --dry-run       # collect + print JSON only
  python oracle_status_collector.py --out out.json  # collect + save JSON

Run it every 15 minutes from cron / Task Scheduler.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta

import oracledb

try:
    import requests
except ImportError:  # only needed for push
    requests = None

HERE = os.path.dirname(os.path.abspath(__file__))

DEFAULT_THRESHOLDS = {
    "tablespaceWarnPct": 85,
    "tablespaceCritPct": 95,
    "fraWarnPct": 80,
    "fraCritPct": 90,
    "fullBackupWarnHours": 192,   # 8 days
    "fullBackupCritHours": 336,   # 14 days
    "incrBackupWarnHours": 36,
    "archBackupWarnHours": 6,
    "archBackupCritHours": 24,
    "lagWarnSec": 300,
    "lagCritSec": 1800,
}

AUTH_MODES = {
    "SYSDBA": oracledb.AUTH_MODE_SYSDBA,
    "SYSDG": oracledb.AUTH_MODE_SYSDG,
    "SYSBACKUP": oracledb.AUTH_MODE_SYSBACKUP,
}

GB = 1024 ** 3


# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════

def ts(dt):
    """datetime -> YYMMDD.HHmmss (same format the dashboard already uses)."""
    return dt.strftime("%y%m%d.%H%M%S") if dt else ""


def gb(v):
    return round(float(v or 0) / GB, 2)


def hours_since(dt):
    if not dt:
        return None
    return round((datetime.now() - dt).total_seconds() / 3600, 1)


def interval_to_sec(s):
    """'+00 00:05:12' (v$dataguard_stats) -> 312."""
    if not s:
        return None
    s = s.strip().lstrip("+")
    try:
        days, hms = s.split(" ")
        h, m, sec = hms.split(":")
        return int(days) * 86400 + int(h) * 3600 + int(m) * 60 + int(float(sec))
    except ValueError:
        return None


def fmt_sec(s):
    if s < 60:
        return "%ds" % s
    if s < 3600:
        return "%dm%02ds" % (s // 60, s % 60)
    return "%dh%02dm" % (s // 3600, s % 3600 // 60)


def rows(cur, sql, **binds):
    cur.execute(sql, binds)
    cols = [c[0].lower() for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def one(cur, sql, **binds):
    r = rows(cur, sql, **binds)
    return r[0] if r else None


# ═══════════════════════════════════════════════════════════════════
# Collectors (each one is isolated so a failure only skips that part)
# ═══════════════════════════════════════════════════════════════════

def collect_instance(cur, out):
    db = one(cur, """
        SELECT name, db_unique_name, database_role, open_mode, log_mode,
               force_logging, protection_mode, switchover_status
          FROM v$database""")
    inst = one(cur, """
        SELECT instance_name, host_name, version, status, startup_time
          FROM v$instance""")
    out.update({
        "dbName": db["name"],
        "dbUniqueName": db["db_unique_name"],
        "role": db["database_role"],
        "openMode": db["open_mode"],
        "logMode": db["log_mode"],
        "forceLogging": db["force_logging"],
        "protectionMode": db["protection_mode"],
        "switchoverStatus": db["switchover_status"],
        "instanceName": inst["instance_name"],
        "hostName": inst["host_name"],
        "version": inst["version"],
        "instanceStatus": inst["status"],
        "startupTime": ts(inst["startup_time"]),
    })


def collect_size(cur, out):
    # v$ views work on a MOUNTED standby too
    r = one(cur, """
        SELECT (SELECT NVL(SUM(bytes),0) FROM v$datafile) data_bytes,
               (SELECT NVL(SUM(bytes),0) FROM v$tempfile) temp_bytes,
               (SELECT NVL(SUM(bytes*members),0) FROM v$log) redo_bytes
          FROM dual""")
    out["size"] = {
        "dataGB": gb(r["data_bytes"]),
        "tempGB": gb(r["temp_bytes"]),
        "redoGB": gb(r["redo_bytes"]),
        "allocatedGB": gb(r["data_bytes"] + r["temp_bytes"] + r["redo_bytes"]),
    }


def collect_tablespaces(cur, out):
    # Needs an OPEN database (primary or Active Data Guard)
    ts_rows = rows(cur, """
        SELECT m.tablespace_name                         name,
               t.contents                                contents,
               m.used_space * t.block_size               used_bytes,
               m.tablespace_size * t.block_size          max_bytes,
               ROUND(m.used_percent, 1)                  pct_used,
               NVL(a.alloc_bytes, 0)                     alloc_bytes
          FROM dba_tablespace_usage_metrics m
          JOIN dba_tablespaces t ON t.tablespace_name = m.tablespace_name
          LEFT JOIN (SELECT tablespace_name, SUM(bytes) alloc_bytes FROM dba_data_files GROUP BY tablespace_name
                     UNION ALL
                     SELECT tablespace_name, SUM(bytes) FROM dba_temp_files GROUP BY tablespace_name) a
            ON a.tablespace_name = m.tablespace_name
         ORDER BY m.used_percent DESC""")
    tbs = []
    used_total = 0
    max_total = 0
    for t in ts_rows:
        tbs.append({
            "name": t["name"],
            "contents": t["contents"],
            "usedGB": gb(t["used_bytes"]),
            "allocatedGB": gb(t["alloc_bytes"]),
            "maxGB": gb(t["max_bytes"]),
            "pctUsed": float(t["pct_used"] or 0),
        })
        if t["contents"] != "TEMPORARY":
            used_total += t["used_bytes"] or 0
            max_total += t["max_bytes"] or 0
    out["tablespaces"] = tbs
    out["size"]["usedGB"] = gb(used_total)
    out["size"]["maxGB"] = gb(max_total)
    out["size"]["freeGB"] = round(out["size"]["dataGB"] - out["size"]["usedGB"], 2)
    out["size"]["pctUsed"] = round(used_total * 100.0 / max_total, 1) if max_total else None
    out["maxTablespacePct"] = max([t["pctUsed"] for t in tbs if t["contents"] != "TEMPORARY"] or [0])


def collect_fra(cur, out):
    r = one(cur, """
        SELECT name, space_limit, space_used, space_reclaimable, number_of_files
          FROM v$recovery_file_dest""")
    if not r or not r["space_limit"]:
        out["fra"] = None
        return
    pct = (r["space_used"] - r["space_reclaimable"]) * 100.0 / r["space_limit"]
    out["fra"] = {
        "dest": r["name"],
        "sizeGB": gb(r["space_limit"]),
        "usedGB": gb(r["space_used"]),
        "reclaimableGB": gb(r["space_reclaimable"]),
        "files": r["number_of_files"],
        "pctUsed": round(pct, 1),
    }


def collect_backups(cur, out):
    hist = rows(cur, """
        SELECT session_key, input_type, status, start_time, end_time,
               input_bytes, output_bytes, elapsed_seconds, output_device_type
          FROM v$rman_backup_job_details
         WHERE start_time > SYSDATE - 14
         ORDER BY start_time DESC""")
    out["backups"] = [{
        "type": h["input_type"],
        "status": h["status"],
        "start": ts(h["start_time"]),
        "end": ts(h["end_time"]),
        "inputGB": gb(h["input_bytes"]),
        "outputGB": gb(h["output_bytes"]),
        "durationMin": round((h["elapsed_seconds"] or 0) / 60, 1),
        "device": h["output_device_type"],
    } for h in hist[:50]]

    # last *successful* backup per type (looks further back than 14 days)
    last = rows(cur, """
        SELECT input_type, MAX(end_time) end_time
          FROM v$rman_backup_job_details
         WHERE status IN ('COMPLETED', 'COMPLETED WITH WARNINGS')
         GROUP BY input_type""")
    by_type = {r["input_type"]: r["end_time"] for r in last}
    full = max([d for k, d in by_type.items() if k in ("DB FULL", "DATAFILE FULL")] or [None],
               key=lambda d: d or datetime.min)
    incr = by_type.get("DB INCR")
    arch = by_type.get("ARCHIVELOG")
    out["backup"] = {
        "lastFull": ts(full),
        "lastIncr": ts(incr),
        "lastArch": ts(arch),
        "fullAgeH": hours_since(full),
        "incrAgeH": hours_since(incr),
        "archAgeH": hours_since(arch),
        "lastStatus": hist[0]["status"] if hist else None,
        "lastType": hist[0]["input_type"] if hist else None,
        "failed14d": sum(1 for h in hist if h["status"] == "FAILED"),
    }


def collect_standby_side(cur, out):
    """Run on a STANDBY: lag, MRP, gap."""
    stats = rows(cur, """
        SELECT name, value, time_computed FROM v$dataguard_stats
         WHERE name IN ('transport lag', 'apply lag', 'apply finish time')""")
    s = {r["name"]: r["value"] for r in stats}
    mrp = None
    try:
        mrp = one(cur, """
            SELECT action status, sequence# seq FROM v$dataguard_process
             WHERE name LIKE 'MRP%' AND ROWNUM = 1""")
    except oracledb.DatabaseError:  # < 12.2
        mrp = one(cur, """
            SELECT status, sequence# seq FROM v$managed_standby
             WHERE process LIKE 'MRP%' AND ROWNUM = 1""")
    gap = one(cur, "SELECT COUNT(*) cnt FROM v$archive_gap")
    applied = one(cur, """
        SELECT MAX(sequence#) seq FROM v$archived_log WHERE applied = 'YES'""")
    received = one(cur, "SELECT MAX(sequence#) seq FROM v$archived_log")
    out["standby"] = {
        "side": "STANDBY",
        "transportLag": s.get("transport lag"),
        "applyLag": s.get("apply lag"),
        "transportLagSec": interval_to_sec(s.get("transport lag")),
        "applyLagSec": interval_to_sec(s.get("apply lag")),
        "mrpStatus": mrp["status"] if mrp else "NOT RUNNING",
        "mrpSeq": mrp["seq"] if mrp else None,
        "lastReceivedSeq": received["seq"] if received else None,
        "lastAppliedSeq": applied["seq"] if applied else None,
        "gap": int(gap["cnt"]) if gap else 0,
    }


def collect_primary_side(cur, out):
    """Run on a PRIMARY: status of each remote (standby) archive destination."""
    dests = rows(cur, """
        SELECT s.dest_id, s.db_unique_name, s.status, s.type, s.database_mode,
               s.recovery_mode, s.gap_status, s.error,
               s.archived_seq#, s.applied_seq#
          FROM v$archive_dest_status s
          JOIN v$archive_dest d ON d.dest_id = s.dest_id
         WHERE d.target = 'STANDBY' AND s.status <> 'INACTIVE'""")
    cur_seq = one(cur, "SELECT MAX(sequence#) seq FROM v$log WHERE status = 'CURRENT'")
    out["dataguard"] = [{
        "destId": d["dest_id"],
        "standby": d["db_unique_name"],
        "status": d["status"],
        "databaseMode": d["database_mode"],
        "recoveryMode": d["recovery_mode"],
        "gapStatus": d["gap_status"],
        "error": d["error"],
        "archivedSeq": d["archived_seq#"],
        "appliedSeq": d["applied_seq#"],
        "currentSeq": cur_seq["seq"] if cur_seq else None,
        "seqBehind": (cur_seq["seq"] - d["applied_seq#"]) if cur_seq and d["applied_seq#"] else None,
    } for d in dests]


# ═══════════════════════════════════════════════════════════════════
# Health evaluation
# ═══════════════════════════════════════════════════════════════════

def evaluate(d, th):
    """Sets d['status'] (OK / WARN / CRIT / DOWN) and d['issues']."""
    issues = []

    def add(level, msg):
        issues.append({"level": level, "msg": msg})

    if d.get("connectError"):
        d["status"] = "DOWN"
        d["issues"] = [{"level": "CRIT", "msg": d["connectError"]}]
        return

    # a standby mirrors its primary's tablespaces -> alert only on the primary
    is_standby = "STANDBY" in str(d.get("role", ""))
    for t in [] if is_standby else (d.get("tablespaces") or []):
        if t["contents"] == "TEMPORARY":
            continue
        if t["pctUsed"] >= th["tablespaceCritPct"]:
            add("CRIT", "Tablespace %s %.1f%% used" % (t["name"], t["pctUsed"]))
        elif t["pctUsed"] >= th["tablespaceWarnPct"]:
            add("WARN", "Tablespace %s %.1f%% used" % (t["name"], t["pctUsed"]))

    fra = d.get("fra")
    if fra:
        if fra["pctUsed"] >= th["fraCritPct"]:
            add("CRIT", "FRA %.1f%% used" % fra["pctUsed"])
        elif fra["pctUsed"] >= th["fraWarnPct"]:
            add("WARN", "FRA %.1f%% used" % fra["pctUsed"])

    b = d.get("backup")
    if b and d.get("backupExpected", True):
        full_age = b["fullAgeH"]
        if full_age is None:
            add("CRIT", "No successful full backup found")
        elif full_age >= th["fullBackupCritHours"]:
            add("CRIT", "Last full backup %.0fh ago" % full_age)
        elif full_age >= th["fullBackupWarnHours"]:
            add("WARN", "Last full backup %.0fh ago" % full_age)
        if b["incrAgeH"] is not None and b["incrAgeH"] >= th["incrBackupWarnHours"]:
            add("WARN", "Last incremental backup %.0fh ago" % b["incrAgeH"])
        if d.get("logMode") == "ARCHIVELOG":
            arch = b["archAgeH"]
            if arch is None or arch >= th["archBackupCritHours"]:
                add("CRIT", "Archivelog backup %s" % ("not found" if arch is None else "%.0fh ago" % arch))
            elif arch >= th["archBackupWarnHours"]:
                add("WARN", "Last archivelog backup %.0fh ago" % arch)
        if b["lastStatus"] == "FAILED":
            add("CRIT", "Last backup job (%s) FAILED" % b["lastType"])
        elif b["lastStatus"] and "ERROR" in b["lastStatus"]:
            add("CRIT", "Last backup job (%s) %s" % (b["lastType"], b["lastStatus"]))

    sb = d.get("standby")
    if sb:
        for key, label in (("applyLagSec", "Apply lag"), ("transportLagSec", "Transport lag")):
            v = sb.get(key)
            if v is None:
                continue
            if v >= th["lagCritSec"]:
                add("CRIT", "%s %s" % (label, fmt_sec(v)))
            elif v >= th["lagWarnSec"]:
                add("WARN", "%s %s" % (label, fmt_sec(v)))
        if sb["mrpStatus"] == "NOT RUNNING":
            add("CRIT", "MRP (managed recovery) not running")
        if sb["gap"]:
            add("CRIT", "Archive gap detected (%s)" % sb["gap"])

    for dg in d.get("dataguard") or []:
        if dg["status"] != "VALID" or dg["error"]:
            add("CRIT", "Dest %s (%s) %s %s" % (dg["destId"], dg["standby"], dg["status"], dg["error"] or ""))
        elif dg["gapStatus"] not in (None, "NO GAP"):
            add("WARN", "Dest %s (%s) %s" % (dg["destId"], dg["standby"], dg["gapStatus"]))

    for e in d.get("errors") or []:
        add("WARN", e)

    d["issues"] = issues
    if any(i["level"] == "CRIT" for i in issues):
        d["status"] = "CRIT"
    elif issues:
        d["status"] = "WARN"
    else:
        d["status"] = "OK"


# ═══════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════

def collect_db(cfg, th):
    started = time.time()
    d = {
        "name": cfg["name"],
        "group": cfg.get("group", ""),
        "env": cfg.get("env", ""),
        "pairWith": cfg.get("pairWith", ""),
        "backupExpected": cfg.get("backupExpected", True),
        "collectedAt": ts(datetime.now()),
        "errors": [],
    }
    password = cfg.get("password") or os.environ.get(cfg.get("passwordEnv", ""), "")
    params = {"user": cfg["user"], "password": password, "dsn": cfg["dsn"]}
    if cfg.get("mode"):
        params["mode"] = AUTH_MODES[cfg["mode"].upper()]
    try:
        conn = oracledb.connect(**params, tcp_connect_timeout=10)
    except oracledb.Error as e:
        d["connectError"] = str(e).splitlines()[0]
        evaluate(d, th)
        return d

    with conn:
        cur = conn.cursor()
        steps = [("instance", collect_instance), ("size", collect_size)]
        for label, fn in steps:
            try:
                fn(cur, d)
            except oracledb.Error as e:
                d["errors"].append("%s: %s" % (label, str(e).splitlines()[0]))

        is_open = str(d.get("openMode", "")).startswith("READ")
        is_standby = "STANDBY" in str(d.get("role", ""))

        optional = [("fra", collect_fra)]
        if is_open:
            optional.append(("tablespaces", collect_tablespaces))
        # v$rman_backup_job_details is controlfile-based -> works on standby too
        optional.append(("backups", collect_backups))
        optional.append(("dataguard", collect_standby_side if is_standby else collect_primary_side))

        for label, fn in optional:
            try:
                fn(cur, d)
            except oracledb.Error as e:
                d["errors"].append("%s: %s" % (label, str(e).splitlines()[0]))

    d["collectSec"] = round(time.time() - started, 1)
    evaluate(d, th)
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", default=os.path.join(HERE, "databases.json"))
    ap.add_argument("--dry-run", action="store_true", help="print JSON, do not push")
    ap.add_argument("--out", help="also write JSON to this file")
    args = ap.parse_args()

    with open(args.config, encoding="utf-8") as f:
        cfg = json.load(f)
    th = dict(DEFAULT_THRESHOLDS, **cfg.get("thresholds", {}))

    if cfg.get("thickMode"):
        oracledb.init_oracle_client(lib_dir=cfg.get("oracleClientLibDir"))

    dbs = [collect_db(db, th) for db in cfg["databases"] if db.get("enabled", True)]
    payload = {
        "action": "pushOracleStatus",
        "token": cfg.get("pushToken") or os.environ.get("ORACLE_PUSH_TOKEN", ""),
        "collector": os.uname().nodename if hasattr(os, "uname") else os.environ.get("COMPUTERNAME", ""),
        "timestamp": ts(datetime.now()),
        "thresholds": th,
        "databases": dbs,
    }

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(dict(payload, token=""), f, indent=2, default=str)

    if args.dry_run:
        print(json.dumps(dict(payload, token="***"), indent=2, default=str))
        return 0

    if requests is None:
        print("requests is not installed: pip install requests", file=sys.stderr)
        return 2
    r = requests.post(cfg["apiUrl"], data=json.dumps(payload, default=str),
                      headers={"Content-Type": "text/plain"}, timeout=60)
    print("push %s: %s" % (r.status_code, r.text[:300]))
    for d in dbs:
        print("  %-20s %-5s %s" % (d["name"], d["status"], "; ".join(i["msg"] for i in d["issues"])))
    return 0 if r.ok else 1


if __name__ == "__main__":
    sys.exit(main())
