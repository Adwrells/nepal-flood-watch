"""Official relief-fund donation channels.

A deliberate design decision, and the most important one in this file:

    THIS MODULE LINKS TO OFFICIAL PORTALS. IT DOES NOT REPRODUCE BANK ACCOUNT
    NUMBERS, AND IT DOES NOT GENERATE QR CODES.

Three reasons, in order of weight:

1.  The Prime Minister's Office has publicly warned that individuals and groups
    are circulating unofficial QR codes and personal account numbers to collect
    "relief" money after the Bhote Koshi floods. An app that renders its own QR
    for people to scan is indistinguishable, to the person scanning it, from
    exactly that pattern. The safest thing this console can do is send people to
    the government's own payment page and tell them how to check it.

2.  A single transposed digit in an account number sends someone's money
    somewhere it cannot be recovered from. There is no error-handling for that.

3.  Account details change. A hardcoded copy goes stale silently, while a link
    always reflects what the government is publishing today.

Both destinations below were verified during development: pmdrf.nchl.com.np is
operated by Nepal Clearing House Ltd, the national payments infrastructure
operator, and is the gateway named in the PMO's own notice.
"""
from dataclasses import dataclass, asdict


@dataclass
class Channel:
    name: str
    operator: str
    url: str
    methods: str
    note: str = ""


OFFICIAL = [
    Channel(
        name="PM Disaster Relief Fund — official portal",
        operator="Nepal Clearing House Ltd (NCHL) for the Government of Nepal",
        url="https://pmdrf.nchl.com.np/",
        methods=("Cards, connectIPS, mobile banking, wallets, "
                 "NepalPay and Fonepay QR, eSewa, Alipay+, UPI-India, UnionPay"),
        note="The government's own donation page. QR codes shown here are the official ones.",
    ),
    Channel(
        name="PM Relief Fund — bank gateway",
        operator="Himalayan Bank, for the Government of Nepal",
        url="https://pmrelieffund.himalayanbank.com/",
        methods="International and domestic cards",
        note="Second gateway named in the PMO notice. Useful for donations from abroad.",
    ),
    Channel(
        name="Office of the Prime Minister",
        operator="Government of Nepal",
        url="https://www.opmcm.gov.np/",
        methods="Published bank accounts, SWIFT details and official QR codes",
        note="The authoritative list of accounts. Check here before any bank transfer.",
    ),
]

# The PMO's own verification rule, which is the single most useful thing a
# donor can be told.
SAFETY = {
    "headline": "Check the account name before you send anything",
    "rule": ("The recipient name must read \"Prime Minister Disaster Relief Fund\" "
             "or \"Disaster Relief Fund\". If it shows a person's name, it is not "
             "the government fund."),
    "warning": ("The Prime Minister's Office has warned that individuals and "
                "groups are circulating unofficial QR codes and personal account "
                "numbers to collect relief money."),
    "points": [
        "Use only QR codes shown on the official portals linked here.",
        "Never scan a relief QR forwarded through social media or messaging apps.",
        "Donations to the fund are non-refundable — check the amount and currency first.",
        "For queries, call the National Emergency Operation Centre on 1149.",
    ],
    "source": "Office of the Prime Minister and Council of Ministers, via The Rising Nepal",
}


def channels() -> dict:
    return {
        "channels": [asdict(c) for c in OFFICIAL],
        "safety": SAFETY,
        "policy": ("This console links to official government payment pages and "
                   "deliberately does not reproduce account numbers or generate "
                   "QR codes, because a self-generated relief QR is exactly the "
                   "pattern the PMO has warned the public about."),
    }
