# Oracle DB Status

หน้า **Oracle DB** ใน Status Dashboard แสดงสถานะ Oracle ทุกตัวในที่เดียว:

| หัวข้อ | รายละเอียด |
|---|---|
| ขนาด / พื้นที่ | datafile, tempfile, redo, used / max (รวม autoextend), tablespace ทุกตัว, size trend 30 วัน |
| FRA | used / limit / reclaimable |
| Backup (RMAN) | last full / incremental / archivelog + ประวัติ 14 วัน, job ที่ FAILED |
| Standby (Data Guard) | transport / apply lag, MRP, archive gap, sequence ที่ apply แล้ว; ฝั่ง primary แสดงสถานะ archive dest |
| Health | OK / WARN / CRIT / DOWN ตาม threshold + แจ้ง STALE ถ้าข้อมูลเก่ากว่า 60 นาที |

ดูตัวอย่างได้ทันทีโดยไม่ต้องต่อ DB: เปิด `index.html?demo` (ใช้ `oracle/sample_status.json`)

## Architecture

```
oracle_status_collector.py  ──POST pushOracleStatus──▶  GAS web app (gas/OracleStatus.gs)
  (cron ทุก 15 นาที บนเครื่อง                               │  Sheet: OracleStatus, OracleSizeHistory
   ที่ต่อ DB ได้)                                          ▼
                                index.html ◀──GET getOracleStatus / getOracleHistory
```

Browser ต่อ Oracle ตรงไม่ได้ จึงให้ collector (Python) ไปดึงข้อมูลแล้ว push ขึ้น GAS ตัวเดียวกับที่ dashboard ใช้อยู่

## Setup

### 1. GAS backend
1. เพิ่มไฟล์ `gas/OracleStatus.gs` ลงใน Apps Script project เดิม (ตัวที่เป็น `API_URL`)
2. ต่อเข้ากับ router เดิม:
   ```js
   function doGet(e) {
     var ora = handleOracleGet_(e);
     if (ora) return ora;
     // ... โค้ดเดิม (getStatus, getHistory) ...
   }

   function doPost(e) {
     var ora = handleOraclePost_(e);
     if (ora) return ora;
     // ... โค้ดเดิม (ถ้ามี) ...
   }
   ```
3. Project Settings → Script Properties: เพิ่ม `ORACLE_PUSH_TOKEN` = รหัสลับยาว ๆ
   (ถ้า script ไม่ได้ bound กับ Spreadsheet ให้เพิ่ม `ORACLE_SHEET_ID` ด้วย)
4. Deploy → Manage deployments → Edit → New version (URL เดิม)

### 2. Oracle user สำหรับ monitor
Primary / Active Data Guard (DB เปิด READ):
```sql
CREATE USER dbmon IDENTIFIED BY "********";
GRANT CREATE SESSION, SELECT_CATALOG_ROLE TO dbmon;
```
Standby ที่ MOUNTED อยู่ (ไม่เปิด) ต้อง login แบบ `SYSDG` (12c+) หรือ `SYSDBA` — ตั้ง `"mode": "SYSDG"` ใน config
ข้อมูล tablespace จะไม่มีสำหรับ standby ที่ mounted (ไม่ต้องใช้ เพราะเหมือน primary)

### 3. Collector
```bash
pip install oracledb requests
cp oracle/databases.example.json oracle/databases.json   # แก้ apiUrl, DSN, user
export ORACLE_PUSH_TOKEN=...                             # หรือใส่ pushToken ใน json
export ORA_PW_PRODDB=...                                 # ตาม passwordEnv ของแต่ละ DB
python oracle/oracle_status_collector.py --dry-run       # ทดสอบ: พิมพ์ JSON ไม่ push
python oracle/oracle_status_collector.py                 # push จริง
```
cron (ทุก 15 นาที):
```
*/15 * * * * cd /opt/status-dashboard && . ./ora.env && python3 oracle/oracle_status_collector.py >> /var/log/ora_status.log 2>&1
```
`databases.json` ถูก gitignore ไว้แล้ว — อย่า commit รหัสผ่าน

#### Config ต่อ DB
| key | ความหมาย |
|---|---|
| `name` | ชื่อที่แสดง (unique) |
| `dsn` | `host:port/service_name` |
| `user`, `passwordEnv` / `password` | credential |
| `mode` | `SYSDG` / `SYSDBA` / `SYSBACKUP` (สำหรับ standby ที่ mounted) |
| `env`, `group` | label สำหรับค้นหา/กรอง |
| `pairWith` | ชื่อคู่ primary/standby |
| `backupExpected` | `false` = ไม่เตือนเรื่อง backup (เช่น standby ที่ไม่ได้ backup, DEV) |
| `enabled` | `false` = ข้าม |

ถ้าเป็น Oracle 11g ให้ตั้ง `"thickMode": true` (+ `oracleClientLibDir`) เพราะ thin mode รองรับ 12.1 ขึ้นไป

#### Thresholds (ปรับใน `thresholds` ของ config)
| key | default |
|---|---|
| `tablespaceWarnPct` / `CritPct` | 85 / 95 |
| `fraWarnPct` / `CritPct` | 80 / 90 |
| `fullBackupWarnHours` / `CritHours` | 192 (8 วัน) / 336 (14 วัน) |
| `incrBackupWarnHours` | 36 |
| `archBackupWarnHours` / `CritHours` | 6 / 24 |
| `lagWarnSec` / `lagCritSec` | 300 / 1800 |
