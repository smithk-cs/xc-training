"""Roster import: 'Last, First' names, Fr/So/Jr/Sr grades, and existing athletes keep what the file leaves out."""
import pytest

from xcapp import imports as I
from xcapp.models import Athlete


def test_names_and_grades_list(session):
    session.add(Athlete(first_name="Sam", last_name="Lowry", gender="Boys", grade=11, group="Varsity", hr_max=201, hr_rest=44,
                        link_token="t1"))
    session.flush()
    text = 'Name,Gr,,\n,,,\n"Lowry, Sam",Sr,,\n"Quill, Theo",Fr,,\nPat Reed,10,,\n'
    with pytest.raises(I.ImportErrors) as e:      # new athletes need a gender
        I.import_roster(session, text)
    assert "add a gender (Boys or Girls) for new athlete 'Theo Quill'" in e.value.errors[0]
    text = text.replace("Name,Gr,,", "Name,Gr,gender,").replace("Fr,,", "Fr,Boys,").replace("10,,", "10,Boys,")
    assert I.import_roster(session, text) == 3
    people = {a.name: (a.gender, a.grade, a.group, a.hr_max, a.hr_rest) for a in session.query(Athlete)}
    assert people == {"Sam Lowry": ("Boys", 12, "Varsity", 201, 44),      # grade updated; group and heart rates kept
                      "Theo Quill": ("Boys", 9, "JV", None, None), "Pat Reed": ("Boys", 10, "JV", None, None)}
    with pytest.raises(I.ImportErrors):
        I.import_roster(session, "name,grade,gender\nAl Bo,Senior-ish,Boys\n")
