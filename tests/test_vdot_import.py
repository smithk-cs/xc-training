"""Setting coach VDOTs from a pasted list, such as the team's VDOT sheet. All names are made up."""
from xcapp import imports as I
from xcapp.models import Athlete

SHEET = """Platinum\tVDOT 73 +\teasy: 6:17\ttempo: 5:02
Owen Ashby\t73\t\t7x8=56
Running Trax
mile pace calculator
Gold\tVDOT 68-72\teasy: 6:21-6:39\ttempo: 5:05-5:20
William Pell\t70\t\t7x8=56
Connor Vale\t62\t*\t7x7=49
Nicky Dorn\t59\t\t6x6=36
Luke Mathews\t51\t\t6x5=30
Wes Harlan\t\t\tinj
Max Okafor\t54\t\t6x6=36
Theo Tester 48
"""


def test_import_vdots(session):
    people = [("Owen", "Ashby"), ("Will", "Pell"), ("Connor", "Vale"), ("Nick", "Dorn"), ("Luke", "Matthews"),
              ("Wes", "Harlan"), ("Theo", "Tester")]
    for i, (f, l) in enumerate(people):
        session.add(Athlete(first_name=f, last_name=l, gender="Boys", grade=11, group="JV", link_token=f"t{i}",
                            vdot_override=40 if f == "Wes" else None))
    session.flush()
    r = I.import_vdots(session, SHEET)
    got = {a.name: a.vdot_override for a in session.query(Athlete)}
    assert got == {"Owen Ashby": 73, "Will Pell": 70, "Connor Vale": 62, "Nick Dorn": 59, "Luke Matthews": 51,
                   "Wes Harlan": 40, "Theo Tester": 48}
    assert [(a.name, w) for a, _, w in r["loose"]] == [("Will Pell", "William Pell"), ("Nick Dorn", "Nicky Dorn"),
                                                       ("Luke Matthews", "Luke Mathews")]
    assert r["unmatched"] == ["Max Okafor"] and r["no_vdot"] == ["Wes Harlan"] and len(r["set"]) == 3
