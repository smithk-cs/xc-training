"""An existing database gets the columns added since it was made (the live app's database is never rebuilt)."""
import ast
import pathlib

from sqlalchemy import inspect, text


def test_added_columns_lists_each_table_once():
    src = pathlib.Path(__file__).resolve().parents[1] / "xcapp" / "db.py"
    tree = ast.parse(src.read_text())
    node = next(n.value for n in ast.walk(tree) if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "ADDED_COLUMNS")
    keys = [k.value for k in node.keys]
    assert len(keys) == len(set(keys)), f"a table is listed twice, so its first entry is ignored: {keys}"


def test_older_database_gets_new_columns():
    from xcapp import db as D
    eng = D.engine()
    with eng.begin() as conn:  # as the database was before weekly mileage goals and group colors
        conn.execute(text("ALTER TABLE training_groups DROP COLUMN weekly_miles"))
        conn.execute(text("ALTER TABLE training_groups DROP COLUMN color"))
    D.create_all()
    insp = inspect(eng)
    for table, cols in D.ADDED_COLUMNS.items():
        have = {c["name"] for c in insp.get_columns(table)}
        assert {n for n, _ in cols} <= have, table
    s = D.SessionLocal()
    from xcapp.models import TrainingGroup
    assert s.query(TrainingGroup).first().weekly_miles == 0
    s.close()


def test_girls_start_on_the_shared_weekly_goal():
    from xcapp import db as D
    eng = D.engine()
    with eng.begin() as conn:  # as the database was when boys and girls shared one weekly mileage goal
        conn.execute(text("ALTER TABLE training_groups DROP COLUMN weekly_miles_girls"))
        conn.execute(text("UPDATE training_groups SET weekly_miles = 35 WHERE name = 'Gold'"))
    D.create_all()
    s = D.SessionLocal()
    from xcapp.models import TrainingGroup
    gold = s.query(TrainingGroup).filter_by(name="Gold").one()
    assert (gold.weekly_miles, gold.weekly_miles_girls) == (35, 35)
    assert s.query(TrainingGroup).filter_by(name="Silver").one().weekly_miles_girls == 0
    gold.weekly_miles_girls = 28           # a coach's own girls' goal is kept on the next start
    s.commit()
    D.create_all()
    s.expire_all()
    assert s.query(TrainingGroup).filter_by(name="Gold").one().weekly_miles_girls == 28
    s.close()
