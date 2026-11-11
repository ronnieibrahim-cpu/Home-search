"""WP10: digest rendering and delivery. Synthetic data only; no socket is opened."""

from __future__ import annotations

import email as email_lib
import importlib.util
import re
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import ClassVar

import pytest
from test_pipeline import offline  # noqa: F401 - fixture
from test_scoring import BELLAIRE_FULL, make_listing

from home_search.digest import render, send
from home_search.models import (
    Affordability,
    Candidate,
    ChangeEvent,
    FloodRisk,
    ListingKind,
    LoanCost,
    MarketSnapshot,
    RentVsBuy,
    SchoolZoning,
    ZoningConfidence,
)

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 11, 12, tzinfo=UTC)


def _load_sample():
    spec = importlib.util.spec_from_file_location("sample_digest",
                                                  ROOT / "scripts" / "sample_digest.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sample = _load_sample()


# ------------------------------------------------------------------ helpers


def cost(scenario="plan_market", pi=5_000.0) -> LoanCost:
    return LoanCost(scenario_id=scenario, down_payment=200_000, loan_amount=800_000,
                    rate=0.0675, is_jumbo=False, principal_interest=pi, property_tax=1_500,
                    homeowners_insurance=600, flood_insurance=0, pmi=0, hoa=0,
                    maintenance=800)


def rvb(verdict="buy", npv=45_000.0, breakeven=7) -> RentVsBuy:
    return RentVsBuy(horizon_years=12, rent_equivalent_monthly=5_100, rent_source="comp:2 rentals",
                     npv_buy_minus_rent=npv, breakeven_year=breakeven, verdict=verdict,
                     rationale=["Buying at $1,000,000 leaves you $45,000 better off.",
                                "Buying comes out ahead if you sell at the end of year 7."])


def cand(n=1, *, address=None, price=1_000_000, zoning=BELLAIRE_FULL, profile=True, **over):
    lst = make_listing(n, address=address, price=price, **over)
    c = Candidate(listing=lst, zoning=zoning, flood=FloodRisk(fema_zone="X"),
                  costs=[cost("conventional_20", 4_800), cost()], score=82.5,
                  score_parts={"schools": 100, "value": 70, "flood": 100, "fit": 100,
                               "carrying_cost": 50},
                  flags=["new"], rent_vs_buy=rvb())
    if profile:
        c.affordability = Affordability(
            cash_needed=230_000, cash_available_at_purchase=250_000, dti_back_end=0.25,
            plan_basis_monthly=8_100, within_comfort=False,
            blockers=["Monthly housing cost $8,100 is above the carry target of $8,000."])
    return c


def snap(area, week, count=10, price=1_000_000):
    return MarketSnapshot(area_id=area, week_of=week, active_count=count, median_price=price,
                          median_ppsf=330.0, median_dom=30, median_rent=5_200,
                          price_cut_share=0.2)


def advisor(**over) -> dict:
    adv = {
        "has_profile": True,
        "portfolio": {"count": 3, "verdicts": {"buy": 2, "close_call": 1, "rent": 0},
                      "buy_share": 2 / 3, "median_npv": 45_000.0, "median_breakeven_year": 7,
                      "never_break_even": 0,
                      "swing_factor": {"dimension": "app", "label": "home appreciation",
                                       "flip_share": 0.4, "mean_npv_range": 90_000.0}},
        "price_band": {"min": 800_000, "max": 1_200_000, "stretch_max": 1_260_000},
        "max_price": {"bellaire": {"plan": 1_111_000, "conventional_20": 1_055_000},
                      "memorial": {"plan": 1_099_000}},
        "close_date": date(2027, 6, 1),
        "cash_at_close": 254_321.0,
        "median_candidate_key": "x",
        "median_candidate_price": 1_000_000,
        "median_candidate_cash_needed": 230_000.0,
        "median_candidate_reserve_months": 14.25,
        "planning_price": 1_050_000,
        "plan_rate_bracket": {0.06: 5_036.0, 0.07: 5_589.0},
        "unverified": [],
        "unverified_total": 0,
        "candidates_total": 1,
        "dropped": {"floodway": 2},
        "stage_failures": {},
        "missing_boundaries": [],
        "assumptions_as_of": date(2026, 11, 11),
    }
    adv.update(over)
    return adv


class LinkCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs: list[str] = []
        self.tags: set[str] = set()

    def handle_starttag(self, tag, attrs):
        self.tags.add(tag)
        for k, v in attrs:
            if k == "href":
                self.hrefs.append(v)


def parse(html: str) -> LinkCollector:
    p = LinkCollector()
    p.feed(html)
    return p


# ------------------------------------------------------------------ formatting


def test_money_and_rates():
    assert render._money(1_234_567.4) == "$1,234,567"
    assert render._money(-1234.6) == "-$1,235"
    assert render._money(None) == "n/a"
    assert render._pct(0.0625) == "6.25%"
    assert render.scenario_label("conventional_20") == "Conventional 20% down"
    assert render.scenario_label("plan") == "Your plan"


@pytest.mark.parametrize("url,ok", [
    ("https://example.com/a?b=1", True),
    ("http://example.com/", True),
    ("javascript:alert(1)", False),
    ("JaVaScRiPt:alert(1)", False),
    (" javascript:alert(1)", False),
    ("data:text/html,x", False),
    ("//example.com/x", False),
    ("https://exa mple.com", False),
    ("", False),
    (None, False),
])
def test_safe_url(url, ok):
    assert (render.safe_url(url) is not None) is ok


# ------------------------------------------------------------------ weekly


def test_weekly_with_profile():
    c = cand(1, address="4501 Sample Ave")
    e = render.weekly([c], [], [], [snap("bellaire", date(2026, 11, 9))], advisor(), [])
    assert e.subject == ("Home search: 1 candidates, buying wins on 2 of 3 "
                         "(week of Nov 9, 2026)")
    for part in (e.html, e.text):
        assert "4501 Sample Ave" in part
        assert "$1,000,000" in part
        assert "Plan $1,111,000 (in band)" in part           # max price per scenario
        assert "$254,321" in part and "$230,000" in part     # runway
        assert "$24,321 spare" in part
        assert "14.2 months" in part or "14.3 months" in part
        assert "$5,036/mo at 6.00%" in part and "$5,589/mo at 7.00%" in part
        assert "home appreciation" in part and "40%" in part
        assert "Median break-even" in part
        assert "Monthly cost $7,900" in part                  # plan_market is primary
        assert "Your plan at today&#39;s market rate" in part or \
            "Your plan at today's market rate" in part
        assert "$8,100/mo plan basis" in part
        assert "above the carry target" in part
        assert "leaves you $45,000 better off" in part
        assert "Schools" in part or "schools" in part
        assert "Assumptions as of Nov 11, 2026." in part
        assert "in a floodway: 2" in part
        assert "zone X does not mean" in part
        assert "lender pre-approval" in part
    assert "first week of data" in e.text
    # The order of sections is the docstring's.
    heads = ["1. THE SHORT ANSWER", "2. TOP CANDIDATES", "3. WHAT CHANGED",
             "4. RENTALS", "5. MARKET TREND", "6. ZONING UNVERIFIED", "7. NOTES AND LIMITS"]
    pos = [e.text.index(h) for h in heads]
    assert pos == sorted(pos)
    hpos = [e.html.index(h) for h in
            ["1. The short answer", "2. Top candidates", "3. What changed",
             "4. Rentals worth", "5. Market trend", "6. Zoning unverified", "7. Notes and"]]
    assert hpos == sorted(hpos)


def test_weekly_without_profile_and_missing_keys():
    c = cand(1, profile=False)
    adv = {"has_profile": False, "portfolio": None, "max_price": {}, "cash_at_close": None,
           "plan_rate_bracket": {}, "unverified": None, "assumptions_as_of": None}
    e = render.weekly([c], [], [], [], adv, [])
    assert "No household profile loaded" in e.text
    assert "Runway" not in e.text and "What you can carry" not in e.text
    assert "plan basis" not in e.text
    assert "Assumptions not yet verified" in e.text
    # Every key missing at all, or None for the whole advisor.
    for empty in ({}, None):
        e2 = render.weekly([], [], [], [], empty, [])
        assert e2.subject.startswith("Home search: 0 candidates, no rent-vs-buy verdict yet")
        assert "No market snapshots yet." in e2.text


def test_weekly_zero_candidates_missing_boundaries():
    unv = [cand(i, address=f"{i} Unverified Rd",
                zoning=SchoolZoning(high="Bellaire High School",
                                    confidence=ZoningConfidence.LISTING_CLAIM))
           for i in (1, 2)]
    adv = advisor(candidates_total=0, unverified=unv, unverified_total=7,
                  missing_boundaries=["hisd.geojson", "sbisd.geojson"])
    outages = [("school boundaries: hisd.geojson missing or unusable; zoning in that "
                "district is unverified")]
    e = render.weekly([], [], [], [], adv, outages)
    summary = e.text.split("1. THE SHORT ANSWER")[1].split("2. TOP CANDIDATES")[0]
    first = summary.strip().splitlines()[1]
    assert first.startswith("!! No verified candidates yet.")
    assert "hisd.geojson, sbisd.geojson are missing" in first
    assert "NOT verified" in first
    assert e.html.index("No verified candidates yet.") < e.html.index("Buy or rent:")
    assert "Showing 2 of 7." in e.text
    assert "address lookup" in e.text
    assert "1 Unverified Rd" in e.text and "2 Unverified Rd" in e.text
    assert "Listing claim only (not verified)" in e.text
    assert outages[0] in e.text
    assert e.subject.startswith("Home search: 0 candidates + 7 unverified")


def test_changes_and_market_trend():
    c = cand(1, address="1 Cut Ln")
    p = cand(2, address="2 Pending Ln")
    at = NOW
    changes = [
        ChangeEvent(key=c.listing.key, kind="price_change", old="1100000", new="1050000", at=at),
        ChangeEvent(key=c.listing.key, kind="price_change", old="1050000", new="1000000", at=at),
        ChangeEvent(key=p.listing.key, kind="status_change", old="active", new="pending", at=at),
        ChangeEvent(key="sale|77401|9 other st", kind="new", new="900000", at=at),
    ]
    market = [snap("bellaire", date(2026, 11, 2), 10, 1_000_000),
              snap("bellaire", date(2026, 11, 9), 12, 1_050_000),
              snap("memorial", date(2026, 11, 9))]
    e = render.weekly([c, p], [], changes, market, advisor(candidates_total=2), [])
    assert "1 Cut Ln: $1,100,000 → $1,000,000 (-$100,000, -9.1%)" in e.text
    assert "Went pending or off market:" in e.text and "2 Pending Ln: active → pending" in e.text
    assert "1 other listing(s) changed" in e.text
    assert "change since Nov 2, 2026" in e.text
    assert "Active listings: 12 (+2)" in e.text
    assert "Median list price: $1,050,000 (+$50,000, +5.0%)" in e.text
    memorial = e.text.split("Spring Branch / Memorial, week of")[1]
    assert memorial.split("\n")[0].endswith("(first week of data):")


def test_rentals_section():
    r = cand(5, address="5 Lease Ln", kind=ListingKind.RENT, price=5_400, profile=False)
    r.costs, r.rent_vs_buy, r.score_parts = [], None, {}
    e = render.weekly([], [r], [], [], advisor(), [])
    rentals = e.text.split("4. RENTALS WORTH A LOOK")[1].split("5. MARKET")[0]
    assert "5 Lease Ln" in rentals and "$5,400/mo" in rentals


# ------------------------------------------------------------------ hot alert


def test_hot_alert():
    c = cand(1, address="7 Hot St", price=900_000)
    c.score = 88.0
    e = render.hot_alert(c)
    assert e.subject == "Hot listing: 7 Hot St $900,000 (score 88)"
    for part in (e.html, e.text):
        assert "7 Hot St" in part and "$900,000" in part
        assert "zone X does not mean" in part
        assert "address lookup" in part
        assert "lender pre-approval" in part
        assert "Bellaire High School" in part
    assert "https://example.test/sale/1" in parse(e.html).hrefs


# ------------------------------------------------------------------ escaping


HOSTILE = '<script>alert("x")</script> 1 "Quote" Ln \'&\' <b>'


def test_hostile_listing_text_is_escaped_and_bad_url_not_linked():
    c = cand(1, address=HOSTILE, description='<img src=x onerror="alert(1)"> nice "house"',
             url="javascript:alert(document.cookie)")
    c.flood = FloodRisk(fema_zone="X", notes=['<script>bad()</script>'])
    for e in (render.weekly([c], [c], [], [], advisor(), ['<script>outage</script>']),
              render.hot_alert(c)):
        assert "<script" not in e.html
        assert "<img" not in e.html and "<b>" not in e.html
        assert "&lt;script&gt;" in e.html
        assert "&#34;Quote&#34;" in e.html or "&quot;Quote&quot;" in e.html
        p = parse(e.html)
        assert not any(h.lower().lstrip().startswith("javascript") for h in p.hrefs)
        assert "javascript:alert(document.cookie)" in e.html      # shown as plain text
        assert all(h.startswith(("http://", "https://")) for h in p.hrefs)
        assert "script" not in p.tags and "img" not in p.tags
        assert "\n" not in e.subject and "\r" not in e.subject
        # Plain text keeps the text as is (it is never interpreted as markup).
        assert "not http/https, not linked" in e.text


def test_html_is_email_safe():
    e = render.weekly(**sample.build_sample())
    p = parse(e.html)
    assert "script" not in p.tags and "img" not in p.tags and "link" not in p.tags
    assert "<style" not in e.html
    assert "max-width:640px" in e.html
    assert "url(" not in e.html and "@import" not in e.html
    assert 'name="color-scheme"' in e.html
    assert all(h.startswith("https://") for h in p.hrefs)


# ------------------------------------------------------------------ plain text


def test_plain_text_has_same_candidates_in_same_order():
    s = sample.build_sample()
    e = render.weekly(**s)
    listings = ([c.listing for c in s["candidates"]] + [c.listing for c in s["rentals"]]
                + [c.listing for c in s["advisor"]["unverified"]])

    def order(text, changes_head):
        # Addresses also appear under "What changed"; compare the cards only.
        before, rest = text.split(changes_head, 1)
        rest = rest.split("Rentals worth" if "Rentals worth" in rest else "RENTALS WORTH", 1)[1]
        cards = before + rest
        return [cards.index(lst.address) for lst in listings]

    assert e.text.strip()
    assert order(e.text, "3. WHAT CHANGED") == sorted(order(e.text, "3. WHAT CHANGED"))
    html_text = re.sub(r"<[^>]+>", "", e.html)
    html_order = order(html_text, "3. What changed")
    assert html_order == sorted(html_order)
    for lst in listings:
        assert lst.url in e.text


# ------------------------------------------------------------------ realistic sample


def test_realistic_sample_digest(tmp_path):
    html_path, text_path = sample.write_sample(tmp_path)
    html, text = html_path.read_text(), text_path.read_text()
    assert text.startswith("Subject: [SAMPLE] Home search: 3 candidates + 2 unverified")
    assert "SAMPLE: synthetic listings" in html and "SAMPLE: synthetic listings" in text
    for addr in ("4501 Sample Ave", "4620 Example St", "812 Placeholder Ln",
                 "1350 Demo Ct", "6011 Mock Blvd", "4419 Fictional Dr", "905 Stand-in Way"):
        assert addr in html and addr in text
    assert "Bellaire, week of Nov 9, 2026" in text
    assert "Spring Branch / Memorial, week of Nov 9, 2026" in text
    assert "change since Nov 2, 2026" in text
    assert "Price cuts:" in text and "$1,099,000 → $1,049,000" in text
    assert "What you can carry:" in text and "Runway:" in text and "Rate bracket:" in text
    assert "Swing factor:" in text
    assert "redfin_csv: HTTP 403" in text
    assert "Listing claim only (not verified)" in text
    assert "Verified from the district boundary map" in text
    # Section 2 holds the three verified candidates, ranked.
    top = text.split("2. TOP CANDIDATES")[1].split("3. WHAT CHANGED")[0]
    assert [ln.split(". ", 1)[0] for ln in top.splitlines() if re.match(r"^\d\. ", ln)] == \
        ["1", "2", "3"]


# ------------------------------------------------------------------ send


@pytest.fixture
def no_creds(monkeypatch, tmp_path):
    for name in ("GMAIL_USER", "GMAIL_APP_PASSWORD", "DIGEST_TO"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(send, "OUT_DIR", tmp_path / "out")

    def no_socket(*a, **k):
        raise AssertionError("SMTP must not be used")

    monkeypatch.setattr(send, "SMTP", no_socket)
    return tmp_path / "out"


class FakeSMTP:
    instances: ClassVar[list[FakeSMTP]] = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.calls, self.sent = host, port, [], []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.calls.append("quit")
        return False

    def ehlo(self):
        self.calls.append("ehlo")

    def starttls(self, context=None):
        assert context is not None
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def send_message(self, msg, from_addr=None, to_addrs=None):
        self.sent.append((msg, from_addr, to_addrs))


EMAIL = render.Email(subject="Home search: 1 candidates, buying wins on 1 of 1 (week of x)",
                     html="<p>secret $123,456</p>", text="secret $123,456")


def test_dry_run_writes_both_files(no_creds, monkeypatch, caplog):
    monkeypatch.setenv("GMAIL_USER", "someone@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-password")
    caplog.set_level("DEBUG")
    path = Path(send.send(EMAIL, dry_run=True))
    assert path.parent == no_creds and path.suffix == ".html"
    assert path.name.endswith("-home-search.html")
    assert path.read_text() == EMAIL.html
    assert path.with_suffix(".txt").read_text() == EMAIL.text
    again = Path(send.send(EMAIL, dry_run=True))        # same second: no overwrite
    assert again != path and again.exists()
    assert "123,456" not in caplog.text and "buying wins" not in caplog.text


def test_no_credentials_returns_file_path(no_creds, capsys):
    out = send.send(EMAIL)
    assert out != send.SENT and Path(out).exists()
    assert "123,456" not in capsys.readouterr().out


def test_send_with_fake_smtp(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(send, "OUT_DIR", tmp_path / "out")
    monkeypatch.setattr(send, "SMTP", FakeSMTP)
    FakeSMTP.instances.clear()
    monkeypatch.setenv("GMAIL_USER", "sender@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-password")
    monkeypatch.setenv("DIGEST_TO", "reader@example.com, other@example.com")
    caplog.set_level("DEBUG")
    assert send.send(EMAIL) == "sent"
    (smtp,) = FakeSMTP.instances
    assert (smtp.host, smtp.port) == ("smtp.gmail.com", 587)
    assert smtp.calls[:4] == ["ehlo", "starttls", "ehlo",
                              ("login", "sender@example.com", "app-password")]
    (msg, from_addr, to_addrs), = smtp.sent
    assert from_addr == "sender@example.com"
    assert to_addrs == ["reader@example.com", "other@example.com"]
    parsed = email_lib.message_from_bytes(msg.as_bytes())
    assert parsed.get_content_type() == "multipart/alternative"
    types = [p.get_content_type() for p in parsed.get_payload()]
    assert types == ["text/plain", "text/html"]
    assert parsed["Subject"] == EMAIL.subject
    assert not (tmp_path / "out").exists()
    assert "123,456" not in caplog.text and "reader@" not in caplog.text


def test_digest_to_defaults_to_user(monkeypatch, tmp_path):
    monkeypatch.setattr(send, "SMTP", FakeSMTP)
    FakeSMTP.instances.clear()
    monkeypatch.setenv("GMAIL_USER", "sender@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "pw")
    monkeypatch.delenv("DIGEST_TO", raising=False)
    send.send(EMAIL)
    (_, _, to_addrs), = FakeSMTP.instances[0].sent
    assert to_addrs == ["sender@example.com"]


def test_smtp_failure_raises_without_body(monkeypatch, tmp_path):
    import smtplib

    class Failing(FakeSMTP):
        def login(self, user, password):
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials for sender@example.com")

    monkeypatch.setattr(send, "OUT_DIR", tmp_path / "out")
    monkeypatch.setattr(send, "SMTP", Failing)
    monkeypatch.setenv("GMAIL_USER", "sender@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "pw")
    with pytest.raises(send.DeliveryError) as info:
        send.send(EMAIL)
    msg = str(info.value)
    assert "SMTPAuthenticationError" in msg and "535" in msg
    assert "123,456" not in msg and "buying wins" not in msg and "sender@" not in msg
    assert info.value.__cause__ is None and info.value.__suppress_context__
    assert list((tmp_path / "out").glob("*.html"))          # saved for the reader


# ------------------------------------------------------------------ CLI, end to end


def test_cli_digest_dry_run_prints_nothing_profile_derived(offline, monkeypatch, capsys,  # noqa: F811
                                                           caplog):
    from home_search import cli

    out_dir = offline / "out"
    monkeypatch.setattr(send, "OUT_DIR", out_dir)
    caplog.set_level("DEBUG")
    rc = cli.main(["digest", "--dry-run", "--db", str(offline / "state.sqlite")])
    out = capsys.readouterr()
    assert rc == 0, out.err
    assert out.out.startswith("digest complete:")
    (html_path,) = out_dir.glob("*-home-search.html")
    assert html_path.with_suffix(".txt").exists()
    text = html_path.with_suffix(".txt").read_text()
    assert "1. THE SHORT ANSWER" in text and "Runway:" in text      # profile reached the email
    for value in ("43,210", "43210", "345,678", "4,321", "7,654", "8,765", "$"):
        assert value not in out.out and value not in out.err
        assert value not in caplog.text
