"""Write real .FIT activity files for tests, using the fit-tool encoder."""
import datetime as dt

from fit_tool.fit_file_builder import FitFileBuilder
from fit_tool.profile.messages.activity_message import ActivityMessage
from fit_tool.profile.messages.event_message import EventMessage
from fit_tool.profile.messages.file_id_message import FileIdMessage
from fit_tool.profile.messages.lap_message import LapMessage
from fit_tool.profile.messages.record_message import RecordMessage
from fit_tool.profile.messages.session_message import SessionMessage
from fit_tool.profile.profile_type import (Event, EventType, FileType, GarminProduct, Intensity, LapTrigger,
                                           Manufacturer, Sport)

INT = {"warmup": Intensity.WARMUP, "active": Intensity.ACTIVE, "interval": Intensity.INTERVAL,
       "rest": Intensity.REST, "cooldown": Intensity.COOLDOWN}


def build_fit(start: dt.datetime, laps: list[dict], serial: int = 0x1234, sport=Sport.RUNNING) -> bytes:
    """laps: dicts with dist (m), dur (s), hr (callable seconds-into-lap -> bpm), intensity, trigger."""
    ms0 = int(start.timestamp()) * 1000
    b = FitFileBuilder(auto_define=True, min_string_size=50)
    f = FileIdMessage()
    f.type = FileType.ACTIVITY
    f.manufacturer = Manufacturer.GARMIN.value
    f.product = GarminProduct.FR265_LARGE.value
    f.time_created = ms0
    f.serial_number = serial
    b.add(f)
    e = EventMessage(); e.event = Event.TIMER; e.event_type = EventType.START; e.timestamp = ms0
    b.add(e)
    t, dist, recs, lapmsgs = 0.0, 0.0, [], []
    for lap in laps:
        n = int(lap["dur"])
        speed = lap["dist"] / lap["dur"]
        hrs = []
        for k in range(n):
            r = RecordMessage()
            r.timestamp = ms0 + int((t + k) * 1000)
            r.distance = dist + speed * k
            r.speed = speed
            hr = int(round(lap["hr"](k)))
            hrs.append(hr)
            r.heart_rate = hr
            recs.append(r)
        lm = LapMessage()
        lm.start_time = ms0 + int(t * 1000)
        lm.timestamp = ms0 + int((t + lap["dur"]) * 1000)
        lm.total_elapsed_time = lap["dur"]
        lm.total_timer_time = lap["dur"]
        lm.total_distance = lap["dist"]
        lm.avg_heart_rate = int(round(sum(hrs) / len(hrs)))
        lm.max_heart_rate = max(hrs)
        lm.intensity = INT[lap.get("intensity", "active")]
        lm.lap_trigger = lap.get("trigger", LapTrigger.MANUAL)
        lm.sport = sport
        lapmsgs.append(lm)
        t += lap["dur"]
        dist += lap["dist"]
    b.add_all(recs)
    b.add_all(lapmsgs)
    s = SessionMessage()
    s.start_time = ms0; s.timestamp = ms0 + int(t * 1000); s.sport = sport
    s.total_distance = dist; s.total_timer_time = t; s.total_elapsed_time = t; s.num_laps = len(laps)
    b.add(s)
    a = ActivityMessage(); a.timestamp = ms0 + int(t * 1000); a.num_sessions = 1; a.total_timer_time = t
    b.add(a)
    return b.build().to_bytes()


def flat(v):
    return lambda k: v


def rep_workout(start, reps, rest_s=90, structured=True, wu=(2000, 600), cd=(1600, 500), rest_m=200):
    """reps: list of (dist, dur, peak_hr, drop_in_first_60s_of_rest)."""
    trig = LapTrigger.SESSION_END if structured else LapTrigger.MANUAL
    laps = [{"dist": wu[0], "dur": wu[1], "hr": flat(140), "intensity": "warmup" if structured else "active", "trigger": trig}]
    for i, (d, dur, peak, drop) in enumerate(reps):
        laps.append({"dist": d, "dur": dur, "hr": (lambda k, p=peak, du=dur: p - 12 + 12 * k / (du - 1)),
                     "intensity": "interval" if structured else "active", "trigger": trig})
        if i < len(reps) - 1:
            laps.append({"dist": rest_m, "dur": rest_s,
                         "hr": (lambda k, p=peak, dr=drop: p - dr * min(k, 60) / 60.0),
                         "intensity": "rest" if structured else "active", "trigger": trig})
    laps.append({"dist": cd[0], "dur": cd[1], "hr": flat(138), "intensity": "cooldown" if structured else "active", "trigger": trig})
    return build_fit(start, laps)
