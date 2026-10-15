/**
 * Oracle Status — GAS backend for the Status Dashboard
 * ====================================================
 * Add this file to the existing Apps Script project that serves API_URL,
 * then hook it into doGet / doPost (see README in /oracle).
 *
 *   doGet  ?action=getOracleStatus             -> latest status of every DB
 *   doGet  ?action=getOracleHistory&db=NAME&days=30 -> daily size history
 *   doPost {action:'pushOracleStatus', token, databases:[...]}  (from collector)
 *
 * Script Properties:
 *   ORACLE_PUSH_TOKEN  (required)  shared secret used by the collector
 *   ORACLE_SHEET_ID    (optional)  spreadsheet id; defaults to the bound spreadsheet
 */

var ORA_STATUS_SHEET = 'OracleStatus';
var ORA_HISTORY_SHEET = 'OracleSizeHistory';

// ── Router helpers (call these from your doGet / doPost) ──────────────

/** Returns a ContentService output, or null if the action is not an Oracle one. */
function handleOracleGet_(e) {
  var action = (e && e.parameter && e.parameter.action) || '';
  if (action === 'getOracleStatus') return oraJson_(getOracleStatus_());
  if (action === 'getOracleHistory') {
    return oraJson_(getOracleHistory_(e.parameter.db || '', parseInt(e.parameter.days, 10) || 30));
  }
  return null;
}

/** Returns a ContentService output, or null if the body is not an Oracle push. */
function handleOraclePost_(e) {
  var body;
  try { body = JSON.parse(e.postData.contents); } catch (err) { return null; }
  if (!body || body.action !== 'pushOracleStatus') return null;
  return oraJson_(pushOracleStatus_(body));
}

// ── Actions ────────────────────────────────────────────────────────────

function pushOracleStatus_(body) {
  var token = PropertiesService.getScriptProperties().getProperty('ORACLE_PUSH_TOKEN');
  if (!token || body.token !== token) return { ok: false, error: 'invalid token' };

  var lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    var sh = oraSheet_(ORA_STATUS_SHEET, ['name', 'collectedAt', 'status', 'collector', 'json']);
    var data = sh.getDataRange().getValues();
    var rowByName = {};
    for (var i = 1; i < data.length; i++) rowByName[data[i][0]] = i + 1;

    var dbs = body.databases || [];
    for (var j = 0; j < dbs.length; j++) {
      var d = dbs[j];
      var row = [d.name, d.collectedAt, d.status, body.collector || '', JSON.stringify(d)];
      if (rowByName[d.name]) {
        sh.getRange(rowByName[d.name], 1, 1, row.length).setValues([row]);
      } else {
        sh.appendRow(row);
        rowByName[d.name] = sh.getLastRow();
      }
      oraRecordSize_(d);
    }
    PropertiesService.getScriptProperties().setProperty('ORACLE_THRESHOLDS', JSON.stringify(body.thresholds || {}));
    return { ok: true, saved: dbs.length };
  } finally {
    lock.releaseLock();
  }
}

function getOracleStatus_() {
  var sh = oraSheet_(ORA_STATUS_SHEET, ['name', 'collectedAt', 'status', 'collector', 'json']);
  var data = sh.getDataRange().getValues();
  var dbs = [];
  for (var i = 1; i < data.length; i++) {
    try { dbs.push(JSON.parse(data[i][4])); } catch (err) {}
  }
  var th = PropertiesService.getScriptProperties().getProperty('ORACLE_THRESHOLDS');
  return {
    timestamp: Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'yyMMdd.HHmmss'),
    thresholds: th ? JSON.parse(th) : {},
    databases: dbs
  };
}

function getOracleHistory_(db, days) {
  var sh = oraSheet_(ORA_HISTORY_SHEET, ['date', 'name', 'allocatedGB', 'usedGB', 'fraUsedGB']);
  var data = sh.getDataRange().getValues();
  var since = Utilities.formatDate(new Date(Date.now() - days * 86400000), Session.getScriptTimeZone(), 'yyyy-MM-dd');
  var points = [];
  for (var i = 1; i < data.length; i++) {
    var date = oraDateStr_(data[i][0]);
    if (data[i][1] !== db || date < since) continue;
    points.push({ date: date, allocatedGB: data[i][2], usedGB: data[i][3], fraUsedGB: data[i][4] });
  }
  return { db: db, days: days, points: points };
}

// ── Internals ──────────────────────────────────────────────────────────

/** One row per DB per day (the last push of the day wins). */
function oraRecordSize_(d) {
  if (!d.size) return;
  var sh = oraSheet_(ORA_HISTORY_SHEET, ['date', 'name', 'allocatedGB', 'usedGB', 'fraUsedGB']);
  var today = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'yyyy-MM-dd');
  var row = [today, d.name, d.size.allocatedGB || '', d.size.usedGB || '', d.fra ? d.fra.usedGB : ''];

  // only scan the tail — today's rows are always at the bottom
  var last = sh.getLastRow();
  var start = Math.max(2, last - 200);
  if (last >= 2) {
    var tail = sh.getRange(start, 1, last - start + 1, 2).getValues();
    for (var i = tail.length - 1; i >= 0; i--) {
      if (oraDateStr_(tail[i][0]) !== today) break;
      if (tail[i][1] === d.name) {
        sh.getRange(start + i, 1, 1, row.length).setValues([row]);
        return;
      }
    }
  }
  sh.appendRow(row);
}

function oraSheet_(name, header) {
  var id = PropertiesService.getScriptProperties().getProperty('ORACLE_SHEET_ID');
  var ss = id ? SpreadsheetApp.openById(id) : SpreadsheetApp.getActiveSpreadsheet();
  var sh = ss.getSheetByName(name);
  if (!sh) {
    sh = ss.insertSheet(name);
    sh.appendRow(header);
    sh.setFrozenRows(1);
    if (name === ORA_HISTORY_SHEET) sh.getRange('A:A').setNumberFormat('@');
  }
  return sh;
}

function oraDateStr_(v) {
  if (v instanceof Date) return Utilities.formatDate(v, Session.getScriptTimeZone(), 'yyyy-MM-dd');
  return String(v);
}

function oraJson_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}
