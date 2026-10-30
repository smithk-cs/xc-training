"""Database tables. Distances in meters, durations in seconds, times stored in UTC.

No GPS positions are stored anywhere: athletes are minors, and routes reveal where they live.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    Boolean, Date, DateTime, Float, ForeignKey, Integer, LargeBinary, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


class StaffUser(Base):
    __tablename__ = "staff_users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True)
    password_hash: Mapped[str] = mapped_column(String(300))
    role: Mapped[str] = mapped_column(String(20))  # admin | coach | trainer
    email: Mapped[str] = mapped_column(String(254), default="")
    full_name: Mapped[str] = mapped_column(String(120), default="")
    title: Mapped[str] = mapped_column(String(80), default="")      # such as Head Coach
    phone: Mapped[str] = mapped_column(String(30), default="")      # cell phone, shown on meet entry sheets
    on_entries: Mapped[bool] = mapped_column(Boolean, default=True)  # listed as a contact on meet entry sheets
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    @property
    def display_name(self) -> str:
        return self.full_name or self.username


class Athlete(Base):
    __tablename__ = "athletes"
    id: Mapped[int] = mapped_column(primary_key=True)
    first_name: Mapped[str] = mapped_column(String(80))
    last_name: Mapped[str] = mapped_column(String(80))
    gender: Mapped[str] = mapped_column(String(10))  # Boys | Girls
    grade: Mapped[int] = mapped_column(Integer)
    group: Mapped[str] = mapped_column(String(30), default="JV")  # Varsity | JV | Development
    hr_max: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hr_rest: Mapped[int | None] = mapped_column(Integer, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    email: Mapped[str] = mapped_column(String(254), default="")  # optional; for contacting the athlete about the team
    other_names: Mapped[str] = mapped_column(String(500), default="")  # other spellings, separated by ';', matched on upload
    grad_year: Mapped[int | None] = mapped_column(Integer, nullable=True)  # class year; grade follows from it each school year
    alumni: Mapped[bool] = mapped_column(Boolean, default=False)           # graduated: off the roster, all data kept as history
    email_reminders: Mapped[bool] = mapped_column(Boolean, default=False)  # opted in to daily reminder emails
    is_test: Mapped[bool] = mapped_column(Boolean, default=False)  # a staff test account: left out of team numbers and the AI
    left_on: Mapped[dt.date | None] = mapped_column(Date, nullable=True)  # set when the athlete leaves the team (active is False)
    vdot_override: Mapped[int | None] = mapped_column(Integer, nullable=True)  # set by a coach; replaces the race VDOT
    vdot_mark: Mapped[str] = mapped_column(String(10), default="")     # coach's mark on the VDOT page, such as *
    weekly_plan: Mapped[str] = mapped_column(String(20), default="")   # coach's weekly mileage plan, such as 7x8 or inj
    experience: Mapped[int] = mapped_column(Integer, default=2, server_default="2")  # 1 new to running, 2 usual, 3 experienced
    # What the weekly newsletter may show about this athlete (minors: off until a coach records consent).
    show_name: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")       # name, and race times with it
    show_photo: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    show_spotlight: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    show_vdot: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")       # a numeric VDOT in shared views
    consent_by: Mapped[str] = mapped_column(String(160), default="", server_default="")       # who recorded it, and on what basis
    consent_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    ability_override: Mapped[str] = mapped_column(String(40), default="")  # coach-pinned ability group, "" = automatic
    ai_consent_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)  # explicit opt-in to AI analysis
    ai_consent_note: Mapped[str] = mapped_column(String(200), default="")
    link_token: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    garmin: Mapped["GarminConnection | None"] = relationship(back_populates="athlete", cascade="all, delete-orphan", uselist=False)
    activities: Mapped[list["Activity"]] = relationship(back_populates="athlete", cascade="all, delete-orphan")
    results: Mapped[list["Result"]] = relationship(cascade="all, delete-orphan")
    goals: Mapped[list["Goal"]] = relationship(cascade="all, delete-orphan")
    injuries: Mapped[list["Injury"]] = relationship(back_populates="athlete", cascade="all, delete-orphan")
    pain_reports: Mapped[list["PainReport"]] = relationship(back_populates="athlete", cascade="all, delete-orphan")
    resting_hrs: Mapped[list["RestingHeartRate"]] = relationship(cascade="all, delete-orphan")
    vdot_changes: Mapped[list["VdotChange"]] = relationship(cascade="all, delete-orphan", order_by="VdotChange.at")
    entries: Mapped[list["DivisionAthlete"]] = relationship(cascade="all, delete-orphan", overlaps="athlete")
    meet_responses: Mapped[list["MeetResponse"]] = relationship(cascade="all, delete-orphan", overlaps="athlete")
    photo: Mapped["AthletePhoto | None"] = relationship(cascade="all, delete-orphan", uselist=False, lazy="select")

    @property
    def name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def code(self) -> str:
        """Stable id used on the dashboard (A01, A02, ...)."""
        return f"A{self.id:02d}"


class GarminConnection(Base):
    __tablename__ = "garmin_connections"
    id: Mapped[int] = mapped_column(primary_key=True)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id", ondelete="CASCADE"), unique=True)
    garmin_user_id: Mapped[str] = mapped_column(String(100), unique=True)
    access_token_enc: Mapped[str] = mapped_column(Text, default="")
    refresh_token_enc: Mapped[str] = mapped_column(Text, default="")
    access_expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    refresh_expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    permissions: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="connected")  # connected | revoked | error
    consent_note: Mapped[str] = mapped_column(Text, default="")
    consent_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    connected_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    last_sync_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    athlete: Mapped[Athlete] = relationship(back_populates="garmin")


class GarminBackfill(Base):
    """One waiting request to Garmin for past runs (Summary Backfill). Sent at most once a minute, retried if Garmin
    is not ready yet; Garmin then delivers the runs through the notification endpoints."""
    __tablename__ = "garmin_backfills"
    id: Mapped[int] = mapped_column(primary_key=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("garmin_connections.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(30))  # activities | activityDetails
    start_at: Mapped[dt.datetime] = mapped_column(DateTime)
    end_at: Mapped[dt.datetime] = mapped_column(DateTime)
    not_before: Mapped[dt.datetime] = mapped_column(DateTime, index=True)
    status: Mapped[str] = mapped_column(String(10), default="waiting")  # waiting | sent | failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_result: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    connection: Mapped[GarminConnection] = relationship()


class OAuthState(Base):
    __tablename__ = "oauth_states"
    state: Mapped[str] = mapped_column(String(100), primary_key=True)
    verifier: Mapped[str] = mapped_column(String(200))
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id", ondelete="CASCADE"))
    consent_note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class PlannedWorkout(Base):
    __tablename__ = "planned_workouts"
    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[dt.date] = mapped_column(Date)
    title: Mapped[str] = mapped_column(String(120))
    rep_distance_m: Mapped[float] = mapped_column(Float)  # always meters; distance_unit only sets how it's shown
    distance_unit: Mapped[str] = mapped_column(String(2), default="m")  # m (interval reps) | mi (runs such as an easy 5 miles)
    # Parts as JSON: [{"sets": 2, "reps": 4, "dist": 400, "zone": "Interval", "rest": "90 s jog"}, ...]. Empty for older
    # workouts, which use rep_distance_m with pace_event x pace_factor instead of VDOT paces.
    parts: Mapped[str] = mapped_column(Text, default="")
    pace_event: Mapped[str] = mapped_column(String(20), default="5K")
    pace_factor: Mapped[float] = mapped_column(Float, default=1.0)
    groups: Mapped[str] = mapped_column(String(120), default="All")  # "All" or comma list
    gender: Mapped[str] = mapped_column(String(10), default="", server_default="")  # Boys | Girls | "" for both

    @property
    def part_list(self) -> list[dict]:
        import json
        from .analysis import RENAMED_ZONES
        try:
            parts = json.loads(self.parts) if self.parts else []
        except ValueError:
            return []
        for p in parts:  # workouts saved before the zones were renamed
            p["zone"] = RENAMED_ZONES.get(p.get("zone"), p.get("zone"))
        return parts

    @property
    def in_miles(self) -> bool:
        return self.distance_unit == "mi"

    @property
    def distance_value(self) -> float:
        """The distance in the unit it was entered in (1000 for 1000 m, 5 for 5 miles)."""
        return round(self.rep_distance_m / 1609.344, 2) if self.in_miles else round(self.rep_distance_m)

    @property
    def distance_text(self) -> str:
        return f"{self.distance_value:g} mi" if self.in_miles else f"{int(self.distance_value):,} m"

    def applies_to(self, *groups: str, gender: str = "") -> bool:
        """For an athlete in any of these groups (and of this gender, when the workout is for boys or girls only)."""
        if self.gender and gender and gender != self.gender:
            return False
        g = [x.strip() for x in (self.groups or "All").split(",") if x.strip()]
        return not g or "All" in g or any(x in g for x in groups if x)


class Activity(Base):
    __tablename__ = "activities"
    __table_args__ = (UniqueConstraint("athlete_id", "external_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(30))  # garmin_file | garmin_details | garmin_summary | upload | demo
    external_id: Mapped[str] = mapped_column(String(120))
    start_utc: Mapped[dt.datetime] = mapped_column(DateTime, index=True)
    local_date: Mapped[dt.date] = mapped_column(Date, index=True)
    duration_s: Mapped[float] = mapped_column(Float, default=0)
    distance_m: Mapped[float] = mapped_column(Float, default=0)
    avg_hr: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_hr: Mapped[float | None] = mapped_column(Float, nullable=True)
    sport: Mapped[str] = mapped_column(String(40), default="running")
    device: Mapped[str] = mapped_column(String(120), default="")
    vo2_max: Mapped[float | None] = mapped_column(Float, nullable=True)  # the watch's estimate after this run; display only
    planned_workout_id: Mapped[int | None] = mapped_column(ForeignKey("planned_workouts.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    # Added by the athlete after the run (never sent to the AI as free text)
    rpe: Mapped[int | None] = mapped_column(Integer, nullable=True)  # effort 0-10 (CR-10 scale)
    feel: Mapped[str] = mapped_column(String(20), default="")  # Fresh | Normal | Heavy | Sore
    athlete_note: Mapped[str] = mapped_column(String(300), default="")
    feedback_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    run_type: Mapped[str] = mapped_column(String(20), default="")  # Easy | Long | Workout | Race | Recovery | Other; "" = not set
    workout_locked: Mapped[bool] = mapped_column(Boolean, default=False)  # workout chosen by hand; automatic matching leaves it
    # The AI's check of this run against its team workout: Done | Partly | Not done ("" = not checked yet)
    workout_check: Mapped[str] = mapped_column(String(12), default="")
    workout_check_note: Mapped[str] = mapped_column(String(400), default="")
    workout_check_for: Mapped[int | None] = mapped_column(Integer, nullable=True)  # the workout it was checked against
    workout_checked_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    # Seconds in each pace zone as JSON ({"Easy": 1800, "Threshold": 600, "walk": 40}), worked out when the run arrived
    # with the VDOT the athlete had then (zone_vdot), so later VDOT changes don't move old points.
    zone_seconds: Mapped[str] = mapped_column(Text, default="")
    zone_vdot: Mapped[int | None] = mapped_column(Integer, nullable=True)

    athlete: Mapped[Athlete] = relationship(back_populates="activities")
    laps: Mapped[list["Lap"]] = relationship(back_populates="activity", cascade="all, delete-orphan", order_by="Lap.idx")
    planned: Mapped[PlannedWorkout | None] = relationship()


class Lap(Base):
    __tablename__ = "laps"
    id: Mapped[int] = mapped_column(primary_key=True)
    activity_id: Mapped[int] = mapped_column(ForeignKey("activities.id", ondelete="CASCADE"), index=True)
    idx: Mapped[int] = mapped_column(Integer)
    start_offset_s: Mapped[float] = mapped_column(Float)
    elapsed_s: Mapped[float] = mapped_column(Float)
    timer_s: Mapped[float] = mapped_column(Float)
    distance_m: Mapped[float] = mapped_column(Float)
    avg_hr: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_hr: Mapped[float | None] = mapped_column(Float, nullable=True)
    intensity: Mapped[str] = mapped_column(String(20), default="")
    trigger: Mapped[str] = mapped_column(String(20), default="")
    is_rep: Mapped[bool] = mapped_column(Boolean, default=False)
    rep_num: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hr_drop_60s: Mapped[float | None] = mapped_column(Float, nullable=True)
    activity: Mapped[Activity] = relationship(back_populates="laps")


class Result(Base):
    """A benchmark result: race or time trial, normalized to 1 Mile, 1.5 Mile, 2 Mile or 5K."""
    __tablename__ = "results"
    id: Mapped[int] = mapped_column(primary_key=True)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id", ondelete="CASCADE"), index=True)
    event: Mapped[str] = mapped_column(String(20))
    time_s: Mapped[float] = mapped_column(Float)
    date: Mapped[dt.date] = mapped_column(Date)
    season: Mapped[str] = mapped_column(String(20))
    course: Mapped[str] = mapped_column(String(120), default="")
    kind: Mapped[str] = mapped_column(String(20), default="race")  # race | time trial | track
    note: Mapped[str] = mapped_column(String(200), default="")


class RaceResult(Base):
    """One athlete's result in one race of a meet, at the race's real distance (5,000 m, 4K, 3 miles, ...).
    Races at a benchmark distance also get a linked Result, which feeds VDOT and running groups."""
    __tablename__ = "race_results"
    id: Mapped[int] = mapped_column(primary_key=True)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id", ondelete="CASCADE"), index=True)
    meet: Mapped[str] = mapped_column(String(160))
    date: Mapped[dt.date] = mapped_column(Date, index=True)
    course: Mapped[str] = mapped_column(String(160), default="")
    race: Mapped[str] = mapped_column(String(120), default="")  # division, e.g. "Boys Varsity Large School"
    distance_m: Mapped[float] = mapped_column(Float)
    distance_label: Mapped[str] = mapped_column(String(40), default="")  # as written in the results, e.g. "5,000 Meters"
    time_s: Mapped[float] = mapped_column(Float)
    points: Mapped[float | None] = mapped_column(Float, nullable=True)  # team-scoring points, when the results give them
    place: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result_id: Mapped[int | None] = mapped_column(ForeignKey("results.id", ondelete="SET NULL"), nullable=True)
    athlete: Mapped[Athlete] = relationship()


class Goal(Base):
    __tablename__ = "goals"
    __table_args__ = (UniqueConstraint("athlete_id", "event", "season"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id", ondelete="CASCADE"), index=True)
    event: Mapped[str] = mapped_column(String(20))
    season: Mapped[str] = mapped_column(String(20))
    goal_s: Mapped[float] = mapped_column(Float)


class Injury(Base):
    __tablename__ = "injuries"
    id: Mapped[int] = mapped_column(primary_key=True)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id", ondelete="CASCADE"), index=True)
    onset_date: Mapped[dt.date] = mapped_column(Date)
    injury: Mapped[str] = mapped_column(String(120))
    body_area: Mapped[str] = mapped_column(String(60))
    return_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)  # expected or actual
    notes: Mapped[str] = mapped_column(Text, default="")  # visible to trainers and admins only; never sent to AI
    logged_by: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    athlete: Mapped[Athlete] = relationship(back_populates="injuries")


class PainReport(Base):
    __tablename__ = "pain_reports"
    id: Mapped[int] = mapped_column(primary_key=True)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id", ondelete="CASCADE"), index=True)
    date: Mapped[dt.date] = mapped_column(Date)
    body_area: Mapped[str] = mapped_column(String(60))
    severity: Mapped[int] = mapped_column(Integer)  # 0-10
    note: Mapped[str] = mapped_column(String(1000), default="")  # athletes type up to 300; merged reports keep every note
    saw_trainer: Mapped[bool] = mapped_column(Boolean, default=False)  # the athlete says they've seen the athletic trainer
    activity_id: Mapped[int | None] = mapped_column(ForeignKey("activities.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    athlete: Mapped[Athlete] = relationship(back_populates="pain_reports")


class AthletePhoto(Base):
    """An athlete's roster photo, stored as a small JPEG with no metadata."""
    __tablename__ = "athlete_photos"
    id: Mapped[int] = mapped_column(primary_key=True)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id", ondelete="CASCADE"), unique=True)
    media_type: Mapped[str] = mapped_column(String(30), default="image/jpeg")
    data: Mapped[bytes] = mapped_column(LargeBinary)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class MeetEntry(Base):
    """A meet the team is entering, with the race director to send entries to."""
    __tablename__ = "meet_entries"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    date: Mapped[dt.date] = mapped_column(Date, index=True)
    location: Mapped[str] = mapped_column(String(160), default="")
    director_name: Mapped[str] = mapped_column(String(120), default="")
    director_email: Mapped[str] = mapped_column(String(254), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    course_id: Mapped[int | None] = mapped_column(ForeignKey("courses.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    course: Mapped["Course | None"] = relationship()
    divisions: Mapped[list["MeetDivision"]] = relationship(back_populates="meet", cascade="all, delete-orphan",
                                                           order_by="MeetDivision.position")
    responses: Mapped[list["MeetResponse"]] = relationship(cascade="all, delete-orphan")


class MeetDivision(Base):
    """One race at a meet (such as Boys Varsity 5K) and the athletes entered in it."""
    __tablename__ = "meet_divisions"
    id: Mapped[int] = mapped_column(primary_key=True)
    meet_id: Mapped[int] = mapped_column(ForeignKey("meet_entries.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    distance: Mapped[str] = mapped_column(String(40), default="")
    gender: Mapped[str] = mapped_column(String(10), default="Boys")      # Boys | Girls
    level: Mapped[str] = mapped_column(String(20), default="Varsity")    # Varsity | JV | Frosh/Soph | Freshman | Sophomore
    max_entries: Mapped[int | None] = mapped_column(Integer, nullable=True)  # the meet's limit for this race, if any
    position: Mapped[int] = mapped_column(Integer, default=0)
    meet: Mapped[MeetEntry] = relationship(back_populates="divisions")
    entries: Mapped[list["DivisionAthlete"]] = relationship(back_populates="division", cascade="all, delete-orphan",
                                                            order_by="DivisionAthlete.position")


class MeetResponse(Base):
    """Whether an athlete will be at a meet: confirmed by the athlete, or marked absent by the athlete or a coach."""
    __tablename__ = "meet_responses"
    __table_args__ = (UniqueConstraint("meet_id", "athlete_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    meet_id: Mapped[int] = mapped_column(ForeignKey("meet_entries.id", ondelete="CASCADE"), index=True)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(10))  # attending | absent
    reason: Mapped[str] = mapped_column(String(300), default="")
    by: Mapped[str] = mapped_column(String(10), default="athlete")  # athlete | coach
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    athlete: Mapped[Athlete] = relationship()


class DivisionAthlete(Base):
    __tablename__ = "division_athletes"
    __table_args__ = (UniqueConstraint("division_id", "athlete_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    division_id: Mapped[int] = mapped_column(ForeignKey("meet_divisions.id", ondelete="CASCADE"), index=True)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    division: Mapped[MeetDivision] = relationship(back_populates="entries")
    athlete: Mapped[Athlete] = relationship()


class VdotChange(Base):
    """Each time a coach sets or clears an athlete's VDOT: the history behind charts, improvement arrows and undo."""
    __tablename__ = "vdot_changes"
    id: Mapped[int] = mapped_column(primary_key=True)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id", ondelete="CASCADE"), index=True)
    date: Mapped[dt.date] = mapped_column(Date, index=True)          # team date of the change
    at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    vdot: Mapped[int | None] = mapped_column(Integer, nullable=True)      # None: cleared (back to the race VDOT)
    previous: Mapped[int | None] = mapped_column(Integer, nullable=True)
    by: Mapped[str] = mapped_column(String(80), default="")
    source: Mapped[str] = mapped_column(String(20), default="")      # athlete page | VDOT page | pasted list


class Announcement(Base):
    """A coach's message shown at the top of athletes' pages between its start and end dates."""
    __tablename__ = "announcements"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(120))
    body: Mapped[str] = mapped_column(Text, default="")
    start_date: Mapped[dt.date] = mapped_column(Date, index=True)
    end_date: Mapped[dt.date] = mapped_column(Date, index=True)
    audience: Mapped[str] = mapped_column(String(40), default="")  # "" everyone | Boys | Girls | a running group's name
    created_by: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    def for_athlete(self, gender: str, group: str) -> bool:
        return not self.audience or self.audience in (gender, group)


class RestingHeartRate(Base):
    """An athlete's resting heart rate for one day: from Garmin's daily summary, or typed in by the athlete.
    Never sent to the AI."""
    __tablename__ = "resting_heart_rates"
    __table_args__ = (UniqueConstraint("athlete_id", "date"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id", ondelete="CASCADE"), index=True)
    date: Mapped[dt.date] = mapped_column(Date, index=True)
    bpm: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(10))  # garmin | athlete
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class MissedDay(Base):
    """An athlete's report of a day they didn't run, and why. Injury details are the athlete's own words."""
    __tablename__ = "missed_days"
    __table_args__ = (UniqueConstraint("athlete_id", "date"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id", ondelete="CASCADE"), index=True)
    date: Mapped[dt.date] = mapped_column(Date)
    reason: Mapped[str] = mapped_column(String(30))  # Injury | Sick | Planned rest day | School or family | Other
    body_area: Mapped[str] = mapped_column(String(60), default="")
    side: Mapped[str] = mapped_column(String(10), default="")
    pain: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 1-10
    started: Mapped[str] = mapped_column(String(40), default="")
    seen_by: Mapped[str] = mapped_column(String(40), default="")
    more_days: Mapped[str] = mapped_column(String(30), default="")
    description: Mapped[str] = mapped_column(Text, default="")  # never sent to the AI
    injury_id: Mapped[int | None] = mapped_column(ForeignKey("injuries.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    athlete: Mapped[Athlete] = relationship()


class DeletionRequest(Base):
    """An athlete's request to delete everything. Admins see it on the home page and complete it by deleting the athlete."""
    __tablename__ = "deletion_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    athlete_id: Mapped[int | None] = mapped_column(ForeignKey("athletes.id", ondelete="SET NULL"), nullable=True)
    athlete_name: Mapped[str] = mapped_column(String(170), default="")
    status: Mapped[str] = mapped_column(String(10), default="open")  # open | done
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    completed_by: Mapped[str] = mapped_column(String(80), default="")


class Insight(Base):
    __tablename__ = "insights"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("ai_runs.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    priority: Mapped[str] = mapped_column(String(10))
    scope: Mapped[str] = mapped_column(String(200))
    athlete_id: Mapped[int | None] = mapped_column(ForeignKey("athletes.id", ondelete="SET NULL"), nullable=True)
    category: Mapped[str] = mapped_column(String(40))
    headline: Mapped[str] = mapped_column(String(200))
    insight: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)


class AiRun(Base):
    __tablename__ = "ai_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    kind: Mapped[str] = mapped_column(String(20), default="briefing")  # briefing | question
    status: Mapped[str] = mapped_column(String(20), default="started")  # started | ok | failed
    model: Mapped[str] = mapped_column(String(60), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    question: Mapped[str] = mapped_column(Text, default="")
    answer: Mapped[str] = mapped_column(Text, default="")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    insights: Mapped[list[Insight]] = relationship(cascade="all, delete-orphan", order_by="Insight.position")


class Course(Base):
    """A cross country course: general information entered once, used to compare times run on different courses."""
    __tablename__ = "courses"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160), unique=True)       # as it appears in race results, such as "Gibson Ranch"
    location: Mapped[str] = mapped_column(String(200), default="")
    distance_m: Mapped[float | None] = mapped_column(Float, nullable=True)        # the usual race distance here
    distances: Mapped[str] = mapped_column(String(200), default="")               # every distance raced here, such as "2 mi,5K"
    surface: Mapped[str] = mapped_column(String(30), default="Firm grass")
    elevation_gain_ft: Mapped[float | None] = mapped_column(Float, nullable=True)  # total climb over that distance
    altitude_ft: Mapped[float | None] = mapped_column(Float, nullable=True)        # height above sea level
    turns: Mapped[str] = mapped_column(String(10), default="Some")                 # Few | Some | Many
    hill_tier: Mapped[int | None] = mapped_column(Integer, nullable=True)          # 1 flat to 5 very hilly; empty = from the climb
    adjust_s_per_mile: Mapped[float | None] = mapped_column(Float, nullable=True)  # coach's own course adjustment, if set
    map_url: Mapped[str] = mapped_column(String(500), default="")
    video_url: Mapped[str] = mapped_column(String(500), default="")   # a YouTube video of the course, for athletes
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class RaceTeamResult(Base):
    """How the team did in one race at a meet (such as Boys Frosh/Soph): its place among the teams and its score."""
    __tablename__ = "race_team_results"
    __table_args__ = (UniqueConstraint("meet", "date", "race"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    meet: Mapped[str] = mapped_column(String(160))
    date: Mapped[dt.date] = mapped_column(Date, index=True)
    race: Mapped[str] = mapped_column(String(120), default="")
    place: Mapped[int | None] = mapped_column(Integer, nullable=True)     # the team's place
    teams: Mapped[int | None] = mapped_column(Integer, nullable=True)     # out of how many teams
    points: Mapped[float | None] = mapped_column(Float, nullable=True)    # the team's score (lower is better in XC)


class MeetConditions(Base):
    """Race-day conditions at a meet, entered after the event, for comparing times like for like."""
    __tablename__ = "meet_conditions"
    __table_args__ = (UniqueConstraint("meet", "date"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    meet: Mapped[str] = mapped_column(String(160))
    date: Mapped[dt.date] = mapped_column(Date, index=True)
    course_id: Mapped[int | None] = mapped_column(ForeignKey("courses.id", ondelete="SET NULL"), nullable=True)
    temp_f: Mapped[float | None] = mapped_column(Float, nullable=True)
    dew_point_f: Mapped[float | None] = mapped_column(Float, nullable=True)
    humidity_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    wind_mph: Mapped[float | None] = mapped_column(Float, nullable=True)
    footing: Mapped[str] = mapped_column(String(20), default="")    # Dry | Wet | Muddy
    notes: Mapped[str] = mapped_column(Text, default="")
    course: Mapped[Course | None] = relationship()


class TrainingGroup(Base):
    """Ability groups (Bronze, Silver, Gold, ...) by minimum VDOT, separately for boys and girls."""
    __tablename__ = "training_groups"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(40), unique=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    min_vdot_boys: Mapped[float] = mapped_column(Float, default=0)
    min_vdot_girls: Mapped[float] = mapped_column(Float, default=0)
    color: Mapped[str] = mapped_column(String(7), default="")  # badge color, e.g. #B6D7A8
    weekly_miles: Mapped[float] = mapped_column(Float, default=0, server_default="0")  # boys' weekly mileage goal (Monday to Sunday); 0 = none
    weekly_miles_girls: Mapped[float] = mapped_column(Float, default=0, server_default="0")  # girls' weekly mileage goal; 0 = none

    def min_vdot(self, gender: str) -> float:
        return self.min_vdot_girls if gender == "Girls" else self.min_vdot_boys

    def miles_goal(self, gender: str) -> float:
        return (self.weekly_miles_girls if gender == "Girls" else self.weekly_miles) or 0


class AppSetting(Base):
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class SyncLog(Base):
    __tablename__ = "sync_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)
    level: Mapped[str] = mapped_column(String(10), default="info")  # info | warn | error
    kind: Mapped[str] = mapped_column(String(30))
    message: Mapped[str] = mapped_column(Text)


# ---------- weekly newsletter ----------

class NewsletterIssue(Base):
    """One weekly issue: drafted from the team's data, edited and approved by a coach, then sent by hand."""
    __tablename__ = "newsletter_issues"
    id: Mapped[int] = mapped_column(primary_key=True)
    week_start: Mapped[dt.date] = mapped_column(Date, unique=True, index=True)   # the Monday of the week it reports on
    status: Mapped[str] = mapped_column(String(20), default="draft")           # draft | ready_for_review | approved | sent
    template_version: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    generated_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)  # when the numbers were last drawn
    approved_by: Mapped[str] = mapped_column(String(80), default="")
    approved_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    sent_by: Mapped[str] = mapped_column(String(80), default="")
    sent_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    sections: Mapped[list["NewsletterSection"]] = relationship(cascade="all, delete-orphan", order_by="NewsletterSection.position")
    photos: Mapped[list["NewsletterPhoto"]] = relationship(cascade="all, delete-orphan", order_by="NewsletterPhoto.id")


class NewsletterSection(Base):
    """One block of an issue. coach_text is the coach's writing; data_snapshot is the generated numbers, kept apart so
    refreshing the numbers never wipes the coach's edits."""
    __tablename__ = "newsletter_sections"
    __table_args__ = (UniqueConstraint("issue_id", "key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    issue_id: Mapped[int] = mapped_column(ForeignKey("newsletter_issues.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(30))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    coach_text: Mapped[str] = mapped_column(Text, default="")
    data_snapshot: Mapped[str] = mapped_column(Text, default="")   # JSON from the section's builder; "" when it had nothing
    athlete_id: Mapped[int | None] = mapped_column(ForeignKey("athletes.id", ondelete="SET NULL"), nullable=True)  # spotlight


class NewsletterPhoto(Base):
    """A photo in an issue: stored without metadata, with alt text and the athletes shown (each needs photo consent)."""
    __tablename__ = "newsletter_photos"
    id: Mapped[int] = mapped_column(primary_key=True)
    issue_id: Mapped[int] = mapped_column(ForeignKey("newsletter_issues.id", ondelete="CASCADE"), index=True)
    data: Mapped[bytes] = mapped_column(LargeBinary)
    alt: Mapped[str] = mapped_column(String(200))
    caption: Mapped[str] = mapped_column(String(200), default="")
    athlete_ids: Mapped[str] = mapped_column(String(400), default="")   # comma list of the athletes shown
    uploaded_by: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    @property
    def tagged(self) -> list[int]:
        return [int(x) for x in self.athlete_ids.split(",") if x.strip().isdigit()]


class NewsletterSend(Base):
    """Every send, by hand, after approval."""
    __tablename__ = "newsletter_sends"
    id: Mapped[int] = mapped_column(primary_key=True)
    issue_id: Mapped[int] = mapped_column(ForeignKey("newsletter_issues.id", ondelete="CASCADE"), index=True)
    recipient_group: Mapped[str] = mapped_column(String(40))   # coaches (families later)
    recipients: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    sent_by: Mapped[str] = mapped_column(String(80), default="")
    sent_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class NewsletterDeadline(Base):
    """A date families need to know (forms, fees, orders), shown in issues from a set number of days before it."""
    __tablename__ = "newsletter_deadlines"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(160))
    due_date: Mapped[dt.date] = mapped_column(Date, index=True)
    show_from_days: Mapped[int] = mapped_column(Integer, default=14)
    notes: Mapped[str] = mapped_column(String(300), default="")


class RecoveryTip(Base):
    """A short, positive recovery tip. Only tips a coach or trainer has approved go into issues."""
    __tablename__ = "recovery_tips"
    id: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[str] = mapped_column(String(300))
    category: Mapped[str] = mapped_column(String(30), default="General")   # Sleep | Hydration | Fueling | Recovery | General
    approved_by: Mapped[str] = mapped_column(String(80), default="")
    last_used: Mapped[dt.date | None] = mapped_column(Date, nullable=True)


class Spotlight(Base):
    """Who has been featured, so everyone gets a turn."""
    __tablename__ = "newsletter_spotlights"
    id: Mapped[int] = mapped_column(primary_key=True)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id", ondelete="CASCADE"), index=True)
    issue_id: Mapped[int] = mapped_column(ForeignKey("newsletter_issues.id", ondelete="CASCADE"), index=True)
