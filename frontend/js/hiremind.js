/* hiremind.js — HireMind Fit overlay (Phase 3b).
 *
 * Optional by construction. The backend route is gated behind HIREMIND_ENABLED
 * and returns 404 while the flag is off, so this script degrades to nothing:
 * no badge, no sort option, no request on later searches. It never mutates a
 * job's JobAwn fields (ai_score / keyword_score / total_score / reason) and
 * never re-fetches /jobs — it only decorates what is already on screen.
 *
 * Classic script (not a module) so it is available to search.js, matching
 * api.js / track.js. Loaded before search.js.
 */
(function () {
  "use strict";

  var _scores = {};      // url -> { score, verdict, matched_role, matched_skills, reasons }
  var _sid = "";         // session the cached scores belong to
  var _token = 0;        // monotonic guard so a superseded load cannot overwrite a newer one
  var _inflight = {};    // sid -> Promise, dedupes concurrent loads
  var _disabled = false; // sticky: backend answered 403/404/409, stop asking

  var _TIMEOUT_MS = 5000;
  var _REASON_CAP = 3;

  function _esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function reset() {
    _scores = {};
    _sid = "";
    _disabled = false;
    _token++; // invalidate anything still in flight for a previous session
  }

  function hasScores() {
    return Object.keys(_scores).length > 0;
  }

  function get(job) {
    if (!job || !job.url || !_scores[job.url]) return null;
    return _scores[job.url];
  }

  function _loadOnce(sid) {
    var token = ++_token;
    var ctrl = (typeof AbortController !== "undefined") ? new AbortController() : null;
    var timer = setTimeout(function () { if (ctrl) ctrl.abort(); }, _TIMEOUT_MS);

    return fetch("/api/hiremind/search/" + encodeURIComponent(sid), {
      method: "GET",
      headers: (typeof window.getAuthToken === "function" && window.getAuthToken())
        ? { "Authorization": "Bearer " + window.getAuthToken() }
        : {},
      signal: ctrl ? ctrl.signal : undefined,
    }).then(function (r) {
      // 404 = feature flag off, 403 = session is not the caller's, 409 = no
      // HireMind profile. All three are permanent for this page load.
      if (r.status === 404 || r.status === 403 || r.status === 409) {
        _disabled = true;
        return null;
      }
      if (!r.ok) return null;
      return r.json();
    }).then(function (d) {
      if (!d || !d.scores || token !== _token) return;
      var n = Object.keys(d.scores).length;
      if (!n) return;
      _sid = sid;
      _scores = d.scores;
    }).catch(function () {
      // Network error, abort or timeout — stay silent, the page is unaffected.
    }).then(function () {
      clearTimeout(timer);
      delete _inflight[sid];
    });
  }

  /**
   * Fetch the fit scores for a finished search session. Idempotent per sid.
   * Never throws, never rejects — callers can await it unconditionally.
   */
  function load(sid) {
    if (!sid || _disabled) return Promise.resolve();
    if (_sid === sid && hasScores()) return Promise.resolve();
    if (_inflight[sid]) return _inflight[sid];
    // Signed-out users never get a score; skip the request entirely.
    if (typeof window.getAuthToken !== "function" || !window.getAuthToken()) {
      return Promise.resolve();
    }
    _inflight[sid] = _loadOnce(sid);
    return _inflight[sid];
  }

  /**
   * A small "Fit 87" pill for the job card. Returns "" when there is no score,
   * so the card markup is byte-identical to the pre-HireMind overlay.
   * The reasons ride along as a native title tooltip — no click handler, so it
   * cannot interfere with the card's link or any existing event delegation.
   */
  function badgeHtml(job) {
    var m = get(job);
    if (!m || typeof m.score !== "number") return "";
    var cls = m.score >= 80
      ? "bg-emerald-50 text-emerald-700 border-emerald-100/60"
      : m.score >= 60
        ? "bg-brand-50 text-brand-700 border-brand-100/60"
        : "bg-slate-50 text-slate-600 border-slate-200";
    var reasons = (m.reasons || []).slice(0, _REASON_CAP).join(" · ");
    return '<span class="text-[11px] ' + cls + ' border px-2.5 py-0.5 rounded-full font-bold shadow-sm" title="'
      + _esc(reasons) + '">Fit ' + m.score + '</span>';
  }

  /**
   * Score for sorting. Jobs without a fit score sort last rather than being
   * dropped — this overlay never removes a job from the list.
   */
  function scoreOf(job) {
    var m = get(job);
    return m && typeof m.score === "number" ? m.score : -1;
  }

  window.HireMind = {
    load: load,
    reset: reset,
    hasScores: hasScores,
    get: get,
    scoreOf: scoreOf,
    badgeHtml: badgeHtml,
  };
})();
