import datetime as dt

from fastapi.testclient import TestClient

from xcapp import analysis as A


def test_race_season_tabs_and_map_links():
    from xcapp.db import SessionLocal
    from xcapp.models import Athlete, MeetEntry, RaceResult
    from xcapp.web import create_app
    today = A.team_today()
    db = SessionLocal()
    a = Athlete(first_name="Sam", last_name="Lowry", gender="Boys", grade=12, group="Varsity", link_token="t-ex")
    db.add(a)
    db.flush()
    for d in (dt.date(2025, 10, 4), dt.date(2026, 10, 3)):
        db.add(RaceResult(athlete_id=a.id, meet=f"Invite {d.year}", date=d, race="Boys Varsity", distance_m=5000, distance_label="5K", time_s=1000))
    db.add(MeetEntry(name="Section Finals", date=today + dt.timedelta(days=5), location="Willow Hill Reservoir, Folsom CA"))
    db.commit()
    db.close()
    page = TestClient(create_app()).get("/a/t-ex").text
    assert page.count('class="year-tab"') == 2 and '>2026 XC</button>' in page and '>2025 XC</button>' in page
    assert page.index('>2026 XC</button>') < page.index('>2025 XC</button>')                 # newest first
    assert ('<a class="map-link" href="https://www.google.com/maps/search/?api=1&amp;query=Willow%20Hill%20Reservoir%2C%20Folsom%20CA"'
            in page)
    assert "dew point" not in page
