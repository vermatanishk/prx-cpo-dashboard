"""Builds the exhaustive Tech_yodas payment-thread report (CSV + stats) from notes.jsonl.
notes.jsonl = one record per thread, written by hand while reading every thread in the Chat UI (18 Sep - 17 Oct 2026).
"""
import json, re, csv, sys, collections, statistics, datetime as dt
SRC = sys.argv[1]
OUT = sys.argv[2]

rows = {}
for l in open(SRC):
    r = json.loads(l); rows[r['tid']] = r        # last write wins

BUCKET = {}
def setb(code, ids):
    for i in ids.split():
        BUCKET[i] = code

# V1 = answerable from our own tables (view alone)
setb('V1', """aak6jtEWBqE QC8TAqAAIWU H7324a_s_8M hinDjhxWJZc P0-U0kqrK_4 RKgpR081S-Y HW63ydWhEvA Z1BNqebKV0w l5Il2MnkYKA
PRWzVF69WoA 5HpgKehzBIE TjFYre7pkSI qShwy16LbSo al0qwzlQj-I 6XJGp7cnLoU O7zgMY7SYr0 wih4ax6FeVg MvRV7vqMLGY PRFf4Il-uFg
MmMLG-vqjeQ l2wJr4OVPss D4Q0XROFwAo p2TyT8hQeAU YqLOjBxnVJM OK9clJhjyhU nvRil-SEqw8 LDGV2flNWPc RJmwvS0DX40 eFQvYny1mxw
uQ0qDXdwa2k 3xf84Cv2id4 9KEhdr0AW9g LVOGtvJvJjk S0JI2dlRiTk npHx792RqDc VlPqxU2gkRA M5balIThwfI LUvIXrvLvXI B8nKq5yyVdQ
7esgQrnPVqc gNZ-nzVeCrU ci8OpAgbOYw szwCQzJp5NQ 87h_6BZSioY JfulVPuYdWw 33s0S81tC0k niYbPBIIlSU WJsSXWSJTrU zesEJT0OLA0
qPf8SV4ffq4 rdR6DBnGXr8""")
# V2 = needs gateway-side (Cashfree / PG) data that is not in our tables
setb('V2', """77etWeBBcrU 1I8YLWVz2HQ -YnfBSjAqEo lXclu3tvmSc WKaT2CBOWTw 3AOxV3ok3ss tBjPHDEFTHw UBfW2EZ9p_Q esTo5b72rNI
HUugoTpQwb8 BtL_SJDQwK4 gtKxtNoULwA bwB-Hf9PXag yHpZj2aIqso AmBAK6jkJJc m1shYuoLP7E KmJGM_uXwjc""")
# T = view can diagnose and route with full context, but fix needs tech / payments / ops action
setb('T', """52WGnTdOzvU tpg6gs6Z0PE LySc3P02_M8 RT-8oZsxWYo uRcq8nnCebE aO3BRlrYLOg nWyMCeiehjY WARS4kf2rzg iq0SP3i3TV4
wJ3h2X9iNXg ICQyiac1VzQ Pm90f8SJWB0 xaPhiLAs8nE I4bQEcU99Qg 8LbywduW_2Q QkwoG_rtlt0 zEsvP77beS8 9UwOMzn1624 A9gHz__lhzk
DhI9VT6P6Bo MPmwPxDJpMA fPOvPuwQs8w 318zG1xRguM 6FWgNtTfvXA OphSK4CpVCo 7IL8cBD7cKU 9wQLQOEaQJI SLXd2ZCoqQU CPKmzwpXxK8
8Z0Wfizhq0g _y7oKwFnNQg w14m8EFep0s nB4mWNsZ180 diEX-cHZp8c AOn22vWfAwc eRp2Axdl_bY EWxhrhHEu_o""")
# P = product / policy / permission / code defect; not a payment-state question
setb('P', """xK6lGtAcV20 o-U091OGeBc HApLfjhg6ns 8yTZJr4RMKQ nU0GVkEiTY4 RUXYw3-mVXE IkCcZhn7r4Q SAHfng30ZW8""")

GROUP_RULES = [
 (r'^Deducted', 'G1 Deducted twice / duplicate debit'),
 (r'^(Paid|Debited)', 'G2 Paid but order not placed / not found / not linked'),
 (r'^(Refund failed|Refund initiated|Refund stuck|Refund not initiated|Refund processed, customer)', 'G3 Refund not credited (payout failed / stuck at gateway)'),
 (r'^Refund status|^Refund failed vs', 'G4 Refund paid but status wrong'),
 (r'^Refund (amount|shortfall)', 'G5 Refund amount discrepancy / shortfall'),
 (r'^(Return|Order edit|Amount mismatch)', 'G6 Return refund blocked / return-order data'),
 (r'^COD-to-online', 'G7 COD to online conversion payment'),
 (r'^(Payment failed|Gateway incident)', 'G8 Payment failed / gateway incident'),
 (r'^(Paid for Edge|PRx Cash|Wallet)', 'G9 Wallet / Edge paid-not-provisioned'),
]
def group_of(r):
    c = r['cat']
    if c.startswith('Refund status') or c.startswith('Refund failed vs'): return 'G4 Refund paid but status wrong'
    if c.startswith('Paid for Edge') or c.startswith('PRx Cash') or c.startswith('Wallet'): return 'G9 Wallet / Edge paid-not-provisioned'
    if c.startswith('Paid, order not placed') or c.startswith('Paid, order not found') or c.startswith('Paid, not linked') or c.startswith('Paid on wrong') or c.startswith('Paid via link') or c.startswith('Paid, no order') or c.startswith('Paid, never') or c.startswith('Paid order stuck') or c.startswith('Paid, order on') or c.startswith('Debited'):
        return 'G2 Paid but order not placed / not found / not linked'
    for pat, g in GROUP_RULES:
        if re.search(pat, c): return g
    return 'G10 Other'

def sherlock_min(t):
    m = re.search(r'SHERLOCK[^0-9]{0,12}(\d+)\s*min', t, re.I)
    if m: return int(m.group(1))
    m = re.search(r'SHERLOCK[^0-9]{0,12}(\d+)\s*h', t, re.I)
    if m: return int(m.group(1))*60
    return None

VIEW = {'V1':'Y - view alone (data already in our tables)',
        'V2':'Needs gateway data (Cashfree/PG attempts & refunds) in the view',
        'T':'Diagnose + route with full context; fix needs tech/payments',
        'P':'N - product / policy / permission / code defect'}
BOT = {'V1':'Y','V2':'Y (after gateway attempt+refund events are ingested)','T':'Partial (diagnose, escalate with ready context)','P':'N'}

out=[]; oos=[]
for tid, r in sorted(rows.items(), key=lambda x:x[1]['raised']):
    if r['cat'].startswith('OUT OF SCOPE'):
        oos.append(r); continue
    b = BUCKET.get(tid)
    assert b, tid
    sm = sherlock_min(r['tech'])
    sher = 'Y' if 'SHERLOCK' in r['tech'].upper() or '/resume' in r['tech'] else 'N'
    out.append(dict(
        thread_id=tid, thread_url=f'https://chat.google.com/u/1/room/AAAAq-O7WEc/{tid}',
        raised_ist=r['raised'], raised_by=r['raiser'], ticket=r['ticket'], order_ids=r['orders'],
        group=group_of(r), sub_category=r['cat'],
        reported_issue=r['issue'], what_actually_went_wrong=r['cause'],
        tech_or_sherlock_action=r['tech'], time_to_resolve=r['ttr'],
        sherlock_replied=sher, sherlock_first_reply_min=sm if sm is not None else '',
        bucket=b, fixable_by_bot_YN=BOT[b], would_view_solve=VIEW[b], bot_note=r['bot_note'], solved_by=r['who']))
with open(OUT,'w',newline='') as f:
    w=csv.DictWriter(f, fieldnames=list(out[0].keys())); w.writeheader(); w.writerows(out)

# ---- stats
print('in-scope', len(out), 'out-of-scope', len(oos))
print(collections.Counter(o['bucket'] for o in out))
print(collections.Counter(o['group'] for o in out))
byg = collections.defaultdict(collections.Counter)
for o in out: byg[o['group']][o['bucket']]+=1
for g,c in sorted(byg.items()): print(g, dict(c), sum(c.values()))
sm=[o['sherlock_first_reply_min'] for o in out if o['sherlock_first_reply_min']!='']
print('sherlock replied', sum(o['sherlock_replied']=='Y' for o in out), 'n with minutes', len(sm), 'median', statistics.median(sm) if sm else None)
# weeks
def wk(s):
    d=dt.datetime.strptime(s[:16],'%Y-%m-%d %H:%M').date()
    start=dt.date(2026,9,18)
    return (d-start).days//7
cw=collections.Counter(wk(o['raised_ist']) for o in out)
print('weekly', sorted(cw.items()))
pre = [o for o in out if o['raised_ist']<'2026-09-23']
print('pre-sherlock threads', len(pre))
# repeat orders
oc=collections.Counter()
for o in out:
    for n in set(re.findall(r'\b\d{7}\b', o['order_ids'])): oc[n]+=1
print('orders in >1 thread', [(k,v) for k,v in oc.items() if v>1])
