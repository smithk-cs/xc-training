/* Race Day team scoring (the same rules as xcapp/raceday.py score(); the tests run both on the same cases).
   Only teams with 5+ runners through count. Each scored team's first 7 take places; runners on other teams,
   "Other" and 8th+ runners are skipped and places renumbered. First 5 places add up (low wins); the 6th and 7th
   only push others back; ties go to the better 6th runner. */
(function (root) {
  function score(taps, teams) {
    const sorted = [...taps].sort((a, b) => a.at - b.at);
    const counts = Object.fromEntries(teams.map(t => [t.id, 0]));
    for (const x of sorted) if (Object.prototype.hasOwnProperty.call(counts, x.team)) counts[x.team]++;
    const complete = teams.filter(t => counts[t.id] >= 5).map(t => t.id);
    const per = {}; for (const id of complete) per[id] = [];
    let place = 0;
    const order = sorted.map((x, i) => {
      let tp = null;
      if (per[x.team] && per[x.team].length < 7) { tp = ++place; per[x.team].push({ place: tp, at: x.at }); }
      return { overall: i + 1, at: x.at, team: x.team, tp };
    });
    const standings = complete.map(id => {
      const p = per[id];
      return { id, score: p.slice(0, 5).reduce((s, r) => s + r.place, 0), places: p,
               sixth: p.length > 5 ? p[5].place : Infinity, spread: p[4].at - p[0].at };
    }).sort((a, b) => a.score - b.score || a.sixth - b.sixth);
    const waiting = teams.filter(t => !complete.includes(t.id)).map(t => ({ id: t.id, n: counts[t.id] }));
    return { standings, waiting, order, total: sorted.length };
  }
  // Metres between two GPS points (haversine)
  function metres(a, b) {
    const R = 6371000, r = Math.PI / 180;
    const dLat = (b.lat - a.lat) * r, dLon = (b.lon - a.lon) * r;
    const h = Math.sin(dLat / 2) ** 2 + Math.cos(a.lat * r) * Math.cos(b.lat * r) * Math.sin(dLon / 2) ** 2;
    return 2 * R * Math.asin(Math.min(1, Math.sqrt(h)));
  }
  // GPS pin from pasted coordinates or a Google Maps link
  function pinFromText(text) {
    const res = [/@(-?\d{1,2}\.\d+),\s*(-?\d{1,3}\.\d+)/, /[?&](?:q|ll|query|destination)=(-?\d{1,2}\.\d+)(?:,|%2C)\s*(-?\d{1,3}\.\d+)/i,
                 /^\s*(-?\d{1,2}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)\s*$/];
    for (const rx of res) {
      const m = String(text || "").match(rx);
      if (m) {
        const lat = Number(m[1]), lon = Number(m[2]);
        if (Math.abs(lat) <= 90 && Math.abs(lon) <= 180) return { lat: +lat.toFixed(6), lon: +lon.toFixed(6) };
      }
    }
    return null;
  }
  const api = { score, metres, pinFromText };
  if (typeof module !== "undefined" && module.exports) module.exports = api; else root.XCScore = api;
})(this);
