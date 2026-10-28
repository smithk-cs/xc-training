

def test_vo2_max_column_is_kept():
    from xcapp import garmin_csv as g
    data = b"Activity Type,Date,Title,Distance,Time,Avg HR,Max HR,VO2 Max\nRunning,2026-10-27 15:28:54,Run,4.39,00:38:54,165,182,54\n"
    runs, skipped, unit = g.parse(data)
    assert runs[0].vo2_max == 54 and unit == "miles"
