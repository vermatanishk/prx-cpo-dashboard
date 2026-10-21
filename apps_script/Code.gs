/**
 * PRX CPO Dashboard -- single-tab view, rebuilt from the GitHub repo.
 *
 * Pulls data/dashboard_long.csv, data/labels.csv and data/meta.json from the (private) repo and
 * rebuilds ONE sheet ("CPO Dashboard"). Nothing is pasted by hand: every refresh re-reads the repo.
 *
 * Reading guide
 *   Ref      letter tag of the row (A, B, ... Z, AA, ...)
 *   Formula  how the row is derived from other tagged rows, e.g.  "B / A",  "J x K"
 *   Base rows hold numbers from the pipeline; derived rows are LIVE sheet formulas over the tagged
 *   rows, so the arithmetic is visible and editable. Totals use the same formula over the Total column.
 *
 * One-time setup: see README.md ("Apps Script setup"). The repo is public, so no token is needed.
 * (A token is only used as a fallback if the repo is ever made private again.)
 */

const CONFIG = {
  OWNER: 'vermatanishk',
  REPO: 'prx-cpo-dashboard',
  BRANCH: 'main',
  DATA_DIR: 'data',
  SHEET_NAME: 'CPO Dashboard',
};

const FMT = { int: '#,##0', pct: '0.00%', ratio: '0.000', dec: '0.00', text: 'General' };
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

// ---------------------------------------------------------------------------------------------
// Menu + setup
// ---------------------------------------------------------------------------------------------

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('PRX CPO')
    .addItem('Refresh from GitHub', 'refreshDashboard')
    .addSeparator()
    .addItem('Set GitHub token…', 'setGithubToken')
    .addItem('Schedule daily refresh (8am)', 'scheduleDailyRefresh')
    .addItem('Remove scheduled refresh', 'removeScheduledRefresh')
    .addToUi();
}

function setGithubToken() {
  const ui = SpreadsheetApp.getUi();
  const r = ui.prompt('GitHub token', 'Fine-grained token, read-only "Contents" on ' + CONFIG.OWNER + '/' + CONFIG.REPO, ui.ButtonSet.OK_CANCEL);
  if (r.getSelectedButton() === ui.Button.OK && r.getResponseText().trim()) {
    PropertiesService.getScriptProperties().setProperty('GITHUB_TOKEN', r.getResponseText().trim());
    ui.alert('Token saved. Use PRX CPO -> Refresh from GitHub.');
  }
}

function scheduleDailyRefresh() {
  removeScheduledRefresh();
  ScriptApp.newTrigger('refreshDashboard').timeBased().everyDays(1).atHour(8).create();
}

function removeScheduledRefresh() {
  ScriptApp.getProjectTriggers()
    .filter(function (t) { return t.getHandlerFunction() === 'refreshDashboard'; })
    .forEach(function (t) { ScriptApp.deleteTrigger(t); });
}

// ---------------------------------------------------------------------------------------------
// GitHub access
// ---------------------------------------------------------------------------------------------

function fetchRepoFile_(name) {
  // Public repo: works with no token. If a token is saved it is sent too (needed only if the repo is made private again).
  const url = 'https://raw.githubusercontent.com/' + CONFIG.OWNER + '/' + CONFIG.REPO + '/' +
    CONFIG.BRANCH + '/' + CONFIG.DATA_DIR + '/' + name + '?nocache=' + new Date().getTime();
  const res = UrlFetchApp.fetch(url, { muteHttpExceptions: true });
  if (res.getResponseCode() === 200) return res.getContentText();

  const token = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
  if (token) {                                     // private-repo fallback via the API
    const api = 'https://api.github.com/repos/' + CONFIG.OWNER + '/' + CONFIG.REPO + '/contents/' +
      CONFIG.DATA_DIR + '/' + name + '?ref=' + CONFIG.BRANCH;
    const r2 = UrlFetchApp.fetch(api, {
      headers: { Authorization: 'Bearer ' + token, Accept: 'application/vnd.github.raw+json', 'X-GitHub-Api-Version': '2022-11-28' },
      muteHttpExceptions: true,
    });
    if (r2.getResponseCode() === 200) return r2.getContentText();
    throw new Error('GitHub ' + r2.getResponseCode() + ' for ' + name + ': ' + r2.getContentText().slice(0, 200));
  }
  throw new Error('GitHub ' + res.getResponseCode() + ' for ' + name + ' (repo is private? then set a token via PRX CPO -> Set GitHub token).');
}

// ---------------------------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------------------------

function colLetter_(n) {            // 1 -> A
  let s = '';
  while (n > 0) { const m = (n - 1) % 26; s = String.fromCharCode(65 + m) + s; n = Math.floor((n - 1) / 26); }
  return s;
}

function tagFor_(i) {               // 0 -> A, 25 -> Z, 26 -> AA
  return colLetter_(i + 1);
}

function monthLabel_(ym) {          // '2026-09' -> 'Sep 2026'
  const p = ym.split('-');
  return MONTHS[Number(p[1]) - 1] + ' ' + p[0];
}

// ---------------------------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------------------------

function refreshDashboard() {
  const meta = JSON.parse(fetchRepoFile_('meta.json'));
  const longRows = Utilities.parseCsv(fetchRepoFile_('dashboard_long.csv')).slice(1);
  const labelRows = Utilities.parseCsv(fetchRepoFile_('labels.csv')).slice(1);

  const V = {};                                    // 'section|key|month' -> number
  longRows.forEach(function (r) { V[r[0] + '|' + r[1] + '|' + r[2]] = Number(r[3]); });

  const months = meta.months.slice().reverse();    // latest first (matches the reference view)
  const nM = months.length;
  const FIRST_COL = 4;                             // D
  const colOf = months.map(function (m, i) { return FIRST_COL + i; });
  const SPACER_COL = FIRST_COL + nM;
  const TOTAL_COL = SPACER_COL + 1;
  const NCOLS = TOTAL_COL;
  const periods = months.concat(['Total']);        // value-lookup keys, in column order
  const periodCol = colOf.concat([TOTAL_COL]);

  // -------- row model --------
  const rows = [];                                 // {type, tag, label, ftext, cell(colLetter, period), fmt, bold, indent}
  const START_ROW = 2;                             // row 1 is the header
  let tagIdx = 0;
  const posOf = {};                                // tag -> row number

  function push(r) { r.row = START_ROW + rows.length; rows.push(r); return r; }
  function section(title, tagged, ftext) {
    const r = { type: 'section', label: title, ftext: ftext || '' };
    if (tagged) { r.tag = tagFor_(tagIdx++); posOf[r.tag] = START_ROW + rows.length; }
    return push(r);
  }
  function blank() { return push({ type: 'blank' }); }
  function val(label, getKey, fmt, o) {            // base number from the pipeline
    o = o || {};
    const r = { type: 'val', tag: o.noTag ? '' : tagFor_(tagIdx++), label: label, ftext: o.ftext || '',
                fmt: fmt || 'int', indent: o.indent || 0,
                cell: function (c, p) { const k = getKey(p); return k in V ? V[k] : ''; } };
    if (r.tag) posOf[r.tag] = START_ROW + rows.length;
    return push(r);
  }
  function calc(label, tpl, fmt, o) {              // live formula; tpl uses {TAG}
    o = o || {};
    const r = { type: 'calc', tag: o.noTag ? '' : tagFor_(tagIdx++), label: label,
                ftext: tpl.replace(/\{(\w+)\}/g, '$1').replace(/\*/g, ' x ').replace(/\//g, ' / ').replace(/-/g, ' - ').replace(/\s+/g, ' ').trim(),
                fmt: fmt || 'int', indent: o.indent || 0,
                cell: function (c) { return '=' + tpl.replace(/\{(\w+)\}/g, function (m, t) { return c + posOf[t]; }); } };
    if (r.tag) posOf[r.tag] = START_ROW + rows.length;
    return push(r);
  }

  const S = function (key) { return function (p) { return 'summary|' + key + '|' + p; }; };

  // ===== 1. Summary =====
  const A = val('Total Orders', S('orders'));
  const B = val('Total Orders w Support Contact', S('orders_w_contact'));
  const C = val('Total Orders w Support Contact - Actionable', S('orders_w_contact_actionable'));
  calc('% Support Driven Orders', '{' + B.tag + '}/{' + A.tag + '}', 'pct');
  const E = val('Total Unique Users (all orders)', S('users'));
  const F = val('Total Unique Users w/ Support Contact', S('users_w_contact'));
  calc('% Users Who Contact for Support at Least Once', '{' + F.tag + '}/{' + E.tag + '}', 'pct');
  const H = val('Total Contacts Made (raw, support-only, sales stripped)', S('contacts_raw'));
  const I = val('Total Contacts After De-dup (unique issues: Order ID x Sub-disposition)', S('contacts_dedup'));
  blank();
  const IPO = calc('IPO = Issues per Order (unique issues / orders)  [old definition of CPO]', '{' + I.tag + '}/{' + A.tag + '}', 'pct');
  const IPI = calc('IPI = Interactions per Issue (raw contacts / unique issues)', '{' + H.tag + '}/{' + I.tag + '}', 'ratio');
  const CPO = calc('CPO = Contacts per Order = IPO x IPI  (= raw contacts / orders)', '{' + IPO.tag + '}*{' + IPI.tag + '}', 'pct');
  blank();

  // ===== 2. Channel volumes =====
  function channelBlock(title, sec, ref) {
    section(title, false);
    const first = rows.length;
    const tags = [];
    meta.channels.forEach(function (ch) {
      tags.push(val(ch, function (p) { return sec + '|' + ch + '|' + p; }).tag);
    });
    const firstRow = START_ROW + first, lastRow = firstRow + meta.channels.length - 1;
    const chk = push({ type: 'calc', tag: tagFor_(tagIdx++), label: 'Check: sum of channels - ' + ref.tag + '  (should be 0)',
      ftext: 'SUM(' + tags[0] + ':' + tags[tags.length - 1] + ') - ' + ref.tag, fmt: 'int', indent: 1,
      cell: function (c) { return '=SUM(' + c + firstRow + ':' + c + lastRow + ')-' + c + posOf[ref.tag]; } });
    posOf[chk.tag] = chk.row;
    blank();
  }
  channelBlock('Contact Volume by Channel -- Before Dedup (raw, support-only, sales stripped)', 'channel_before', H);
  channelBlock('Contact Volume by Channel -- After Dedup (Order ID x Sub-disposition, first-Actionable)', 'channel_after', I);

  // ===== 3. Contacts to resolution =====
  function resBlock(title, sec, checkAgainst) {
    section(title, false);
    const tot = [], bot = [], hum = [];
    [['1', '1 contact'], ['2', '2 contacts'], ['3', '3 contacts'], ['4+', '4+ contacts']].forEach(function (b) {
      const t = val(b[1], function (p) { return sec + '|b' + b[0] + '_total|' + p; });
      const bo = val('Bot Resolved', function (p) { return sec + '|b' + b[0] + '_bot|' + p; }, 'int', { indent: 2 });
      const hu = calc('Human Resolved', '{' + t.tag + '}-{' + bo.tag + '}', 'int', { indent: 2 });
      tot.push(t); bot.push(bo); hum.push(hu);
    });
    const TK = calc('Total keys', tot.map(function (t) { return '{' + t.tag + '}'; }).join('+'), 'int');
    if (checkAgainst) calc('Check: Total keys - ' + checkAgainst.tag + '  (should be 0)', '{' + TK.tag + '}-{' + checkAgainst.tag + '}', 'int', { indent: 1 });
    ['Mean', 'Median (P50)', 'P75', 'P90', 'P95', 'P99', 'P99.9', 'Max (tail)'].forEach(function (st) {
      val(st, function (p) { return sec + '|stat_' + st + '|' + p; }, st === 'Mean' ? 'dec' : 'int', { indent: 1 });
    });
    calc('Overall FCR = 1-contact keys / total keys', '{' + tot[0].tag + '}/{' + TK.tag + '}', 'pct');
    calc('BFCR (bot-resolved FCR) = bot 1-contact / total keys', '{' + bot[0].tag + '}/{' + TK.tag + '}', 'pct');
    calc('HFCR (human-touched FCR) = human 1-contact / total keys', '{' + hum[0].tag + '}/{' + TK.tag + '}', 'pct');
    blank();
  }
  resBlock('Contacts to Resolution (By Order ID)', 'res_order', B);
  resBlock('Contacts to Resolution (By Order ID x Sub-Disposition, Actionable only)', 'res_order_sub', null);

  // ===== 4. Breakdown by dispositions =====
  // labels: key, disp_key, disp_label, sub_key, sub_label (already ordered by volume)
  const groups = [];                                // [{disp_key, disp_label, subs:[{key, sub_label}]}]
  labelRows.forEach(function (r) {
    let g = groups.length ? groups[groups.length - 1] : null;
    if (!g || g.disp_key !== r[1]) { g = { disp_key: r[1], disp_label: r[2], subs: [] }; groups.push(g); }
    g.subs.push({ key: r[0], sub_label: r[4] });
  });

  // Layout: [display block: "volume | share | IPI"]  [IPO block]  [calculation base blocks, grouped + collapsed]
  // Tags are assigned in creation order, so the later blocks' tags are known up front.
  const T_DISP = tagFor_(tagIdx), T_IPO = tagFor_(tagIdx + 1), T_RAW = tagFor_(tagIdx + 2), T_ISS = tagFor_(tagIdx + 3);
  const RAW = { pos: {} }, ISS = { pos: {} };       // filled when the base blocks are built below

  // --- display block: one text cell per month = "volume | % share | IPI" (live formula over the base blocks) ---
  //   disposition row: volume | % of ALL raw contacts (row H)      | IPI = volume / issues
  //   sub-disp row   : volume | % of its disposition's volume      | IPI = volume / issues
  function pctCell(c, cnt, base) { return 'IFERROR(TEXT(' + c + cnt + '/' + c + base + ',"0.0%"),"-")'; }
  function ipiCell(c, raw, iss) { return 'IFERROR(TEXT(' + c + raw + '/' + c + iss + ',"0.00"),"-")'; }
  function triple(c, raw, iss, base) {
    return '=IF(N(' + c + raw + ')=0,"-",TEXT(' + c + raw + ',"#,##0")&" | "&' + pctCell(c, raw, base) + '&" | "&' + ipiCell(c, raw, iss) + ')';
  }
  section('Breakdown by Dispositions -- Contacts before dedup (raw):  volume | % share | IPI', true,
    'volume | share | IPI.  Disposition: share = vol / ' + H.tag + '.  Sub: share = vol / its disposition.  IPI = ' + T_RAW + ' / ' + T_ISS);
  groups.forEach(function (g) {
    push({ type: 'calc', label: g.disp_label, fmt: 'text', bold: true, ftext: '', tag: '',
      cell: function (c) { return triple(c, RAW.pos['__disp_' + g.disp_key], ISS.pos['__disp_' + g.disp_key], posOf[H.tag]); } });
    g.subs.forEach(function (s) {
      push({ type: 'calc', label: s.sub_label, fmt: 'text', indent: 2, ftext: '', tag: '',
        cell: function (c) { return triple(c, RAW.pos[s.key], ISS.pos[s.key], RAW.pos['__disp_' + g.disp_key]); } });
    });
  });
  blank();

  // --- IPO block (unchanged): issues / orders ---
  section('Breakdown by Dispositions -- IPO by sub-disposition (issues / orders)', true, 'each row = ' + T_ISS + ' / ' + A.tag);
  groups.forEach(function (g) {
    push({ type: 'calc', label: g.disp_label, fmt: 'pct', bold: true, ftext: '', tag: '',
      cell: function (c) { return '=IFERROR(' + c + ISS.pos['__disp_' + g.disp_key] + '/' + c + posOf[A.tag] + ',0)'; } });
    g.subs.forEach(function (s) {
      push({ type: 'calc', label: s.sub_label, fmt: 'pct', indent: 2, ftext: '', tag: '',
        cell: function (c) { return '=IFERROR(' + c + ISS.pos[s.key] + '/' + c + posOf[A.tag] + ',0)'; } });
    });
  });
  blank();

  // --- calculation base blocks (numeric): raw contacts and issues, rolled up per disposition ---
  function baseBlock(title, sec, store) {
    const hdr = section(title, true, 'Disposition row = SUM of its sub-dispositions');
    const first = rows.length;
    groups.forEach(function (g) {
      const parent = push({ type: 'calc', label: g.disp_label, fmt: 'int', bold: true, ftext: '', tag: '', cell: null });
      const f = START_ROW + rows.length;
      g.subs.forEach(function (s) {
        const r = val(s.sub_label, function (p) { return sec + '|' + s.key + '|' + p; }, 'int', { indent: 2, noTag: true });
        store.pos[s.key] = r.row;
      });
      const l = START_ROW + rows.length - 1;
      parent.cell = function (c) { return '=SUM(' + c + f + ':' + c + l + ')'; };
      store.pos['__disp_' + g.disp_key] = parent.row;
    });
    store.first = START_ROW + first - 1;            // include the header row in the collapsible group
    store.last = START_ROW + rows.length - 1;
    blank();
  }
  baseBlock('Calculation base -- Contacts before dedup (raw)', 'disp_before', RAW);
  baseBlock('Calculation base -- Issues after dedup (Order ID x Sub-disposition)', 'disp_after', ISS);
  const GROUP_RANGE = { first: RAW.first, last: ISS.last };

  // ===== Notes =====
  section('Notes', false);
  push({ type: 'note', label: 'Data: version ' + meta.version_tag + ', ' + meta.range_start + ' to ' + meta.range_end_exclusive + ' (end exclusive); generated ' + meta.generated_at_utc + ' UTC from github.com/' + CONFIG.OWNER + '/' + CONFIG.REPO });
  (meta.caveats || []).forEach(function (t) { push({ type: 'note', label: t }); });

  // -------- render --------
  const out = [], fmts = [];
  const header = ['Ref', 'Metric', 'Formula'];
  for (let i = 0; i < nM; i++) header.push(monthLabel_(months[i]));
  header.push('');
  header.push('Total');
  out.push(header);
  fmts.push(header.map(function () { return '@'; }));   // keep 'Sep 2026' as text, not a date

  rows.forEach(function (r) {
    const line = new Array(NCOLS).fill('');
    const f = new Array(NCOLS).fill('General');
    if (r.type === 'blank') { out.push(line); fmts.push(f); return; }
    line[0] = r.tag || '';
    line[1] = new Array((r.indent || 0) + 1).join('  ') + r.label;
    line[2] = r.ftext || '';
    if (r.type === 'val' || r.type === 'calc') {
      periods.forEach(function (p, i) {
        const c = periodCol[i];
        line[c - 1] = r.cell(colLetter_(c), p);
        f[c - 1] = FMT[r.fmt];
      });
    }
    out.push(line); fmts.push(f);
  });

  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sh = ss.getSheetByName(CONFIG.SHEET_NAME);
  if (!sh) sh = ss.insertSheet(CONFIG.SHEET_NAME, 0);
  sh.clear();
  sh.clearFormats();
  const nRows = out.length;
  if (sh.getMaxRows() < nRows + 5) sh.insertRowsAfter(sh.getMaxRows(), nRows + 5 - sh.getMaxRows());
  if (sh.getMaxColumns() < NCOLS) sh.insertColumnsAfter(sh.getMaxColumns(), NCOLS - sh.getMaxColumns());

  const rng = sh.getRange(1, 1, nRows, NCOLS);
  rng.setNumberFormats(fmts);
  rng.setValues(out);                              // strings starting with '=' become live formulas

  // -------- styling --------
  sh.getRange(1, 1, 1, NCOLS).setFontWeight('bold').setFontColor('#ffffff').setBackground('#632423');
  sh.getRange(1, FIRST_COL, nRows, NCOLS - FIRST_COL + 1).setHorizontalAlignment('right');
  sh.getRange(1, 1, nRows, 1).setFontColor('#7f7f7f').setHorizontalAlignment('center');
  sh.getRange(2, 3, nRows - 1, 1).setFontColor('#7f7f7f').setFontStyle('italic');
  rows.forEach(function (r) {
    const rr = r.row;
    if (r.type === 'section') {
      sh.getRange(rr, 1, 1, NCOLS).setFontWeight('bold').setBackground('#f2dcdb');
    } else if (r.bold) {
      sh.getRange(rr, 1, 1, NCOLS).setFontWeight('bold').setBackground('#f7f7f7');
    } else if (r.type === 'calc' && r.tag && r.fmt !== 'int') {
      sh.getRange(rr, 2, 1, NCOLS - 1).setFontWeight('bold');
    } else if (r.type === 'note') {
      sh.getRange(rr, 2).setFontStyle('italic').setFontColor('#595959');
    }
  });
  sh.getRange(1, SPACER_COL, nRows, 1).setBackground('#ffffff');
  sh.setColumnWidth(1, 44); sh.setColumnWidth(2, 520); sh.setColumnWidth(3, 190);
  for (let c = FIRST_COL; c <= NCOLS; c++) sh.setColumnWidth(c, c === SPACER_COL ? 18 : 92);
  sh.setFrozenRows(1); sh.setFrozenColumns(3);
  try {                                            // collapse the numeric calculation-base rows (expand with the +)
    const grp = sh.getRange(GROUP_RANGE.first, 1, GROUP_RANGE.last - GROUP_RANGE.first + 1, 1);
    grp.shiftRowGroupDepth(1);
    sh.getRowGroup(GROUP_RANGE.first, 1).collapse();
  } catch (e) { /* grouping is cosmetic */ }
  sh.getRange('A1').setNote('Generated ' + meta.generated_at_utc + ' UTC · version ' + meta.version_tag +
    '\nSource: github.com/' + CONFIG.OWNER + '/' + CONFIG.REPO + ' (' + CONFIG.BRANCH + ')');
  SpreadsheetApp.getActive().toast('CPO Dashboard refreshed (' + meta.version_tag + ')', 'PRX CPO', 5);
}
