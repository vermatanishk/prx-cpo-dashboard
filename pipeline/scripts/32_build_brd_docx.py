from docx import Document
from docx.shared import Pt, Inches, RGBColor
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

d = Document()
sec = d.sections[0]
sec.page_width = Inches(8.27); sec.page_height = Inches(11.69)
sec.left_margin = sec.right_margin = Inches(0.55)
sec.top_margin = sec.bottom_margin = Inches(0.5)
st = d.styles['Normal']; st.font.name = 'Calibri'; st.font.size = Pt(9)
st.element.rPr.rFonts.set(qn('w:eastAsia'), 'Calibri')
st.paragraph_format.space_after = Pt(2); st.paragraph_format.space_before = Pt(0); st.paragraph_format.line_spacing = 1.0
NAVY = RGBColor(0x1F, 0x38, 0x64)


def shade(cell, color):
    tcPr = cell._tc.get_or_add_tcPr(); sh = OxmlElement('w:shd')
    sh.set(qn('w:val'), 'clear'); sh.set(qn('w:color'), 'auto'); sh.set(qn('w:fill'), color); tcPr.append(sh)


def h(text):
    p = d.add_paragraph(); p.paragraph_format.space_before = Pt(6); p.paragraph_format.space_after = Pt(2)
    r = p.add_run(text); r.bold = True; r.font.size = Pt(10.5); r.font.color.rgb = NAVY


def para(text, size=None):
    p = d.add_paragraph(); r = p.add_run(text)
    if size: r.font.size = Pt(size)
    return p


def bullet(text, lead=None):
    p = d.add_paragraph(style='List Bullet'); p.paragraph_format.space_after = Pt(1)
    if lead:
        r = p.add_run(lead); r.bold = True
    p.add_run(text)


def table(rows, widths, size=8.5, bold_rows=()):
    t = d.add_table(rows=len(rows), cols=len(rows[0])); t.style = 'Table Grid'; t.autofit = False
    for i, row in enumerate(rows):
        for j, val in enumerate(row):
            c = t.cell(i, j); c.width = Inches(widths[j]); c.text = ''
            p = c.paragraphs[0]; p.paragraph_format.space_after = Pt(0)
            r = p.add_run(str(val)); r.font.size = Pt(size)
            if i == 0:
                r.bold = True; r.font.color.rgb = RGBColor(255, 255, 255); shade(c, '1F3864')
            elif i in bold_rows:
                r.bold = True; shade(c, 'E8EEF7')


t = d.add_paragraph(); r = t.add_run('BRD: Payment status views in Polaris'); r.bold = True; r.font.size = Pt(15); r.font.color.rgb = NAVY
para('Window: 18 Sep - 17 Oct 2026 (last 30 days)  |  Source: Zoho Desk tickets and every payment thread in Tech_yodas, read in full and checked against order and payment records  |  Cases: Payment_Cases_L30_Tech_Yodas.xlsx', 8.5)

h('1. Problem')
p = para('')
r = p.add_run('Top line: in the last 30 days support received 252 payment-stuck tickets in Zoho; 63 of them (25%) were then raised to Tech in Tech_yodas. Tech_yodas holds 114 payment cases in all, because calls and chat contacts also escalate there.'); r.bold = True
table([
    ['L30 funnel (18 Sep - 17 Oct)', 'Count', 'Note'],
    ['Zoho tickets: Payments and Refunds, six payment-stuck types', '252', '110 deducted twice, 85 refund delay, 20 not placed, 15 refund not credited, 13 wrong amount, 9 refund details'],
    ['...of which raised to Tech_yodas (matched by order id)', '63 (25%)', 'Deducted twice: 53 of 110 (48%). All other types: 10 of 142 (7%)'],
    ['Payment cases in Tech_yodas (all channels)', '114', '58 have a Zoho ticket for the same order; the rest came via calls, chat or sales'],
], [3.0, 0.8, 3.25])
para('When a customer says "my money was taken", the agent cannot see what happened to it, so the case goes to Tech. 61% of those 114 needed no engineering work, only visibility. Goal: the agent sees the payment and refund story at first contact and reassures the customer with proof, without escalating. This adds to the Transactions view; it does not replace it.')

h('2. The numbers')
table([
    ['114 payment cases in L30', 'Cases', 'Share', 'Engineering needed?', 'What is inside'],
    ['1. Solved by a view on data we already hold', '52', '46%', 'No', 'Themes 1, 3 (10), 4, 5, 6'],
    ['2. Solved once gateway-side payment and refund events are visible', '17', '15%', 'No, after data is added', 'Theme 2, plus 1 in theme 3'],
    ['Engineering not needed once views exist (1 + 2)', '69', '61%', 'No', 'Agent reassures and quotes proof: bank reference, date, order, refund status'],
    ['3. View diagnoses, fix needs Tech or Payments', '37', '32%', 'Yes', 'Themes 7, 8, 9'],
    ['4. Product or policy, not a visibility problem', '8', '7%', 'Yes', 'Theme 10'],
    ['Engineering still needed (3 + 4)', '45', '39%', 'Yes', 'With a view they arrive with the diagnosis and ids attached'],
], [1.9, 0.4, 0.4, 0.7, 3.65], bold_rows=(3, 6))
para('21 more Tech_yodas threads (OTP, return QC, delivery delay) were read and left out. Weekly cases: 26, 19, 28, 31.', 8.5)

h('3. Themes: what happened, and what we give the platform')
table([
    ['#', 'Theme (cases)', 'What actually happened', 'Add (see 4)', 'Agent can then say'],
    ['1', 'Paid twice, we hold one payment (27)', 'Second debit was a dropped attempt; the bank reverses it', 'A1', '"We received one payment, ref X. The other is a bank hold, it reverses on its own."'],
    ['2', 'Paid twice, money held at gateway (16)', 'Real duplicate capture or orphan payment our records never stored; 11 of 16 were refunded later', 'A1, A2', '"A second payment was captured; refund status is Y, ref Z."'],
    ['3', 'Paid, order missing or elsewhere (11)', 'Order sits under another phone or account; cart changed mid-payment so an auto-refund fired in minutes; COD placed after a failed online try', 'A3', '"Your payment created order N under this number" or "refunded, ref X".'],
    ['4', 'Refund journey unclear (5)', 'Refund failed with a reason, still within its time, or already paid to a UPI-linked account', 'A4', '"Refund of Rs X went to UPI Y on date Z, ref W."'],
    ['5', 'Refund status or amount confusion (8)', 'Order says Initiated though paid (3); refund lower than item price due to discount, coins, edits (5)', 'A5', '"It is paid; here is the breakdown."'],
    ['6', 'Other (2)', 'Card not supported; one return query', 'A1', ''],
    ['Subtotal', 'Engineering not needed (69, 61%)', 'Themes 1-6', '', ''],
    ['7', 'Refund payout failed or stuck (21)', 'Gateway rejected or never confirmed the payout; limits; retries used up', 'A4, A6', 'Not alone: view shows why; Payments re-initiates'],
    ['8', 'Paid, not linked to order (8)', 'Payment never attached to an order; stale cart; outage', 'A3, A6', 'Not alone: Tech maps or refunds'],
    ['9', 'Status, wallet, Edge fixes (8)', 'Status needs correcting; Edge fee or PRx cash not credited; amount shortfall', 'A5, A6', 'Not alone: Tech or Finance fixes'],
    ['10', 'Product / policy (8)', '6 returns blocked (Rs 5,000 cap, 10-day rule); 1 incident; 1 wallet-only order rejected', '-', 'Outside this BRD'],
    ['Subtotal', 'Engineering still needed (45, 39%)', 'Themes 7-10; with the view they arrive with diagnosis and ids', '', ''],
], [0.45, 1.55, 2.35, 0.55, 2.2], bold_rows=(7, 12))
para('Speed today: view-answerable cases close in a median 0.8 h but still take an engineer; gateway or fix cases take 2.5 to 4 days (33 of 105 measured took 3+ days). Repeats: order 4256603 raised 4 threads; 3846618 and 4360436, 3 each.', 8.5)

h('4. Requirements: what Polaris shows today, and what to add')
table([
    ['Polaris today (Customer 360, from a sample customer)', 'What it gives the agent'],
    ['User details, coin / cash balance', 'Who the customer is, order history, wallet balance'],
    ['Customer Orders', 'Order id, status (e.g. Refund Processed), tracking, created date'],
    ['Transactions', 'One row per payment attempt we recorded: method, created / updated time, cart id, order id, gateway, amount, status, error'],
], [2.9, 4.15])
para('Gap, seen in the sample: order 3299823 shows "Refund Processed", but Transactions lists only one failed attempt from a different cart (order id N/A). The order\'s own payment, the refund amount, where it went and the bank reference are not visible anywhere.', 9)
table([
    ['#', 'Add this', 'Solves', 'Pri'],
    ['A1', 'Transactions: add the payment reference and bank reference (UTR) on each attempt, so the agent can quote proof.', '43 deducted twice, 19 not linked', 'P0'],
    ['A2', 'Transactions: add the attempts the gateway recorded but we did not (dropped, abandoned, captured with no order), marked as such.', '17 gateway cases; completes the 27 now half-visible', 'P0'],
    ['A3', 'Transactions: show which order each payment created, or "no order", and let the agent search a payment by amount and time, UTR or payer UPI across accounts.', '19', 'P0'],
    ['A4', 'New Refunds list per order: amount, where it went (UPI / card / wallet / PRx cash), status, attempts and failure reason, dates, bank reference, expected-by date; flag failed and stuck.', '26 + 6', 'P0'],
    ['A5', 'Flag when refund list and order status disagree (refund paid, order says Initiated; approved refund never issued); show refund breakdown per item with the reason.', '12 + 6', 'P1'],
    ['A6', 'One-line verdict and what to tell the customer; for the 37 that need a fix, a prefilled escalation with ids, amounts and state.', 'All 114', 'P1'],
    ['A7', 'Earlier contacts and Tech threads on the same order or phone.', '5 orders, 2-4 threads each', 'P2'],
], [0.3, 4.4, 2.0, 0.35])

h('5. Success measures (baseline = L30; targets to be agreed)')
table([
    ['Measure', 'Baseline L30', 'Target'],
    ['Payment cases escalated to Tech', '114 (69 avoidable)', 'Avoidable cases down 80%+'],
    ['Cases a view can answer, resolved by the agent alone', '0 (all went to Tech)', 'All of the 69'],
    ['Cases open 3 days or more', '33 of 105 measured', 'Halve'],
], [3.1, 2.2, 1.75])

h('6. Open items')
bullet(' Tech and Payments to confirm how gateway attempt and refund data can reach Polaris. On 14 Oct Tech wrote that logs are on the gateway dashboard and support should self-serve; who has access today is unconfirmed.')
bullet(' "Stuck" thresholds to set with Payments. Observed: payments with no order are refunded within 17 h in 99% of cases; validated returns in a median 0.2 days. The 5-7 working-day bank window is Sherlock\'s figure, to confirm.')
bullet(' Tagging is my reading of thread text (dates, times, raisers match the Tech Issues Tracker; 53 orders ClickHouse-checked). Zoho-to-Tech match is by order id only (224 of 252 tickets have one); Zoho excludes calls and chat. Audit 30 cases per bucket before sign-off.')
d.save('/Users/tanishkverma/Desktop/CTO/deliverables/BRD_Polaris_Payment_Views.docx')
